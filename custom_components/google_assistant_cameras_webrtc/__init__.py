"""Google Assistant WebRTC Cameras.

Makes Home Assistant offer WebRTC (with HLS as fallback) when Google Assistant
shows a camera on a Google device. No configuration: installed means enabled.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    Platform,
    __version__ as HA_VERSION,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from . import patch
from .const import (
    CONF_WEBHOOK_ID,
    DATA_RUNTIME,
    DATA_SYNC,
    DOMAIN,
    ISSUE_CORE_HAS_WEBRTC,
    ISSUE_TRAIT_CHANGED,
    NAME,
    SESSION_SWEEP_INTERVAL,
    STORAGE_KEY,
    STORAGE_VERSION,
    SYNC_SCHEMA_VERSION,
)
from .runtime import Runtime
from .sync import SyncRequester

_LOGGER = logging.getLogger(__name__)

type GoogleAssistantCamerasWebRTCConfigEntry = ConfigEntry[Runtime]

PLATFORMS = [Platform.SWITCH]


async def async_setup_entry(
    hass: HomeAssistant, entry: GoogleAssistantCamerasWebRTCConfigEntry
) -> bool:
    """Extend the Google Assistant camera trait and register the signalling webhook."""
    if not entry.data.get(CONF_WEBHOOK_ID):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_ID: secrets.token_hex(32)}
        )

    sync: SyncRequester = hass.data.get(DATA_SYNC) or SyncRequester(hass)
    hass.data[DATA_SYNC] = sync
    runtime = Runtime(
        hass=hass,
        entry=entry,
        sync=sync,
        store=Store(hass, STORAGE_VERSION, STORAGE_KEY),
    )
    runtime.stand_down = lambda reason: async_stand_down(hass, runtime, reason)
    sync.on_result = lambda ok: runtime.mark_synced(ok, SYNC_SCHEMA_VERSION)
    await runtime.async_load()
    entry.runtime_data = runtime
    hass.data[DATA_RUNTIME] = runtime

    try:
        runtime.patch = patch.apply(hass)
    except patch.CoreHasWebRTC:
        _stand_down_issue(hass, ISSUE_CORE_HAS_WEBRTC, "")
        _LOGGER.warning(
            "Home Assistant %s already offers WebRTC to Google Assistant; %s stands down",
            HA_VERSION,
            NAME,
        )
        return True
    except patch.ShapeMismatch as err:
        _stand_down_issue(hass, ISSUE_TRAIT_CHANGED, str(err))
        _LOGGER.warning(
            "Home Assistant %s changed the Google Assistant camera trait (%s); %s stands down",
            HA_VERSION,
            err,
            NAME,
        )
        return True

    webhook.async_register(
        hass,
        DOMAIN,
        NAME,
        runtime.webhook_id,
        runtime.webhook_handler,
        allowed_methods=["POST"],
    )
    runtime.active = True
    ir.async_delete_issue(hass, DOMAIN, ISSUE_TRAIT_CHANGED)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_CORE_HAS_WEBRTC)
    _LOGGER.info("%s active on Home Assistant %s", NAME, HA_VERSION)

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
    elif runtime.sync_schema != SYNC_SCHEMA_VERSION:
        sync.request("sync schema changed")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: GoogleAssistantCamerasWebRTCConfigEntry
) -> bool:
    """Restore the stock trait, close the sessions and drop the webhook."""
    runtime = entry.runtime_data
    was_active = runtime.active
    if was_active:
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime.stop("unload")
    if was_active:
        webhook.async_unregister(hass, runtime.webhook_id)
    if runtime.patch is not None and patch.remove(runtime.patch):
        _LOGGER.info("Restored the stock Google Assistant camera trait")
    runtime.patch = None
    hass.data.pop(DATA_RUNTIME, None)
    await runtime.async_save()
    if was_active:
        runtime.sync.request("integration unloaded")
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget stored state and repairs; make sure Google learns about it."""
    if (sync := hass.data.pop(DATA_SYNC, None)) is not None:
        ok = await sync.async_run()
        sync.clear_issue()
        if not ok:
            persistent_notification.async_create(
                hass,
                f"{NAME} was removed but Home Assistant could not ask Google to re-sync. Say "
                '"Hey Google, sync my devices" so your cameras go back to the stock stream type.',
                title=NAME,
                notification_id=f"{DOMAIN}_removed",
            )
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_remove()


@callback
def async_stand_down(hass: HomeAssistant, runtime: Runtime, reason: str) -> None:
    """Deactivate at runtime: HA grew native WebRTC after we patched."""
    if not runtime.active:
        return
    runtime.stop("stand_down")
    webhook.async_unregister(hass, runtime.webhook_id)
    if runtime.patch is not None:
        patch.remove(runtime.patch)
        runtime.patch = None
    _stand_down_issue(hass, reason, "")
    _LOGGER.warning(
        "%s stood down (%s); Google Assistant keeps stock behaviour", NAME, reason
    )


def _stand_down_issue(hass: HomeAssistant, issue_id: str, detail: str) -> None:
    placeholders: dict[str, Any] = {"ha_version": HA_VERSION}
    if issue_id == ISSUE_TRAIT_CHANGED:
        placeholders["detail"] = detail or "unknown"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=issue_id,
        translation_placeholders=placeholders,
    )
