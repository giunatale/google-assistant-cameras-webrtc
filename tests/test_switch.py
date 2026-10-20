"""The per-camera 'WebRTC for Google Assistant' switch."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.google_assistant_cameras_webrtc.const import (
    DOMAIN,
    ISSUE_CAMERA_UNSUPPORTED_PREFIX,
    REASON_USER,
)
from custom_components.google_assistant_cameras_webrtc.switch import SYNC_SETTLE

from .conftest import ENTITY_ID, OTHER_ENTITY_ID

SWITCH_ID = "switch.test_camera_webrtc"
OTHER_SWITCH_ID = "switch.other_camera_webrtc"


def google_link(hass: HomeAssistant, exposed: set[str]) -> MockConfigEntry:
    """A Google Assistant entry whose config exposes the given cameras."""
    entry = MockConfigEntry(domain="google_assistant", data={})
    entry.add_to_hass(hass)
    entry.runtime_data = MagicMock(should_expose=lambda entity_id: entity_id in exposed)
    return entry


async def test_switches_for_exposed_capable_cameras(
    hass: HomeAssistant, setup_integration
) -> None:
    """Without a Google link both cameras get a switch; the switch is on by default."""
    for switch_id in (SWITCH_ID, OTHER_SWITCH_ID):
        state = hass.states.get(switch_id)
        assert state is not None and state.state == "on"
    assert hass.states.get(SWITCH_ID).attributes["camera"] == ENTITY_ID
    entry = er.async_get(hass).async_get(SWITCH_ID)
    assert (
        entry is not None and entry.unique_id == f"{ENTITY_ID}_google_assistant_webrtc"
    )
    assert entry.entity_category == "config"


async def test_only_exposed_cameras_get_a_switch(
    hass: HomeAssistant, config_entry, https_url, cameras, trait
) -> None:
    """Cameras Google does not expose have no switch; a SYNC creates it once exposed."""
    link = google_link(hass, {ENTITY_ID})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH_ID) is not None
    assert hass.states.get(OTHER_SWITCH_ID) is None

    link.runtime_data = MagicMock(should_expose=lambda entity_id: True)
    trait(OTHER_ENTITY_ID).sync_attributes()
    await hass.async_block_till_done()
    assert hass.states.get(OTHER_SWITCH_ID) is not None


async def test_switch_off_and_on(hass: HomeAssistant, setup_integration, trait) -> None:
    """Off: stock protocols, persisted, re-sync, no repair. On: WebRTC again."""
    runtime = setup_integration.runtime_data
    with patch.object(runtime.sync, "request") as request:
        await hass.services.async_call(
            "switch", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: SWITCH_ID}, blocking=True
        )
    state = hass.states.get(SWITCH_ID)
    assert state.state == "off" and state.attributes["reason"] == REASON_USER
    assert runtime.disabled == {ENTITY_ID: REASON_USER}
    assert trait().sync_attributes()["cameraStreamSupportedProtocols"] == ["hls"]
    assert trait(OTHER_ENTITY_ID).sync_attributes()[
        "cameraStreamSupportedProtocols"
    ] == ["webrtc", "hls"]
    request.assert_called_once_with("camera disabled")
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{ENTITY_ID}"
        )
        is None
    )
    await runtime.async_save()
    assert (await runtime.store.async_load())["disabled"] == {ENTITY_ID: REASON_USER}

    with patch.object(runtime.sync, "request") as request:
        await hass.services.async_call(
            "switch", SERVICE_TURN_ON, {ATTR_ENTITY_ID: SWITCH_ID}, blocking=True
        )
    state = hass.states.get(SWITCH_ID)
    assert state.state == "on" and "reason" not in state.attributes
    assert not runtime.disabled
    assert trait().sync_attributes()["cameraStreamSupportedProtocols"] == [
        "webrtc",
        "hls",
    ]
    request.assert_called_once_with("camera enabled")


async def test_failure_turns_switch_off_and_on_is_the_retry(
    hass: HomeAssistant, setup_integration
) -> None:
    """A negotiation failure flips the switch and raises a repair; turning it on clears it."""
    runtime = setup_integration.runtime_data
    runtime.disable(ENTITY_ID, "codecs not matched")
    await hass.async_block_till_done()
    state = hass.states.get(SWITCH_ID)
    assert state.state == "off" and state.attributes["reason"] == "codecs not matched"
    issue_id = f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{ENTITY_ID}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    await hass.services.async_call(
        "switch", SERVICE_TURN_ON, {ATTR_ENTITY_ID: SWITCH_ID}, blocking=True
    )
    assert hass.states.get(SWITCH_ID).state == "on"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_switch_goes_with_its_camera(
    hass: HomeAssistant, setup_integration
) -> None:
    """Removing the camera entity removes its switch."""
    registry = er.async_get(hass)
    assert registry.async_get(SWITCH_ID) is not None
    registry.async_remove(ENTITY_ID)
    await hass.async_block_till_done()
    assert registry.async_get(SWITCH_ID) is None
    assert hass.states.get(SWITCH_ID) is None
    assert (
        dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, ENTITY_ID), setup_integration.entry_id
        )
        is None
    )
    assert hass.states.get(OTHER_SWITCH_ID) is not None


async def test_switch_goes_when_camera_is_hidden_from_google(
    hass: HomeAssistant, config_entry, https_url, cameras
) -> None:
    """Un-exposing the camera in Google Assistant removes its switch."""
    exposed = {ENTITY_ID, OTHER_ENTITY_ID}
    google_link(hass, exposed)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH_ID) is not None

    exposed.discard(ENTITY_ID)
    registry = er.async_get(hass)
    registry.async_update_entity_options(ENTITY_ID, "google_assistant", {"foo": 1})
    await hass.async_block_till_done()
    assert registry.async_get(SWITCH_ID) is None
    assert hass.states.get(OTHER_SWITCH_ID) is not None


async def test_switch_goes_when_camera_is_disabled(
    hass: HomeAssistant, setup_integration
) -> None:
    """Disabling the camera entity removes its switch and device."""
    registry = er.async_get(hass)
    registry.async_update_entity(ENTITY_ID, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    assert registry.async_get(SWITCH_ID) is None
    assert (
        dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, ENTITY_ID), setup_integration.entry_id
        )
        is None
    )


async def test_sync_without_the_camera_retires_its_switch(
    hass: HomeAssistant, setup_integration, trait
) -> None:
    """A full SYNC that no longer includes a camera means Google stopped exposing it."""
    trait(OTHER_ENTITY_ID).sync_attributes()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SYNC_SETTLE + 1))
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH_ID) is None
    assert hass.states.get(OTHER_SWITCH_ID) is not None
    # the camera comes back in a later SYNC: so does the switch
    trait().sync_attributes()
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH_ID) is not None


async def test_stale_switches_are_removed_at_setup(
    hass: HomeAssistant, config_entry, https_url, cameras
) -> None:
    """A registry entry left over for a camera that is gone is cleaned up at setup."""
    registry = er.async_get(hass)
    config_entry.add_to_hass(hass)
    stale = registry.async_get_or_create(
        "switch",
        DOMAIN,
        "camera.gone_google_assistant_webrtc",
        config_entry=config_entry,
        suggested_object_id="gone_webrtc",
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(stale.entity_id) is None
    assert hass.states.get(SWITCH_ID) is not None


async def test_one_device_per_camera_device(
    hass: HomeAssistant, config_entry, https_url, cameras
) -> None:
    """Two camera entities of one physical camera share one device here, linked by MAC."""
    camera_entry = MockConfigEntry(domain="test", data={})
    camera_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    camera_device = devices.async_get_or_create(
        config_entry_id=camera_entry.entry_id,
        identifiers={("test", "cam")},
        connections={(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")},
        name="Cam",
    )
    registry = er.async_get(hass)
    for entity_id in (ENTITY_ID, OTHER_ENTITY_ID):
        registry.async_update_entity(entity_id, device_id=camera_device.id)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    first = hass.states.get("switch.cam_webrtc_for_test_camera")
    second = hass.states.get("switch.cam_webrtc_for_other_camera")
    assert first is not None and second is not None
    assert first.name == "Cam WebRTC for Test camera"
    own = devices.async_get_device_by_identifier(
        (DOMAIN, camera_device.id), config_entry.entry_id
    )
    assert own is not None and own.name == "Cam"
    assert (dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff") in own.connections
    assert {e.entity_id for e in er.async_entries_for_device(registry, own.id)} == {
        first.entity_id,
        second.entity_id,
    }
    # one camera goes: its switch goes, the device stays for the other
    registry.async_remove(ENTITY_ID)
    await hass.async_block_till_done()
    assert hass.states.get(first.entity_id) is None
    assert devices.async_get(own.id) is not None
    registry.async_remove(OTHER_ENTITY_ID)
    await hass.async_block_till_done()
    assert devices.async_get(own.id) is None
