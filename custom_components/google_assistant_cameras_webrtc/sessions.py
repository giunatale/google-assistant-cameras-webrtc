"""Signalling sessions.

Every WebRTC EXECUTE creates a session: a fresh 256-bit token handed to the
display as ``cameraStreamAuthToken``, bound to one camera. All sessions share
the one signalling URL; the bearer token selects the session and the camera.
Tokens of closed sessions stay resolvable for a while so a late ``end`` is
answered quietly and a late offer gets a plain refusal.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import logging
import secrets
from typing import TYPE_CHECKING, Any
import uuid

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    MAX_PENDING_PER_CAMERA,
    MAX_SESSION_HISTORY,
    MAX_SESSIONS_TOTAL,
    OFFER_WINDOW,
    SESSION_MAX_LIFETIME,
    TOKEN_GRACE,
)

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera

_LOGGER = logging.getLogger(__name__)


def token_key(token: str) -> str:
    """Return the index key of a token (its SHA-256)."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Session:
    """One signalling session."""

    entity_id: str
    token: str
    created: datetime
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    key: str = field(init=False)
    label: str = field(init=False)
    offer_received: datetime | None = None
    answered: datetime | None = None
    closed: datetime | None = None
    outcome: str = "pending"
    provider_open: bool = False
    offer_addresses: set[str] = field(default_factory=set)
    host_addresses: set[str] = field(default_factory=set)
    answer_ms: int | None = None
    gather_ms: int | None = None
    candidates: int = 0
    candidate_types: dict[str, int] = field(default_factory=dict)
    video_codecs: tuple[str, ...] = ()
    audio_codecs: tuple[str, ...] = ()
    display_h264_profiles: tuple[str, ...] = ()
    camera_h264: str | None = None
    origin_seen: bool | None = None
    media_check: str | None = None
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Index the session under its own token and fix its log label."""
        self.key = token_key(self.token)
        self.label = self.session_id[:8]

    @property
    def short(self) -> str:
        """Return a short, non-secret id for logs (fixed for the session's life)."""
        return self.label

    @property
    def active(self) -> bool:
        """Return True once an offer has been taken."""
        return self.offer_received is not None

    def offer_expired(self, now: datetime) -> bool:
        """Return True once the offer window has passed."""
        return now - self.created > OFFER_WINDOW

    def lifetime_expired(self, now: datetime) -> bool:
        """Return True once the maximum lifetime has passed."""
        return now - self.created > SESSION_MAX_LIFETIME

    def as_record(self) -> dict[str, Any]:
        """Return a diagnostics record (no secrets, no addresses)."""
        iso = lambda value: value.isoformat() if value else None  # noqa: E731
        return {
            "id": self.short,
            "entity_id": self.entity_id,
            "created": iso(self.created),
            "offer_received": iso(self.offer_received),
            "answered": iso(self.answered),
            "closed": iso(self.closed),
            "outcome": self.outcome,
            "answer_ms": self.answer_ms,
            "gather_ms": self.gather_ms,
            "candidates": self.candidates,
            "candidate_types": self.candidate_types,
            "video_codecs": list(self.video_codecs),
            "audio_codecs": list(self.audio_codecs),
            "display_h264_profiles": list(self.display_h264_profiles),
            "camera_h264": self.camera_h264,
            "origin_seen": self.origin_seen,
            "media_check": self.media_check,
        }


@dataclass
class ClosedToken:
    """A token whose session closed; it still ends quietly and is refused for offers."""

    entity_id: str
    short: str
    closed: datetime


class SessionManager:
    """Create, resolve and close sessions."""

    def __init__(
        self, hass: HomeAssistant, camera_lookup: Callable[[str], Camera | None]
    ) -> None:
        """Initialise."""
        self._hass = hass
        self._camera_lookup = camera_lookup
        self.sessions: dict[str, Session] = {}
        self._by_token: dict[str, Session] = {}
        self._closed_tokens: dict[str, ClosedToken] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=MAX_SESSION_HISTORY)
        self.counters: dict[str, int] = {"created": 0, "evicted": 0}

    @callback
    def create(self, entity_id: str) -> Session:
        """Create a session, evicting old pending ones when a cap is reached."""
        self._evict_pending(self.for_camera(entity_id), MAX_PENDING_PER_CAMERA)
        self._evict_pending(list(self.sessions.values()), MAX_SESSIONS_TOTAL)
        session = Session(entity_id, secrets.token_hex(32), dt_util.utcnow())
        self.sessions[session.key] = session
        self._by_token[session.key] = session
        self.counters["created"] += 1
        _LOGGER.debug("session %s: created for %s", session.short, entity_id)
        return session

    def resolve(self, token: str) -> Session | ClosedToken | None:
        """Return the live session or the closed-token record a bearer selects."""
        key = token_key(token)
        if (session := self._by_token.get(key)) is not None:
            if key == session.key and not hmac.compare_digest(session.token, token):
                return None
            return session
        self._expire_closed_tokens()
        return self._closed_tokens.get(key)

    def for_camera(self, entity_id: str) -> list[Session]:
        """Return the live sessions of a camera."""
        return [s for s in self.sessions.values() if s.entity_id == entity_id]

    @callback
    def close(self, session: Session, reason: str) -> None:
        """Close a session, its provider side and its tasks."""
        if self.sessions.pop(session.key, None) is None:
            return
        session.closed = dt_util.utcnow()
        for key in (session.key,):
            if self._by_token.get(key) is session:
                del self._by_token[key]
                self._closed_tokens[key] = ClosedToken(
                    session.entity_id, session.short, session.closed
                )
        if session.outcome in ("pending", "answered"):
            session.outcome = reason
        for task in session.tasks:
            task.cancel()
        session.tasks.clear()
        self.close_provider(session)
        self.history.append(session.as_record())
        self.counters[f"closed_{reason}"] = self.counters.get(f"closed_{reason}", 0) + 1
        _LOGGER.debug("session %s: closed (%s)", session.short, reason)

    @callback
    def close_provider(self, session: Session) -> None:
        """Close the provider (go2rtc) side of a session if open."""
        if not session.provider_open:
            return
        session.provider_open = False
        if (camera := self._camera_lookup(session.entity_id)) is not None:
            camera.close_webrtc_session(session.session_id)

    @callback
    def close_all(self, reason: str) -> None:
        """Close every session and forget every token."""
        for session in list(self.sessions.values()):
            self.close(session, reason)
        self._closed_tokens.clear()

    @callback
    def sweep(self) -> None:
        """Close sessions that got no offer in time or outlived their lifetime."""
        now = dt_util.utcnow()
        for session in list(self.sessions.values()):
            if session.lifetime_expired(now):
                self.close(session, "expired")
            elif not session.active and session.offer_expired(now):
                self.close(session, "no_offer")
        self._expire_closed_tokens()

    def _evict_pending(self, among: list[Session], cap: int) -> None:
        pending = sorted((s for s in among if not s.active), key=lambda s: s.created)
        # The cap counts pending sessions only: a display that never ends its
        # stream must not use up the slots a retrying player needs.
        while len(pending) >= cap:
            oldest = pending.pop(0)
            among.remove(oldest)
            self.counters["evicted"] += 1
            self.close(oldest, "evicted")

    def _expire_closed_tokens(self) -> None:
        now = dt_util.utcnow()
        for key, closed in list(self._closed_tokens.items()):
            if now - closed.closed > TOKEN_GRACE:
                del self._closed_tokens[key]
