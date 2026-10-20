"""Runtime state and the decisions taken at SYNC and EXECUTE time."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.components.camera.const import StreamType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_NEED_AUTH_TOKEN,
    ATTR_SUPPORTED_PROTOCOLS,
    CONF_WEBHOOK_ID,
    DOMAIN,
    ISSUE_CAMERA_UNSUPPORTED_PREFIX,
    ISSUE_NO_HTTPS,
    MEDIA_CHECK_DELAY,
    PARAM_STREAM_TO_CHROMECAST,
    PARAM_SUPPORTED_PROTOCOLS,
    PROTOCOL_HLS,
    PROTOCOL_WEBRTC,
    REASON_USER,
    RESULT_ACCESS_URL,
    RESULT_AUTH_TOKEN,
    RESULT_ICE_SERVERS,
    RESULT_PROTOCOL,
    RESULT_SIGNALING_URL,
    SIGNAL_CAMERA_SYNCED,
    SIGNAL_SWITCH_STATE,
)
from .go2rtc import async_stream_state, describe
from .patch import PatchHandle
from .sessions import Session, SessionManager
from .signaling import async_handle_webhook
from .sync import SyncRequester

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera

_LOGGER = logging.getLogger(__name__)

CAMERA_DOMAIN = "camera"


@dataclass
class Eligibility:
    """Whether a camera is offered over WebRTC, and why not otherwise."""

    ok: bool
    reason: str = ""


@dataclass
class Runtime:
    """Everything the wrapped trait methods need, kept in hass.data."""

    hass: HomeAssistant
    entry: ConfigEntry
    sync: SyncRequester
    store: Store[dict[str, Any]]
    patch: PatchHandle | None = None
    stand_down: Callable[[str], None] | None = None
    active: bool = False
    synced: bool = False
    sync_schema: int | None = None
    disabled: dict[str, str] = field(default_factory=dict)
    https_issue_open: bool = False
    last_sync_seen: str | None = None
    counters: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("sync_seen", "sync_webrtc_offered", "execute_webrtc", "execute_hls"), 0
        )
    )
    sessions: SessionManager = field(init=False)

    def __post_init__(self) -> None:
        """Create the session manager."""
        self.sessions = SessionManager(self.hass, self.camera)

    # Lookups

    def camera(self, entity_id: str) -> Camera | None:
        """Return the camera entity for an entity id."""
        get_entity = getattr(self.hass.data.get(CAMERA_DOMAIN), "get_entity", None)
        return get_entity(entity_id) if get_entity else None

    @property
    def webhook_id(self) -> str:
        """Return the id of the signalling webhook."""
        return str(self.entry.data[CONF_WEBHOOK_ID])

    def base_url(self) -> str | None:
        """Return the URL HA hands to Google (the same call stock HLS uses)."""
        try:
            return get_url(self.hass)
        except NoURLAvailableError:
            return None

    def webrtc_capable(self, camera: Camera | None) -> bool:
        """Return True if HA can serve the camera over WebRTC."""
        try:
            return camera is not None and (
                StreamType.WEB_RTC in camera.camera_capabilities.frontend_stream_types
            )
        except Exception:
            return False

    def eligibility(self, entity_id: str) -> Eligibility:
        """Evaluate a camera without I/O (EXECUTE must finish within 2 s)."""
        camera = self.camera(entity_id)
        if camera is None:
            return Eligibility(False, "camera entity not found")
        if not self.webrtc_capable(camera):
            return Eligibility(False, "camera has no WebRTC in HA")
        if (url := self.base_url()) is None:
            return Eligibility(False, "HA has no URL to hand to Google")
        if (scheme := urlsplit(url).scheme.lower()) != "https":
            self._set_https_issue(scheme)
            return Eligibility(False, f"HA's URL for Google is {scheme}, not https")
        self._clear_https_issue()
        if (reason := self.disabled.get(entity_id)) is not None:
            return Eligibility(False, f"WebRTC switched off: {reason}")
        return Eligibility(True)

    def google_exposes(self, entity_id: str) -> bool:
        """Return True if a Google Assistant link exposes the entity (True when unknown)."""
        configs: list[Any] = []
        for entry in self.hass.config_entries.async_entries("google_assistant"):
            if hasattr(getattr(entry, "runtime_data", None), "should_expose"):
                configs.append(entry.runtime_data)
        cloud = getattr(
            getattr(self.hass.data.get("cloud"), "client", None), "_google_config", None
        )
        if hasattr(cloud, "should_expose"):
            configs.append(cloud)
        if not configs:
            return True
        return any(config.should_expose(entity_id) for config in configs)

    def switch_wanted(self, entity_id: str) -> bool:
        """Return True if the camera gets a 'WebRTC for Google Assistant' switch."""
        return self.webrtc_capable(self.camera(entity_id)) and self.google_exposes(
            entity_id
        )

    # Trait decisions

    def decorate_sync_attributes(
        self, entity_id: str, attrs: dict[str, Any]
    ) -> dict[str, Any]:
        """Advertise WebRTC first for an eligible camera.

        ``cameraStreamNeedAuthToken`` is set because Google's players only
        negotiate WebRTC reliably with it.
        """
        self.counters["sync_seen"] += 1
        self.last_sync_seen = dt_util.utcnow().isoformat()
        self.sync.clear_issue()
        protocols = attrs.get(ATTR_SUPPORTED_PROTOCOLS)
        if not isinstance(protocols, list):
            return attrs
        if PROTOCOL_WEBRTC in protocols:
            self.hass.loop.call_soon(self._stand_down_core_has_webrtc)
            return attrs
        async_dispatcher_send(self.hass, SIGNAL_CAMERA_SYNCED, entity_id)
        verdict = self.eligibility(entity_id)
        if not verdict.ok:
            _LOGGER.debug(
                "SYNC %s: stock protocols %s (%s)", entity_id, protocols, verdict.reason
            )
            return attrs
        self.counters["sync_webrtc_offered"] += 1
        new_protocols = [PROTOCOL_WEBRTC, *protocols]
        _LOGGER.debug("SYNC %s: advertising %s", entity_id, new_protocols)
        return {
            **attrs,
            ATTR_SUPPORTED_PROTOCOLS: new_protocols,
            ATTR_NEED_AUTH_TOKEN: True,
        }

    def webrtc_execute(self, entity_id: str, params: Any) -> dict[str, str] | None:
        """Return the WebRTC EXECUTE result, or None for stock HLS."""
        wanted = (
            params.get(PARAM_SUPPORTED_PROTOCOLS) if isinstance(params, dict) else None
        )
        to_cast = (
            params.get(PARAM_STREAM_TO_CHROMECAST) if isinstance(params, dict) else None
        )
        if not isinstance(wanted, list) or PROTOCOL_WEBRTC not in wanted:
            self.counters["execute_hls"] += 1
            _LOGGER.debug(
                "EXECUTE %s: target wants %s (StreamToChromecast=%s) -> stock HLS",
                entity_id,
                wanted,
                to_cast,
            )
            return None
        verdict = self.eligibility(entity_id)
        if not verdict.ok:
            self.counters["execute_hls"] += 1
            _LOGGER.debug(
                "EXECUTE %s: target wants %s but WebRTC is not available (%s) -> stock HLS",
                entity_id,
                wanted,
                verdict.reason,
            )
            if PROTOCOL_HLS not in wanted:
                _LOGGER.warning(
                    "EXECUTE %s: Google asked for WebRTC only but it is unavailable (%s); "
                    "Google holds a stale device list, requesting a sync",
                    entity_id,
                    verdict.reason,
                )
                self.sync.request("stale SYNC")
            return None
        base = self.base_url()
        assert base is not None
        session = self.sessions.create(entity_id)
        self.counters["execute_webrtc"] += 1
        result = {
            RESULT_PROTOCOL: PROTOCOL_WEBRTC,
            RESULT_SIGNALING_URL: f"{base.rstrip('/')}{webhook.async_generate_path(self.webhook_id)}",
            RESULT_AUTH_TOKEN: session.token,
        }
        if ice := self._stun_servers(entity_id):
            result[RESULT_ICE_SERVERS] = json.dumps(ice)
        _LOGGER.debug(
            "EXECUTE %s: target wants %s (StreamToChromecast=%s) -> WebRTC session %s "
            "(%d STUN server(s) passed)",
            entity_id,
            wanted,
            to_cast,
            session.short,
            len(ice),
        )
        return result

    def decorate_hls_result(
        self, entity_id: str, stream_info: dict[str, str] | None
    ) -> dict[str, str] | None:
        """Complete a stock HLS result for a camera that may be advertised with WebRTC.

        Google requires ``cameraStreamProtocol`` once a device lists two
        protocols and ``cameraStreamAuthToken`` once it needs auth tokens. The
        camera is judged by capability alone, since Google may still hold an
        earlier SYNC.
        """
        if not isinstance(stream_info, dict) or RESULT_PROTOCOL in stream_info:
            return stream_info
        if RESULT_ACCESS_URL not in stream_info or not self.webrtc_capable(
            self.camera(entity_id)
        ):
            return stream_info
        return {
            **stream_info,
            RESULT_PROTOCOL: PROTOCOL_HLS,
            RESULT_AUTH_TOKEN: secrets.token_hex(32),
        }

    def _stun_servers(self, entity_id: str) -> list[dict[str, Any]]:
        """Return HA's STUN servers for the display; TURN credentials never leave HA."""
        camera = self.camera(entity_id)
        if camera is None:
            return []
        try:
            servers = (
                camera.async_get_webrtc_client_configuration().configuration.ice_servers
            )
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for server in servers:
            urls = server.urls if isinstance(server.urls, list) else [server.urls]
            if stun := [
                u for u in urls if isinstance(u, str) and u.lower().startswith("stun:")
            ]:
                out.append({"urls": stun})
        return out

    # Signalling

    async def webhook_handler(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Dispatch the webhook to the signalling handler."""
        return await async_handle_webhook(self, request)

    @callback
    def disable(self, entity_id: str, reason: str) -> None:
        """Stop advertising WebRTC for a camera; a repair explains it unless the user did it."""
        if self.disabled.get(entity_id) == reason:
            return
        self.disabled[entity_id] = reason
        if reason == REASON_USER:
            _LOGGER.info("%s: WebRTC for Google Assistant switched off", entity_id)
            ir.async_delete_issue(
                self.hass, DOMAIN, f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{entity_id}"
            )
        else:
            _LOGGER.warning(
                "%s: WebRTC switched off for Google Assistant (%s); turn the switch on to try again",
                entity_id,
                reason,
            )
            self._camera_issue(entity_id, reason)
        self._switched(entity_id, "camera disabled")

    @callback
    def enable(self, entity_id: str) -> None:
        """Advertise WebRTC for a camera again."""
        if self.disabled.pop(entity_id, None) is None:
            return
        _LOGGER.info("%s: WebRTC for Google Assistant switched on", entity_id)
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{entity_id}"
        )
        self._switched(entity_id, "camera enabled")

    def _switched(self, entity_id: str, reason: str) -> None:
        self._schedule_save()
        self.sync.request(reason)
        async_dispatcher_send(self.hass, SIGNAL_SWITCH_STATE, entity_id)

    @callback
    def schedule_media_check(self, session: Session) -> None:
        """Log, a few seconds after the answer, whether media reaches the display."""
        session.tasks.append(
            self.hass.async_create_background_task(
                self._media_check(session), f"{DOMAIN} media check {session.short}"
            )
        )

    async def _media_check(self, session: Session) -> None:
        await asyncio.sleep(MEDIA_CHECK_DELAY)
        if session.key not in self.sessions.sessions:
            return
        if (camera := self.camera(session.entity_id)) is None:
            return
        state = await async_stream_state(self.hass, camera)
        session.media_check = describe(state, session.offer_addresses)
        if state is not None and state.camera_h264 and not session.camera_h264:
            session.camera_h264 = state.camera_h264
            _LOGGER.debug(
                "session %s: camera stream: H264 %s", session.short, state.camera_h264
            )
        _LOGGER.debug(
            "session %s: media check at +%.0f s: %s",
            session.short,
            MEDIA_CHECK_DELAY,
            session.media_check,
        )

    async def log_stream_state(self, session: Session) -> None:
        """Log the go2rtc consumer state when the display ends a session."""
        if not _LOGGER.isEnabledFor(logging.DEBUG) or not session.active:
            return
        if (camera := self.camera(session.entity_id)) is None:
            return
        state = await async_stream_state(self.hass, camera)
        _LOGGER.debug(
            "session %s: end received for %s; go2rtc consumer: %s",
            session.short,
            session.entity_id,
            describe(state, session.offer_addresses),
        )

    # Repair issues

    def _camera_issue(self, entity_id: str, reason: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{entity_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="camera_unsupported",
            translation_placeholders={"entity_id": entity_id, "reason": reason},
        )

    def _set_https_issue(self, scheme: str) -> None:
        if self.https_issue_open:
            return
        self.https_issue_open = True
        _LOGGER.warning(
            "The URL HA hands to Google is %s://, not https://; WebRTC is not offered",
            scheme,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_NO_HTTPS,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NO_HTTPS,
            translation_placeholders={"scheme": scheme},
        )

    def _clear_https_issue(self) -> None:
        if self.https_issue_open:
            self.https_issue_open = False
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_HTTPS)

    @callback
    def _stand_down_core_has_webrtc(self) -> None:
        if self.active and self.stand_down is not None:
            self.stand_down("core_has_webrtc")

    # Persistence

    async def async_load(self) -> None:
        """Restore the switched-off cameras and the sync state."""
        data = await self.store.async_load() or {}
        self.synced = bool(data.get("synced"))
        self.sync_schema = (
            data.get("sync_schema")
            if isinstance(data.get("sync_schema"), int)
            else None
        )
        cameras = data.get("disabled")
        if isinstance(cameras, dict):
            self.disabled = {
                k: str(v) for k, v in cameras.items() if isinstance(k, str)
            }
        for entity_id, reason in self.disabled.items():
            if reason != REASON_USER:
                self._camera_issue(entity_id, reason)

    def _data(self) -> dict[str, Any]:
        return {
            "synced": self.synced,
            "sync_schema": self.sync_schema,
            "disabled": self.disabled,
        }

    @callback
    def _schedule_save(self) -> None:
        self.store.async_delay_save(self._data, 2)

    async def async_save(self) -> None:
        """Persist now."""
        await self.store.async_save(self._data())

    @callback
    def mark_synced(self, ok: bool, schema: int) -> None:
        """Record that Google accepted a sync of the current SYNC schema."""
        if ok and (not self.synced or self.sync_schema != schema):
            self.synced = True
            self.sync_schema = schema
            self._schedule_save()

    @callback
    def stop(self, reason: str) -> None:
        """Close every session."""
        self.active = False
        self.sessions.close_all(reason)
