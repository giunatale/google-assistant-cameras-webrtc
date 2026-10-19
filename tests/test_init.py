"""Setup, unload, removal, stand-down and Google sync behaviour."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.google_assistant import trait as ga_trait
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
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
)
from custom_components.google_assistant_cameras_webrtc.sync import (
    async_request_google_sync,
)

from .conftest import make_trait


async def test_setup_patches_and_unload_restores(
    hass: HomeAssistant, setup_integration, caplog
):
    cls = ga_trait.CameraStreamTrait
    runtime = setup_integration.runtime_data
    assert setup_integration.state is ConfigEntryState.LOADED
    assert runtime.active and runtime.patch.active
    assert hass.data[DATA_RUNTIME] is runtime
    assert any(
        "active on Home Assistant" in r.getMessage()
        for r in caplog.get_records("setup")
    )
    original_sync = runtime.patch.orig_sync
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert cls.__dict__["sync_attributes"] is original_sync
    assert DATA_RUNTIME not in hass.data
    assert not runtime.active
    assert "Restored the stock" in caplog.text


async def test_first_activation_requests_sync_then_remembers(
    hass: HomeAssistant, config_entry, https_url, camera_entity
):
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.google_assistant_cameras_webrtc.sync.async_request_google_sync",
        new=AsyncMock(return_value=(True, "test link")),
    ) as req:
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        runtime = config_entry.runtime_data
        assert not runtime.synced
        async_fire_time_changed(
            hass, dt_util.utcnow() + dt_util.dt.timedelta(seconds=6)
        )
        await hass.async_block_till_done()
        assert req.await_count == 1
        assert runtime.synced
        await runtime.async_save()
        assert (await runtime.store.async_load())["synced"] is True
        # reload: no new sync request from setup, one from unload (debounced)
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.runtime_data.synced
        async_fire_time_changed(
            hass, dt_util.utcnow() + dt_util.dt.timedelta(seconds=6)
        )
        await hass.async_block_till_done()
        assert req.await_count == 2
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_sync_failure_creates_repair_cleared_by_next_sync(
    hass: HomeAssistant, config_entry, https_url, camera_entity
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + dt_util.dt.timedelta(seconds=6))
    await hass.async_block_till_done()
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, ISSUE_SYNC_NEEDED)
    assert issue is not None and issue.is_fixable
    assert "no Google Assistant link" in issue.translation_placeholders["reason"]
    # Google syncs: our sync_attributes wrapper runs -> issue clears
    make_trait(hass).sync_attributes()
    assert registry.async_get_issue(DOMAIN, ISSUE_SYNC_NEEDED) is None
    await hass.config_entries.async_unload(config_entry.entry_id)


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
    # self-hosted project without a service account
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


async def test_shape_mismatch_stands_down_with_repair(
    hass: HomeAssistant, config_entry, https_url, camera_entity, monkeypatch, caplog
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
    assert "stands down" in caplog.text
    assert make_trait(hass).sync_attributes()["cameraStreamSupportedProtocols"] == [
        "hls"
    ]
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_core_native_webrtc_stands_down(
    hass: HomeAssistant, config_entry, https_url, camera_entity, monkeypatch
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
    """HA updated underneath us: the first SYNC that already lists webrtc deactivates."""
    runtime = setup_integration.runtime_data
    with patch.object(
        runtime.patch,
        "orig_sync",
        return_value={"cameraStreamSupportedProtocols": ["webrtc", "hls"]},
    ):
        attrs = runtime.decorate_sync_attributes(
            "camera.test_camera", {"cameraStreamSupportedProtocols": ["webrtc", "hls"]}
        )
    assert attrs["cameraStreamSupportedProtocols"] == ["webrtc", "hls"]
    await hass.async_block_till_done()
    assert not runtime.active and runtime.patch is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CORE_HAS_WEBRTC) is not None
    assert "stood down" in caplog.text


async def test_remove_entry_cleans_up(
    hass: HomeAssistant, config_entry, https_url, camera_entity
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    runtime.breaker.mark_unsupported("camera.test_camera", "codec")
    await runtime.async_save()
    ir.async_create_issue(
        hass,
        DOMAIN,
        "leftover",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="trait_changed",
    )
    with (
        patch(
            "custom_components.google_assistant_cameras_webrtc.sync.async_request_google_sync",
            new=AsyncMock(return_value=(False, "nope")),
        ),
        patch(
            "custom_components.google_assistant_cameras_webrtc.persistent_notification.async_create"
        ) as notify,
    ):
        await hass.config_entries.async_remove(config_entry.entry_id)
        await hass.async_block_till_done()
    assert not [key for key in ir.async_get(hass).issues if key[0] == DOMAIN]
    assert DATA_SYNC not in hass.data
    notify.assert_called_once()
    assert "sync my devices" in notify.call_args.args[1]
    assert await runtime.store.async_load() is None


async def test_diagnostics(hass: HomeAssistant, setup_integration, hass_client):
    from homeassistant.components.diagnostics import DOMAIN as DIAG
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, DIAG, {})
    await hass.async_block_till_done()
    client = await hass_client()
    resp = await client.get(
        f"/api/diagnostics/config_entry/{setup_integration.entry_id}"
    )
    assert resp.status == 200
    data = (await resp.json())["data"]
    assert data["state"] == "active" and data["patch_in_place"]
    assert data["google_url_scheme"] == "https"
    assert data["cameras"][0] == {
        "entity_id": "camera.test_camera",
        "webrtc_capable": True,
        "provider": "fake",
        "offered_webrtc": True,
        "reason": None,
    }
    assert "ha.example" not in str(data)


async def test_config_flow_single_instance(hass: HomeAssistant, setup_integration):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort" and result["reason"] == "single_instance_allowed"
