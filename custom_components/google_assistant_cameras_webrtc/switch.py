"""One switch per exposed, WebRTC-capable camera: advertise WebRTC to Google or not.

Home Assistant keeps devices per config entry, so a switch cannot join the
camera's own device. Instead this integration has one device per camera
device (named like it, sharing its connections so the two show as linked),
carrying one switch per camera entity of that device, named "WebRTC for
<what distinguishes the camera entity on its device>" (plain "WebRTC" when the
device has one). A camera entity without a device gets a device of its own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_entity_registry_updated_event,
    async_track_state_added_domain,
)

from .const import DOMAIN, REASON_USER, SIGNAL_CAMERA_SYNCED, SIGNAL_SWITCH_STATE

if TYPE_CHECKING:
    from . import GoogleAssistantCamerasWebRTCConfigEntry
    from .runtime import Runtime

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

SIGNAL_SWITCH_REMOVED = f"{DOMAIN}_switch_removed"
SIGNAL_RECONCILE = f"{DOMAIN}_reconcile"
UNIQUE_ID_SUFFIX = "_google_assistant_webrtc"
# A Google SYNC serialises every exposed entity in one go; this long after the
# last camera passed through, the SYNC is over and cameras it did not include
# are no longer exposed.
SYNC_SETTLE = 3.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoogleAssistantCamerasWebRTCConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create switches for the cameras Google can stream over WebRTC."""
    runtime = entry.runtime_data
    known: set[str] = set()
    synced: set[str] = set()
    settle: Any = None

    @callback
    def ensure(entity_ids: list[str]) -> None:
        new = [
            WebRTCSwitch(runtime, entity_id)
            for entity_id in entity_ids
            if entity_id not in known and runtime.switch_wanted(entity_id)
        ]
        if not new:
            return
        known.update(switch.camera_entity_id for switch in new)
        async_add_entities(new)

    @callback
    def refresh(_: Event | None = None) -> None:
        _retire_stale(hass, entry, runtime)
        ensure([state.entity_id for state in hass.states.async_all("camera")])

    @callback
    def camera_added(event: Event[EventStateChangedData]) -> None:
        ensure([event.data["entity_id"]])

    @callback
    def sync_settled(_now: Any) -> None:
        nonlocal settle
        settle = None
        # cameras with a switch that this SYNC did not include are gone from
        # Google's point of view (un-exposed); their switches go
        async_dispatcher_send(hass, SIGNAL_RECONCILE, frozenset(synced))
        synced.clear()

    @callback
    def camera_synced(entity_id: str) -> None:
        nonlocal settle
        synced.add(entity_id)
        ensure([entity_id])
        if settle is not None:
            settle()
        settle = async_call_later(hass, SYNC_SETTLE, sync_settled)

    @callback
    def forget(entity_id: str) -> None:
        known.discard(entity_id)

    @callback
    def cancel_settle() -> None:
        if settle is not None:
            settle()

    entry.async_on_unload(cancel_settle)

    # WebRTC providers are attached once Home Assistant is up; before that a
    # camera may not report WebRTC yet, so switches are created when it is,
    # when a camera appears and whenever a SYNC passes through a camera (the
    # moment Google is known to expose it).
    if hass.is_running:
        refresh()
    else:
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, refresh)
        )
    entry.async_on_unload(async_track_state_added_domain(hass, "camera", camera_added))
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_CAMERA_SYNCED, camera_synced)
    )
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_SWITCH_REMOVED, forget))


def _retire_stale(
    hass: HomeAssistant,
    entry: GoogleAssistantCamerasWebRTCConfigEntry,
    runtime: Runtime,
) -> None:
    """Remove registry entries of switches whose camera is gone or no longer wanted."""
    registry = er.async_get(hass)
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not reg.unique_id.endswith(UNIQUE_ID_SUFFIX):
            continue
        camera_entity_id = reg.unique_id[: -len(UNIQUE_ID_SUFFIX)]
        if runtime.switch_wanted(camera_entity_id):
            continue
        _LOGGER.debug("%s: removing stale switch", camera_entity_id)
        registry.async_remove(reg.entity_id)
    _remove_empty_devices(hass, entry.entry_id)


