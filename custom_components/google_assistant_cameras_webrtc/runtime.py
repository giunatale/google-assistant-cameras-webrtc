"""Runtime state and the decisions taken at SYNC and EXECUTE time."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.components.camera.const import StreamType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .breaker import Breaker
from .const import (
    ATTR_SUPPORTED_PROTOCOLS,
    DOMAIN,
    ISSUE_CAMERA_UNSUPPORTED_PREFIX,
    ISSUE_NO_HTTPS,
    MEDIA_CHECK_DELAYS,
    PARAM_STREAM_TO_CHROMECAST,
    PARAM_SUPPORTED_PROTOCOLS,
    PROTOCOL_HLS,
    PROTOCOL_WEBRTC,
    RESULT_ACCESS_URL,
    RESULT_AUTH_TOKEN,
    RESULT_ICE_SERVERS,
    RESULT_PROTOCOL,
    RESULT_SIGNALING_URL,
)
from .health import async_check_media_flow
from .patch import PatchHandle
from .sessions import Session, SessionManager
from .signaling import NegotiationError, async_handle_webhook
from .sync import SyncRequester

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera

_LOGGER = logging.getLogger(__name__)

CAMERA_DOMAIN = "camera"


@dataclass
class Eligibility:
    """Why a camera is (not) offered over WebRTC."""

    ok: bool
    reason: str = ""


@dataclass
class Runtime:
    """Everything the wrapped trait methods need, keyed in hass.data."""

    hass: HomeAssistant
    entry: ConfigEntry
    sync: SyncRequester
    breaker: Breaker
    store: Store[dict[str, Any]]
    patch: PatchHandle | None = None
    stand_down: Callable[[str], None] | None = None
    active: bool = False
    synced: bool = False
    https_issue_open: bool = False
    last_sync_seen: str | None = None
    counters: dict[str, int] = field(default_factory=dict)
    sessions: SessionManager = field(init=False)
    _expiry_unsub: CALLBACK_TYPE | None = None
    _save_task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        """Create the session manager."""
        self.sessions = SessionManager(self.hass, self._webhook_handler, self.camera)
        self.breaker.now = dt_util.utcnow
        for key in (
            "sync_seen",
            "sync_webrtc_offered",
            "execute_webrtc",
            "execute_hls",
            "negotiations_ok",
            "negotiations_failed",
            "media_flowing",
            "media_not_flowing",
            "media_unknown",
        ):
            self.counters.setdefault(key, 0)

    # ----------------------------------------------------------------- helpers

    def camera(self, entity_id: str) -> Camera | None:
        """Return the camera entity object for an entity id."""
        component = self.hass.data.get(CAMERA_DOMAIN)
        get_entity = getattr(component, "get_entity", None)
        if get_entity is None:
            return None
        return get_entity(entity_id)

    def base_url(self) -> str | None:
        """Return the URL HA hands to Google (same call as stock HLS)."""
        try:
            return get_url(self.hass)
        except NoURLAvailableError:
            return None

    def webrtc_capable(self, camera: Camera | None) -> bool:
        """Return True if HA can serve this camera over WebRTC."""
        if camera is None:
            return False
        try:
            return (
                StreamType.WEB_RTC in camera.camera_capabilities.frontend_stream_types
            )
        except Exception:
            return False

    def eligibility(
        self, entity_id: str, *, consult_breaker: bool = True
    ) -> Eligibility:
        """Evaluate a camera (cheap, no I/O; safe inside EXECUTE's 2 s limit)."""
        camera = self.camera(entity_id)
        if camera is None:
            return Eligibility(False, "camera entity not found")
        if not self.webrtc_capable(camera):
            return Eligibility(False, "camera has no WebRTC in HA")
        url = self.base_url()
        if url is None:
            return Eligibility(False, "HA has no URL to hand to Google")
        scheme = urlsplit(url).scheme.lower()
        if scheme != "https":
            self._set_https_issue(scheme)
            return Eligibility(False, f"HA's URL for Google is {scheme}, not https")
        self._clear_https_issue()
        if consult_breaker:
            allowed, why = self.breaker.allows(entity_id)
            if not allowed:
                return Eligibility(False, why)
        return Eligibility(True)

    # --------------------------------------------------------- trait decisions

    def decorate_sync_attributes(
        self, entity_id: str, attrs: dict[str, Any]
    ) -> dict[str, Any]:
        """Extend the stock SYNC attributes for an eligible camera."""
        self.counters["sync_seen"] += 1
        self.last_sync_seen = dt_util.utcnow().isoformat()
        self.sync.clear_issue()  # a SYNC is happening: Google has the new list
        protocols = attrs.get(ATTR_SUPPORTED_PROTOCOLS)
        if not isinstance(protocols, list):
            return attrs
        if PROTOCOL_WEBRTC in protocols:
            # HA grew native support since we patched: hands off.
            self.hass.loop.call_soon(self._stand_down_core_has_webrtc)
            return attrs
        verdict = self.eligibility(entity_id)
        if not verdict.ok:
            _LOGGER.debug(
                "SYNC %s: stock protocols %s (%s)", entity_id, protocols, verdict.reason
            )
            return attrs
        self.counters["sync_webrtc_offered"] += 1
        new_protocols = [PROTOCOL_WEBRTC, *protocols]
        _LOGGER.debug("SYNC %s: advertising %s", entity_id, new_protocols)
        return {**attrs, ATTR_SUPPORTED_PROTOCOLS: new_protocols}

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
            RESULT_SIGNALING_URL: f"{base.rstrip('/')}{webhook.async_generate_path(session.webhook_id)}",
            RESULT_AUTH_TOKEN: session.token,
        }
        ice = self._ice_servers_for_google(entity_id)
        if ice:
            result[RESULT_ICE_SERVERS] = json.dumps(ice)
        _LOGGER.debug(
            "EXECUTE %s: target wants %s (StreamToChromecast=%s) -> WebRTC session %s "
            "(signalling via webhook, %d STUN server(s) passed)",
            entity_id,
            wanted,
            to_cast,
            session.short,
            len(ice),
        )
        return result

    def decorate_hls_result(
        self, entity_id: str, params: Any, stream_info: dict[str, str] | None
    ) -> dict[str, str] | None:
        """Add cameraStreamProtocol to a stock HLS result of a WebRTC camera.

        Google requires the field once a device advertises two protocols; stock
        HA omits it because it only ever advertised one. Cameras we never
        advertise WebRTC for keep the stock result untouched.
        """
        if not isinstance(stream_info, dict) or RESULT_PROTOCOL in stream_info:
            return stream_info
        if RESULT_ACCESS_URL not in stream_info:
            return stream_info
        if not self.webrtc_capable(self.camera(entity_id)):
            return stream_info
        return {**stream_info, RESULT_PROTOCOL: PROTOCOL_HLS}

    def _ice_servers_for_google(self, entity_id: str) -> list[dict[str, Any]]:
        """Return HA's STUN servers for the display (no TURN credentials leave HA)."""
        camera = self.camera(entity_id)
        if camera is None:
            return []
        try:
            config = camera.async_get_webrtc_client_configuration()
            servers = config.configuration.ice_servers
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for server in servers:
            urls = server.urls if isinstance(server.urls, list) else [server.urls]
            stun = [
                u for u in urls if isinstance(u, str) and u.lower().startswith("stun:")
            ]
            if stun:
                out.append({"urls": stun})
        return out

    # ------------------------------------------------------------- signalling

    async def _webhook_handler(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        return await async_handle_webhook(self, hass, webhook_id, request)

    @callback
    def record_negotiation_failure(self, entity_id: str, err: NegotiationError) -> None:
        """Feed a failed exchange to the breaker."""
        self.counters["negotiations_failed"] += 1
        if err.permanent:
            if self.breaker.mark_unsupported(entity_id, err.reason):
                _LOGGER.warning(
                    "%s: WebRTC disabled for Google Assistant (%s); HLS only until retried "
                    "from the repair issue",
                    entity_id,
                    err.reason,
                )
                self._camera_issue(entity_id, err.reason)
                self._on_breaker_change("camera unsupported")
            return
        self._failure(entity_id, err.reason)

    @callback
    def _failure(self, entity_id: str, reason: str) -> None:
        if self.breaker.record_failure(entity_id, reason):
            health = self.breaker.health(entity_id)
            _LOGGER.warning(
                "%s: WebRTC for Google Assistant tripped to HLS until %s after repeated "
                "failures (%s)",
                entity_id,
                health.until,
                reason,
            )
            self._on_breaker_change("camera tripped")
        else:
            self._schedule_save()

    @callback
    def _success(self, entity_id: str) -> None:
        self.counters["negotiations_ok"] += 1
        if self.breaker.record_success(entity_id):
            _LOGGER.info("%s: WebRTC for Google Assistant working again", entity_id)
            self._on_breaker_change("camera recovered")
        else:
            self._schedule_save()

    @callback
    def schedule_media_check(self, session: Session) -> None:
        """Verify (through go2rtc) that media actually flows for a session."""
        session.tasks.append(
            self.hass.async_create_background_task(
                self._media_check(session), f"{DOMAIN} media check {session.short}"
            )
        )

    async def _media_check(self, session: Session) -> None:
        elapsed = 0.0
        verdict: bool | None = None
        detail = ""
        for delay in MEDIA_CHECK_DELAYS:
            await asyncio.sleep(max(delay - elapsed, 0))
            elapsed = delay
            if session.token not in self.sessions.sessions:
                return  # ended meanwhile; nothing to judge
            camera = self.camera(session.entity_id)
            if camera is None:
                return
            verdict, detail = await async_check_media_flow(
                self.hass, camera, session.offer_addresses
            )
            if verdict is not False:
                break
        session.media_check = f"{verdict}: {detail}"
        if verdict is None:
            self.counters["media_unknown"] += 1
            _LOGGER.debug("session %s: media flow unknown (%s)", session.short, detail)
            return
        if verdict:
            self.counters["media_flowing"] += 1
            _LOGGER.debug(
                "session %s: media flowing to the display (%s)", session.short, detail
            )
            self._success(session.entity_id)
            return
        self.counters["media_not_flowing"] += 1
        _LOGGER.warning(
            "session %s: no media reached the display for %s within %.0f s (%s); "
            "the display probably cannot reach the ICE candidates",
            session.short,
            session.entity_id,
            MEDIA_CHECK_DELAYS[-1],
            detail,
        )
        self._failure(session.entity_id, f"no media flow: {detail}")

    # --------------------------------------------------------------- breaker

    @callback
    def _on_breaker_change(self, reason: str) -> None:
        self._schedule_save()
        self.sync.request(reason)
        self._arm_expiry_timer()

    @callback
    def _arm_expiry_timer(self) -> None:
        if self._expiry_unsub is not None:
            self._expiry_unsub()
            self._expiry_unsub = None
        expiry = self.breaker.next_expiry()
        if expiry is None:
            return
        delay = max((expiry - dt_util.utcnow()).total_seconds(), 1.0)
        self._expiry_unsub = async_call_later(
            self.hass,
            delay,
            HassJob(
                self._on_trip_expired, f"{DOMAIN} trip expiry", cancel_on_shutdown=True
            ),
        )

    @callback
    def _on_trip_expired(self, _now: Any) -> None:
        self._expiry_unsub = None
        cleared = self.breaker.expired_trips()
        if cleared:
            _LOGGER.info(
                "WebRTC re-enabled for Google Assistant: %s", ", ".join(cleared)
            )
            self._on_breaker_change("trip expired")
        else:
            self._arm_expiry_timer()

    @callback
    def retry_camera(self, entity_id: str) -> None:
        """User asked (repair flow) to try WebRTC again for a camera."""
        if self.breaker.reset(entity_id):
            _LOGGER.info(
                "%s: WebRTC for Google Assistant re-enabled by the user", entity_id
            )
            self._on_breaker_change("user retry")
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{entity_id}"
        )

    # ---------------------------------------------------------------- issues

    def _camera_issue(self, entity_id: str, reason: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{entity_id}",
            is_fixable=True,
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
        if not self.https_issue_open:
            return
        self.https_issue_open = False
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_HTTPS)

    @callback
    def _stand_down_core_has_webrtc(self) -> None:
        if not self.active or self.stand_down is None:
            return
        self.stand_down("core_has_webrtc")

    # ----------------------------------------------------------- persistence

    async def async_load(self) -> None:
        """Restore breaker state and the synced flag."""
        data = await self.store.async_load() or {}
        self.synced = bool(data.get("synced"))
        cameras = data.get("cameras")
        if isinstance(cameras, dict):
            self.breaker.load(cameras)
        for entity_id, health in self.breaker.cameras.items():
            if health.state == "unsupported" and health.reason:
                self._camera_issue(entity_id, health.reason)
        self._arm_expiry_timer()

    def _data(self) -> dict[str, Any]:
        return {"synced": self.synced, "cameras": self.breaker.as_dict()}

    @callback
    def _schedule_save(self) -> None:
        self.store.async_delay_save(self._data, 2)

    async def async_save(self) -> None:
        """Persist now."""
        await self.store.async_save(self._data())

    @callback
    def mark_synced(self, ok: bool) -> None:
        """Remember that Google received our SYNC at least once."""
        if ok and not self.synced:
            self.synced = True
            self._schedule_save()

    # -------------------------------------------------------------- shutdown

    @callback
    def stop(self, reason: str) -> None:
        """Close sessions and timers."""
        self.active = False
        if self._expiry_unsub is not None:
            self._expiry_unsub()
            self._expiry_unsub = None
        self.sessions.close_all(reason)
