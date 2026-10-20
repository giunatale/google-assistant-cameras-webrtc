"""Diagnostics: patch state, Google link, cameras, recent sessions. No URLs, tokens or addresses."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from homeassistant.components import camera as camera_component
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .runtime import Runtime


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the entry."""
    integration = await async_get_integration(hass, DOMAIN)
    data: dict[str, Any] = {
        "home_assistant": HA_VERSION,
        "integration_version": str(integration.version),
        "google_assistant_entries": len(
            hass.config_entries.async_entries("google_assistant")
        ),
        "google_assistant_request_sync_available": hass.services.has_service(
            "google_assistant", "request_sync"
        ),
        "cloud_loaded": "cloud" in hass.config.components,
        "go2rtc_loaded": "go2rtc" in hass.config.components,
    }
    runtime: Runtime | None = getattr(entry, "runtime_data", None)
    if runtime is None:
        data["state"] = "not loaded"
        return data
    base = runtime.base_url()
    data.update(
        {
            "state": "active" if runtime.active else "inactive",
            "patch_in_place": bool(runtime.patch and runtime.patch.active),
            "google_url_scheme": urlsplit(base).scheme if base else None,
            "synced_once": runtime.synced,
            "sync_schema": runtime.sync_schema,
            "last_sync_seen": runtime.last_sync_seen,
            "last_sync_request": runtime.sync.last_request,
            "last_sync_result": runtime.sync.last_result,
            "counters": {**runtime.counters, **runtime.sessions.counters},
            "cameras": [
                {
                    "entity_id": state.entity_id,
                    "webrtc_capable": runtime.webrtc_capable(
                        camera := runtime.camera(state.entity_id)
                    ),
                    "provider": getattr(
                        getattr(camera, "webrtc_provider", None), "domain", None
                    ),
                    "offered_webrtc": (
                        verdict := runtime.eligibility(state.entity_id)
                    ).ok,
                    "reason": verdict.reason or None,
                }
                for state in hass.states.async_all(camera_component.DOMAIN)
            ],
            "disabled": runtime.disabled,
            "open_sessions": [
                s.as_record() for s in runtime.sessions.sessions.values()
            ],
            "recent_sessions": list(runtime.sessions.history),
        }
    )
    return data
