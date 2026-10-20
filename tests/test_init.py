"""Setup, unload, removal, stand-down, Google sync and diagnostics."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.google_assistant import trait as ga_trait
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.google_assistant_cameras_webrtc.const import (
    DATA_RUNTIME,
    DATA_SYNC,
    DOMAIN,
    ISSUE_CORE_HAS_WEBRTC,
    ISSUE_SYNC_NEEDED,
    ISSUE_TRAIT_CHANGED,
    SYNC_SCHEMA_VERSION,
)
from custom_components.google_assistant_cameras_webrtc.sync import (
    async_request_google_sync,
)

from .conftest import ENTITY_ID, make_trait

SYNC = (
    "custom_components.google_assistant_cameras_webrtc.sync.async_request_google_sync"
)


async def _debounce(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()


async def test_setup_and_unload(hass: HomeAssistant, setup_integration, caplog):
    cls = ga_trait.CameraStreamTrait
    runtime = setup_integration.runtime_data
    assert setup_integration.state is ConfigEntryState.LOADED
    assert (
        runtime.active and runtime.patch.active and hass.data[DATA_RUNTIME] is runtime
    )
    assert len(setup_integration.data["webhook_id"]) == 64
    assert any(
        "active on Home Assistant" in r.getMessage()
        for r in caplog.get_records("setup")
    )
    original = runtime.patch.orig_sync
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert cls.__dict__["sync_attributes"] is original and DATA_RUNTIME not in hass.data
    assert "Restored the stock" in caplog.text


async def test_webhook_id_survives_reload(hass: HomeAssistant, setup_integration):
    webhook_id = setup_integration.data["webhook_id"]
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.data["webhook_id"] == webhook_id
    assert setup_integration.runtime_data.active


async def test_first_activation_and_schema_sync(
    hass: HomeAssistant, config_entry, https_url, cameras
):
    config_entry.add_to_hass(hass)
    with patch(SYNC, new=AsyncMock(return_value=(True, "test link"))) as req:
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        runtime = config_entry.runtime_data
        assert not runtime.synced
        await _debounce(hass)
        assert (
            req.await_count == 1
            and runtime.synced
            and runtime.sync_schema == SYNC_SCHEMA_VERSION
        )
        await runtime.async_save()
        stored = await runtime.store.async_load()
        assert stored["synced"] is True and stored["sync_schema"] == SYNC_SCHEMA_VERSION
        # a reload requests one sync from the unload, none from the setup
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
        await _debounce(hass)
        assert req.await_count == 2
        # an older schema on disk requests a sync at setup
        await config_entry.runtime_data.store.async_save(
            {"synced": True, "sync_schema": SYNC_SCHEMA_VERSION - 1, "disabled": {}}
        )
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
        await _debounce(hass)
        assert (
            req.await_count == 3
            and config_entry.runtime_data.sync_schema == SYNC_SCHEMA_VERSION
        )  # unload + schema coalesce
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_sync_failure_creates_repair_cleared_by_next_sync(
    hass: HomeAssistant, setup_integration
):
    await _debounce(hass)
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, ISSUE_SYNC_NEEDED)
    assert (
        issue is not None
        and issue.is_fixable
        and "no Google Assistant link" in issue.translation_placeholders["reason"]
    )
    make_trait(hass).sync_attributes()
    assert registry.async_get_issue(DOMAIN, ISSUE_SYNC_NEEDED) is None


async def test_request_sync_prefers_cloud_then_service_account(hass: HomeAssistant):
    assert await async_request_google_sync(hass) == (
        False,
        "no Google Assistant link found",
    )
    gconf = MagicMock(enabled=True, async_sync_entities_all=AsyncMock(return_value=200))
    cloud = MagicMock(is_logged_in=True)
    cloud.client.get_google_config = AsyncMock(return_value=gconf)
    hass.data["cloud"] = cloud
    assert await async_request_google_sync(hass) == (True, "Nabu Casa")
    gconf.async_sync_entities_all.return_value = 500
    ok, detail = await async_request_google_sync(hass)
    assert not ok and "500" in detail
    hass.data.pop("cloud")
    entry = MockConfigEntry(domain="google_assistant", data={"project_id": "x"})
    entry.add_to_hass(hass)
    entry.runtime_data = MagicMock(async_sync_entities_all=AsyncMock(return_value=200))
    ok, detail = await async_request_google_sync(hass)
    assert not ok and "service_account" in detail
    hass.services.async_register("google_assistant", "request_sync", lambda call: None)
    assert await async_request_google_sync(hass) == (
        True,
        "google_assistant service account",
    )


async def test_shape_mismatch_stands_down(
    hass: HomeAssistant, config_entry, https_url, cameras, monkeypatch, caplog
):
    monkeypatch.setattr(ga_trait.CameraStreamTrait, "commands", [])
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    assert not runtime.active and runtime.patch is None
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_TRAIT_CHANGED)
    assert (
        issue is not None
        and "GetCameraStream" in issue.translation_placeholders["detail"]
    )
    assert make_trait(hass).sync_attributes()["cameraStreamSupportedProtocols"] == [
        "hls"
    ]
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_core_native_webrtc_stands_down(
    hass: HomeAssistant, config_entry, https_url, cameras, monkeypatch
):
    monkeypatch.setattr(
        ga_trait.CameraStreamTrait,
        "sync_attributes",
        lambda self: {
            "cameraStreamSupportedProtocols": ["webrtc", "hls"],
            "cameraStreamNeedAuthToken": False,
        },
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert not config_entry.runtime_data.active
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CORE_HAS_WEBRTC) is not None
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_runtime_stand_down_when_core_grows_webrtc(
    hass: HomeAssistant, setup_integration, caplog
):
    runtime = setup_integration.runtime_data
    attrs = runtime.decorate_sync_attributes(
        ENTITY_ID, {"cameraStreamSupportedProtocols": ["webrtc", "hls"]}
    )
    assert attrs["cameraStreamSupportedProtocols"] == ["webrtc", "hls"]
    await hass.async_block_till_done()
    assert not runtime.active and runtime.patch is None and "stood down" in caplog.text
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CORE_HAS_WEBRTC) is not None


async def test_remove_entry_cleans_up(
    hass: HomeAssistant, config_entry, https_url, cameras
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    runtime.disable(ENTITY_ID, "codec")
    await runtime.async_save()
    with (
        patch(SYNC, new=AsyncMock(return_value=(False, "nope"))),
        patch(
            "custom_components.google_assistant_cameras_webrtc.persistent_notification.async_create"
        ) as notify,
    ):
        await hass.config_entries.async_remove(config_entry.entry_id)
        await hass.async_block_till_done()
    assert not [key for key in ir.async_get(hass).issues if key[0] == DOMAIN]
    assert DATA_SYNC not in hass.data and "sync my devices" in notify.call_args.args[1]
    assert await runtime.store.async_load() is None


async def test_diagnostics(hass: HomeAssistant, setup_integration, hass_client):
    assert await async_setup_component(hass, "diagnostics", {})
    await hass.async_block_till_done()
    client = await hass_client()
    resp = await client.get(
        f"/api/diagnostics/config_entry/{setup_integration.entry_id}"
    )
    assert resp.status == 200
    data = (await resp.json())["data"]
    assert (
        data["state"] == "active"
        and data["patch_in_place"]
        and data["google_url_scheme"] == "https"
    )
    assert {c["entity_id"]: c["offered_webrtc"] for c in data["cameras"]} == {
        ENTITY_ID: True,
        "camera.other_camera": True,
    }
    assert "ha.example" not in str(data) and "webhook" not in str(data)


async def test_config_flow_single_instance(hass: HomeAssistant, setup_integration):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort" and result["reason"] == "single_instance_allowed"
