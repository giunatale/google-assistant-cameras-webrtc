"""Google Assistant WebRTC Cameras.

Makes Home Assistant offer WebRTC (with HLS fallback) when Google Assistant
shows a camera on a Nest display. No configuration: installed means enabled.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, __version__ as HA_VERSION
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import patch
from .breaker import Breaker
from .const import (
    DATA_RUNTIME,
    DATA_SYNC,
    DOMAIN,
    ISSUE_CORE_HAS_WEBRTC,
    ISSUE_TRAIT_CHANGED,
    NAME,
    SESSION_SWEEP_INTERVAL,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .runtime import Runtime
from .sync import SyncRequester

_LOGGER = logging.getLogger(__name__)

type GoogleAssistantCamerasWebRTCConfigEntry = ConfigEntry[Runtime]


async def async_setup_entry(
    hass: HomeAssistant, entry: GoogleAssistantCamerasWebRTCConfigEntry
) -> bool:
    """Patch the Google Assistant camera trait and start serving sessions."""
    sync: SyncRequester = hass.data.get(DATA_SYNC) or SyncRequester(hass)
    hass.data[DATA_SYNC] = sync
    runtime = Runtime(
        hass=hass,
        entry=entry,
        sync=sync,
        breaker=Breaker(now=dt_util.utcnow),
        store=Store(hass, STORAGE_VERSION, STORAGE_KEY),
    )
    runtime.stand_down = lambda reason: async_stand_down(hass, runtime, reason)
    sync.on_result = runtime.mark_synced
    await runtime.async_load()
    entry.runtime_data = runtime
    hass.data[DATA_RUNTIME] = runtime

    try:
        runtime.patch = patch.apply(hass)
    except patch.CoreHasWebRTC:
        _stand_down_issue(hass, "core_has_webrtc", "")
        _LOGGER.warning(
            "Home Assistant %s already offers WebRTC to Google Assistant; "
            "%s stands down and can be removed",
            HA_VERSION,
            NAME,
        )
        return True
    except patch.ShapeMismatch as err:
        _stand_down_issue(hass, "trait_changed", str(err))
        _LOGGER.warning(
            "Home Assistant %s changed the Google Assistant camera trait (%s); "
            "%s stands down, Google Assistant keeps stock HLS",
            HA_VERSION,
            err,
            NAME,
        )
        return True
    except Exception:
        _stand_down_issue(hass, "trait_changed", "unexpected error, see the log")
        _LOGGER.exception(
            "Could not extend the Google Assistant camera trait; %s stands down", NAME
        )
        return True

    runtime.active = True
    ir.async_delete_issue(hass, DOMAIN, ISSUE_TRAIT_CHANGED)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_CORE_HAS_WEBRTC)
    _LOGGER.info(
        "%s active on Home Assistant %s: Google Assistant will be offered WebRTC "
        "for cameras with WebRTC support (HLS stays as fallback)",
        NAME,
        HA_VERSION,
    )

    entry.async_on_unload(
        async_track_time_interval(
            hass, lambda _now: runtime.sessions.sweep(), SESSION_SWEEP_INTERVAL
        )
    )

    @callback
    def _on_stop(_event: Event) -> None:
        runtime.stop("unload")
        sync.cancel()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    )

    if not runtime.synced:
        sync.request("first activation")
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: GoogleAssistantCamerasWebRTCConfigEntry
) -> bool:
    """Restore the stock trait and close everything."""
    runtime: Runtime = entry.runtime_data
    was_active = runtime.active
    runtime.stop("unload")
    if runtime.patch is not None:
        if patch.remove(runtime.patch):
            _LOGGER.info("Restored the stock Google Assistant camera trait")
        runtime.patch = None
    hass.data.pop(DATA_RUNTIME, None)
    await runtime.async_save()
    if was_active:
        runtime.sync.request("integration unloaded")
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget stored state and repairs; make sure Google learns about it."""
    sync: SyncRequester | None = hass.data.get(DATA_SYNC)
    if sync is not None:
        ok = await sync.async_run()
        sync.clear_issue()
        if not ok:
            persistent_notification.async_create(
                hass,
                f"{NAME} was removed but Home Assistant could not ask Google to "
                're-sync. Say "Hey Google, sync my devices" so your cameras go back '
                "to the stock stream type.",
                title=NAME,
                notification_id=f"{DOMAIN}_removed",
            )
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_remove()
    hass.data.pop(DATA_SYNC, None)


@callback
def async_stand_down(hass: HomeAssistant, runtime: Runtime, reason: str) -> None:
    """Deactivate at runtime (HA grew native WebRTC after we patched)."""
    if not runtime.active:
        return
    runtime.stop("stand_down")
    if runtime.patch is not None:
        patch.remove(runtime.patch)
        runtime.patch = None
    _stand_down_issue(hass, reason, "")
    _LOGGER.warning(
        "%s stood down (%s); Google Assistant keeps stock behaviour", NAME, reason
    )


def _stand_down_issue(hass: HomeAssistant, key: str, detail: str) -> None:
    placeholders: dict[str, Any] = {"ha_version": HA_VERSION}
    if key == "trait_changed":
        placeholders["detail"] = detail or "unknown"
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_CORE_HAS_WEBRTC if key == "core_has_webrtc" else ISSUE_TRAIT_CHANGED,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders=placeholders,
    )
