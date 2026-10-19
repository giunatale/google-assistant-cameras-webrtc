"""Diagnostics: patch state, Google link, cameras, breaker, recent sessions.

No URLs, hostnames, tokens or addresses are included.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from homeassistant.components import camera as camera_component
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .runtime import Runtime


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the entry."""
    runtime: Runtime | None = getattr(entry, "runtime_data", None)
    integration_version = None
    try:
        from homeassistant.loader import async_get_integration

        integration_version = str((await async_get_integration(hass, DOMAIN)).version)
    except Exception:
        pass

    data: dict[str, Any] = {
        "home_assistant": HA_VERSION,
        "integration_version": integration_version,
        "google_assistant_entries": len(
            hass.config_entries.async_entries("google_assistant")
        ),
        "google_assistant_request_sync_available": hass.services.has_service(
            "google_assistant", "request_sync"
        ),
        "cloud_loaded": "cloud" in hass.config.components,
        "go2rtc_loaded": "go2rtc" in hass.config.components,
    }
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
            "last_sync_seen": runtime.last_sync_seen,
            "last_sync_request": runtime.sync.last_request,
            "last_sync_result": runtime.sync.last_result,
            "counters": {**runtime.counters, **runtime.sessions.counters},
            "cameras": _cameras(hass, runtime),
            "breaker": runtime.breaker.as_dict(),
            "open_sessions": [
                s.as_record() for s in runtime.sessions.sessions.values()
            ],
            "recent_sessions": list(runtime.sessions.history),
        }
    )
    return data


def _cameras(hass: HomeAssistant, runtime: Runtime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for state in hass.states.async_all(camera_component.DOMAIN):
        camera = runtime.camera(state.entity_id)
        verdict = runtime.eligibility(state.entity_id)
        out.append(
            {
                "entity_id": state.entity_id,
                "webrtc_capable": runtime.webrtc_capable(camera),
                "provider": getattr(
                    getattr(camera, "webrtc_provider", None), "domain", None
                ),
                "offered_webrtc": verdict.ok,
                "reason": verdict.reason or None,
            }
        )
    return out
