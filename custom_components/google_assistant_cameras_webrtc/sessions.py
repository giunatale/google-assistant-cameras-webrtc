"""WebRTC signalling sessions.

Every GetCameraStream answered with WebRTC creates one session: a fresh
secret (256 bit) that is both the webhook id of the signalling URL and the
``cameraStreamAuthToken`` Google hands to the display, bound to one camera,
valid for one offer window and closed on ``end``, expiry or unload.

Why a webhook: ``/api/webhook/<id>`` is Home Assistant's public, unauthenticated,
CORS-enabled endpoint (any origin, ``Authorization`` allowed in preflight),
which is exactly what the player page on https://www.gstatic.com needs. No
HTTP internals are touched and registration is symmetric on reload.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
import logging
import secrets
from typing import TYPE_CHECKING, Any
import uuid

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    MAX_SESSION_HISTORY,
    MAX_SESSIONS_PER_CAMERA,
    MAX_SESSIONS_TOTAL,
    NAME,
    OFFER_WINDOW,
    SESSION_MAX_LIFETIME,
    TOMBSTONE_TTL,
)

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera

_LOGGER = logging.getLogger(__name__)

WebhookHandler = Callable[
    [HomeAssistant, str, web.Request], Coroutine[Any, Any, web.Response | None]
]


@dataclass
class Session:
    """One signalling session."""

    entity_id: str
    token: str
    created: datetime
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    offer_received: datetime | None = None
    answered: datetime | None = None
    closed: datetime | None = None
    outcome: str = "pending"
    provider_open: bool = False
    offer_addresses: set[str] = field(default_factory=set)
    offer_ufrag: str | None = None
    answer_ms: int | None = None
    gather_ms: int | None = None
    candidates: int = 0
    candidate_types: dict[str, int] = field(default_factory=dict)
    video_codecs: tuple[str, ...] = ()
    audio_codecs: tuple[str, ...] = ()
    auth_header_seen: bool | None = None
    origin_seen: bool | None = None
    media_check: str | None = None
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    @property
    def short(self) -> str:
        """Return a short, non-secret id for logs."""
        return self.session_id[:8]

    @property
    def webhook_id(self) -> str:
        """Return the webhook id (the session secret)."""
        return self.token

    def offer_expired(self, now: datetime) -> bool:
        """Return True if the offer window has passed."""
        return now - self.created > OFFER_WINDOW

    def lifetime_expired(self, now: datetime) -> bool:
        """Return True if the session is past its maximum lifetime."""
        return now - self.created > SESSION_MAX_LIFETIME

    def as_record(self) -> dict[str, Any]:
        """Return a diagnostics record (no secrets, no addresses)."""
        return {
            "id": self.short,
            "entity_id": self.entity_id,
            "created": self.created.isoformat(),
            "offer_received": self.offer_received.isoformat()
            if self.offer_received
            else None,
            "answered": self.answered.isoformat() if self.answered else None,
            "closed": self.closed.isoformat() if self.closed else None,
            "outcome": self.outcome,
            "answer_ms": self.answer_ms,
            "gather_ms": self.gather_ms,
            "candidates": self.candidates,
            "candidate_types": self.candidate_types,
            "video_codecs": list(self.video_codecs),
            "audio_codecs": list(self.audio_codecs),
            "auth_header_seen": self.auth_header_seen,
            "origin_seen": self.origin_seen,
            "media_check": self.media_check,
        }


class SessionManager:
    """Create, look up and close sessions."""

    def __init__(
        self,
        hass: HomeAssistant,
        handler: WebhookHandler,
        camera_lookup: Callable[[str], Camera | None],
    ) -> None:
        """Initialise."""
        self._hass = hass
        self._handler = handler
        self._camera_lookup = camera_lookup
        self.sessions: dict[str, Session] = {}
        self.tombstones: dict[str, datetime] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=MAX_SESSION_HISTORY)
        self.counters: dict[str, int] = {
            "created": 0,
            "evicted": 0,
            "closed_end": 0,
            "closed_expired": 0,
            "closed_no_offer": 0,
            "closed_unload": 0,
        }

    @callback
    def create(self, entity_id: str) -> Session:
        """Create a session for a camera and register its webhook."""
        self._enforce_caps(entity_id)
        token = secrets.token_hex(32)
        session = Session(entity_id=entity_id, token=token, created=dt_util.utcnow())
        webhook.async_register(
            self._hass,
            DOMAIN,
            f"{NAME} session {session.short}",
            session.webhook_id,
            self._handler,
            allowed_methods=["POST"],
        )
        self.sessions[token] = session
        self.counters["created"] += 1
        _LOGGER.debug("session %s: created for %s", session.short, entity_id)
        return session

    def get(self, webhook_id: str) -> Session | None:
        """Return the session owning a webhook id."""
        return self.sessions.get(webhook_id)

    def for_camera(self, entity_id: str) -> list[Session]:
        """Return the open sessions of a camera."""
        return [s for s in self.sessions.values() if s.entity_id == entity_id]

    @callback
    def close(self, session: Session, reason: str) -> None:
        """Close a session: provider session, webhook, tasks."""
        if session.token not in self.sessions:
            return
        self.sessions.pop(session.token, None)
        session.closed = dt_util.utcnow()
        if session.outcome in ("pending", "answered"):
            session.outcome = reason
        for task in session.tasks:
            if not task.done():
                task.cancel()
        session.tasks.clear()
        self.close_provider(session)
        self.tombstones[session.webhook_id] = session.closed
        self.history.append(session.as_record())
        self.counters[f"closed_{reason}"] = self.counters.get(f"closed_{reason}", 0) + 1
        _LOGGER.debug("session %s: closed (%s)", session.short, reason)

    @callback
    def close_provider(self, session: Session) -> None:
        """Close the provider (go2rtc) side of a session if open."""
        if not session.provider_open:
            return
        session.provider_open = False
        camera = self._camera_lookup(session.entity_id)
        if camera is None:
            return
        try:
            camera.close_webrtc_session(session.session_id)
        except Exception:
            _LOGGER.debug(
                "session %s: provider refused to close the session", session.short
            )

    @callback
    def close_all(self, reason: str) -> None:
        """Close every session and drop every webhook (unload)."""
        for session in list(self.sessions.values()):
            self.close(session, reason)
        for webhook_id in list(self.tombstones):
            self._unregister(webhook_id)

    @callback
    def _unregister(self, webhook_id: str) -> None:
        self.tombstones.pop(webhook_id, None)
        try:
            webhook.async_unregister(self._hass, webhook_id)
        except Exception:
            _LOGGER.debug("webhook of a closed session was already gone")

    @callback
    def sweep(self) -> None:
        """Close sessions that expired without an offer or outlived their lifetime."""
        now = dt_util.utcnow()
        for webhook_id, closed in list(self.tombstones.items()):
            if now - closed > TOMBSTONE_TTL:
                self._unregister(webhook_id)
        for session in list(self.sessions.values()):
            if session.lifetime_expired(now):
                self.close(session, "expired")
            elif session.offer_received is None and session.offer_expired(now):
                _LOGGER.debug(
                    "session %s: no offer arrived for %s within the offer window",
                    session.short,
                    session.entity_id,
                )
                self.close(session, "no_offer")

    def _enforce_caps(self, entity_id: str) -> None:
        """Evict the oldest sessions when a cap would be exceeded."""
        same = sorted(self.for_camera(entity_id), key=lambda s: s.created)
        while len(same) >= MAX_SESSIONS_PER_CAMERA:
            self.counters["evicted"] += 1
            self.close(same.pop(0), "evicted")
        everything = sorted(self.sessions.values(), key=lambda s: s.created)
        while len(everything) >= MAX_SESSIONS_TOTAL:
            self.counters["evicted"] += 1
            self.close(everything.pop(0), "evicted")