class WebRTCSwitch(SwitchEntity):
    """Whether WebRTC is advertised to Google for one camera."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False
    _attr_icon = "mdi:video-wireless"

    def __init__(self, runtime: Runtime, camera_entity_id: str) -> None:
        """Bind to a camera."""
        self._runtime = runtime
        self.camera_entity_id = camera_entity_id
        self._attr_unique_id = f"{camera_entity_id}{UNIQUE_ID_SUFFIX}"
        state = runtime.hass.states.get(camera_entity_id)
        camera_name = state.name if state is not None else camera_entity_id
        device = _camera_device(runtime.hass, camera_entity_id)
        device_name = (
            (device.name_by_user or device.name) if device is not None else None
        ) or camera_name
        # "WebRTC for <what distinguishes this camera entity on its device>"
        short = camera_name
        if short.lower().startswith(device_name.lower()):
            short = short[len(device_name) :].strip(" -:")
        self._attr_name = f"WebRTC for {short}" if short else "WebRTC"
        self._attr_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, device.id if device is not None else camera_entity_id)
            },
            connections=set(device.connections) if device is not None else set(),
            name=device_name,
            model="WebRTC for Google Assistant",
        )

    @property
    def is_on(self) -> bool:
        """Return True when WebRTC is advertised for the camera."""
        return self.camera_entity_id not in self._runtime.disabled

    @property
    def available(self) -> bool:
        """Follow the camera."""
        return self._runtime.hass.states.get(self.camera_entity_id) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show the camera and, when off, why."""
        attrs: dict[str, Any] = {"camera": self.camera_entity_id}
        if (reason := self._runtime.disabled.get(self.camera_entity_id)) is not None:
            attrs["reason"] = reason
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Advertise WebRTC again (the retry after a failure)."""
        self._runtime.enable(self.camera_entity_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop advertising WebRTC for this camera."""
        self._runtime.disable(self.camera_entity_id, REASON_USER)

    async def async_added_to_hass(self) -> None:
        """Follow runtime changes, the camera's registry entry and Google's SYNCs."""

        @callback
        def switched(entity_id: str) -> None:
            if entity_id == self.camera_entity_id:
                self.async_write_ha_state()

        @callback
        def registry_updated(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
            if event.data["action"] == "remove":
                self.hass.async_create_task(self._async_retire("camera removed"))
                return
            entry = er.async_get(self.hass).async_get(self.camera_entity_id)
            if entry is None or entry.disabled:
                self.hass.async_create_task(self._async_retire("camera disabled"))
            elif not self._runtime.google_exposes(self.camera_entity_id):
                self.hass.async_create_task(
                    self._async_retire("camera no longer exposed to Google")
                )
            else:
                self.async_write_ha_state()

        @callback
        def reconcile(synced: frozenset[str]) -> None:
            if self.camera_entity_id not in synced:
                self.hass.async_create_task(
                    self._async_retire("camera not in Google's last SYNC")
                )

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SWITCH_STATE, switched)
        )
        self.async_on_remove(
            async_track_entity_registry_updated_event(
                self.hass, self.camera_entity_id, registry_updated
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_RECONCILE, reconcile)
        )

    async def _async_retire(self, why: str) -> None:
        """Remove the switch, and its device once nothing of ours is left on it."""
        _LOGGER.debug("%s: removing switch (%s)", self.camera_entity_id, why)
        async_dispatcher_send(self.hass, SIGNAL_SWITCH_REMOVED, self.camera_entity_id)
        registry = er.async_get(self.hass)
        if self.entity_id and registry.async_get(self.entity_id) is not None:
            registry.async_remove(self.entity_id)
        else:
            await self.async_remove(force_remove=True)
        _remove_empty_devices(self.hass, self._runtime.entry.entry_id)


def _remove_empty_devices(hass: HomeAssistant, entry_id: str) -> None:
    """Remove this integration's devices that carry no entity any more."""
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(devices, entry_id):
        if not er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        ):
            devices.async_remove_device(device.id)


def _camera_device(hass: HomeAssistant, entity_id: str) -> dr.DeviceEntry | None:
    """Return the camera's device, if it has one."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None
    return dr.async_get(hass).async_get(entry.device_id)
