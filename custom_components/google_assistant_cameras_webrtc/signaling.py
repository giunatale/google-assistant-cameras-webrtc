"""The signalling endpoint behind each session's webhook.

Google's player POSTs ``{"action": "offer"|"end", "deviceId", "sdp"}`` with
``Authorization: Bearer <cameraStreamAuthToken>`` (the docs spell the header
"Authentication"; real displays send ``Authorization`` and both are accepted).
The session secret is already in the URL, so a missing header is tolerated; a
present one must match.

An offer is handed to the camera's WebRTC provider; the answer and the
trickled candidates are collected, folded into one SDP (no trickle for Google)
and returned as ``{"action": "answer", "sdp": ...}``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hmac
import json
import logging
from typing import TYPE_CHECKING, Any
import uuid

from aiohttp import web
from homeassistant.components.camera import (
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCMessage,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import sdp as sdp_tools
from .const import (
    ACTION_ANSWER,
    ACTION_END,
    ACTION_OFFER,
    ANSWER_TIMEOUT,
    GATHER_CAP,
    GATHER_MIN,
    GATHER_QUIET,
    MAX_BODY_BYTES,
    MAX_SDP_BYTES,
)

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera

    from .runtime import Runtime
    from .sessions import Session

_LOGGER = logging.getLogger(__name__)

CODEC_MISMATCH_MARKERS = ("codecs not matched",)


class NegotiationError(Exception):
    """A failed offer/answer exchange."""

    def __init__(
        self, reason: str, *, status: int = 502, permanent: bool = False
    ) -> None:
        """Store reason, HTTP status and whether the failure is deterministic."""
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.permanent = permanent


@dataclass
class Negotiation:
    """Result of a successful exchange."""

    answer_sdp: str
    merged_sdp: str
    info: sdp_tools.AnswerInfo
    candidates: list[sdp_tools.Candidate]
    answer_ms: int
    gather_ms: int

    @property
    def candidate_types(self) -> dict[str, int]:
        """Count candidates per type/transport for logs and diagnostics."""
        counts: dict[str, int] = {}
        for candidate in self.candidates:
            key = f"{candidate.typ or 'unknown'}/{candidate.transport or '?'}"
            counts[key] = counts.get(key, 0) + 1
        return counts


@dataclass
class _Collector:
    """Collects provider messages for one offer."""

    loop: asyncio.AbstractEventLoop
    answer: asyncio.Future[str]
    error: asyncio.Future[WebRTCError]
    candidates: list[sdp_tools.Candidate] = field(default_factory=list)
    last_candidate_at: float | None = None
    ignored: int = 0

    def __call__(self, message: WebRTCMessage) -> None:
        """Receive a message from the provider (sync callback)."""
        if isinstance(message, WebRTCAnswer):
            if not self.answer.done():
                self.answer.set_result(message.answer)
        elif isinstance(message, WebRTCCandidate):
            raw = message.candidate
            self.candidates.append(
                sdp_tools.Candidate(
                    candidate=str(getattr(raw, "candidate", raw)),
                    sdp_mid=getattr(raw, "sdp_mid", None),
                    sdp_m_line_index=getattr(raw, "sdp_m_line_index", None),
                )
            )
            self.last_candidate_at = self.loop.time()
        elif isinstance(message, WebRTCError):
            if not self.error.done():
                self.error.set_result(message)
        else:
            self.ignored += 1


async def async_negotiate(
    camera: Camera, session: Session, offer_sdp: str
) -> Negotiation:
    """Hand the offer to the provider and return the complete answer."""
    loop = asyncio.get_running_loop()
    collector = _Collector(loop, loop.create_future(), loop.create_future())
    started = loop.time()
    try:
        await camera.async_handle_async_webrtc_offer(
            offer_sdp, session.session_id, collector
        )
    except HomeAssistantError as err:
        raise NegotiationError(f"provider refused the offer: {err}") from err
    session.provider_open = True

    # 1. Wait for the answer (or an error).
    done, _pending = await asyncio.wait(
        {collector.answer, collector.error},
        timeout=ANSWER_TIMEOUT,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if collector.error.done():
        _raise_provider_error(collector.error.result())
    if collector.answer not in done:
        raise NegotiationError(f"no answer within {ANSWER_TIMEOUT:.0f}s", status=504)
    answer_sdp = collector.answer.result()
    answered_at = loop.time()
    answer_ms = int((answered_at - started) * 1000)

    # 2. Gather candidates: providers never say "done", so stop after a quiet
    #    period (candidates arrive in a burst) or a hard cap.
    while True:
        if collector.error.done():
            _raise_provider_error(collector.error.result())
        now = loop.time()
        elapsed = now - answered_at
        if elapsed >= GATHER_CAP:
            break
        if (
            elapsed >= GATHER_MIN
            and collector.candidates
            and collector.last_candidate_at is not None
            and now - collector.last_candidate_at >= GATHER_QUIET
        ):
            break
        await asyncio.sleep(0.05)
    gather_ms = int((loop.time() - answered_at) * 1000)

    info = sdp_tools.inspect_answer(answer_sdp)
    if not info.has_video:
        raise NegotiationError(
            "answer carries no video (codec not accepted by the display)",
            permanent=True,
        )
    if not info.video_ok:
        raise NegotiationError(
            f"video codec {'/'.join(info.video_codecs) or 'unknown'} is not H.264/VP8",
            permanent=True,
        )
    if not collector.candidates and info.embedded_candidates == 0:
        raise NegotiationError("provider produced no ICE candidates")

    merged = sdp_tools.merge_candidates(answer_sdp, collector.candidates)
    return Negotiation(
        answer_sdp=answer_sdp,
        merged_sdp=merged,
        info=info,
        candidates=collector.candidates,
        answer_ms=answer_ms,
        gather_ms=gather_ms,
    )


def _raise_provider_error(error: WebRTCError) -> None:
    message = f"{error.code}: {error.message}"
    permanent = any(marker in error.message for marker in CODEC_MISMATCH_MARKERS)
    raise NegotiationError(message, permanent=permanent)


def _json(status: int, payload: dict[str, Any]) -> web.Response:
    return web.json_response(payload, status=status)


def _bearer(request: web.Request) -> str | None:
    """Return the bearer token from Authorization or Authentication, if any."""
    for header in ("Authorization", "Authentication"):
        value = request.headers.get(header)
        if value:
            scheme, _sep, token = value.strip().partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
            return ""
    return None


async def async_handle_webhook(
    runtime: Runtime, hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.Response:
    """Handle a signalling POST for one session."""
    session = runtime.sessions.get(webhook_id)
    if session is None or not runtime.active:
        return _json(403, {"error": "forbidden"})
    short = session.short

    # Authentication: the URL already carries the secret; verify a header too.
    bearer = _bearer(request)
    session.auth_header_seen = bearer is not None
    if bearer is not None and not hmac.compare_digest(bearer, session.token):
        _LOGGER.debug("session %s: bearer token mismatch", short)
        return _json(403, {"error": "forbidden"})
    session.origin_seen = request.headers.get("Origin") is not None

    # Body
    length = request.content_length
    if length is not None and length > MAX_BODY_BYTES:
        return _json(413, {"error": "body too large"})
    raw = await request.content.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        return _json(413, {"error": "body too large"})
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except UnicodeDecodeError, ValueError:
        return _json(400, {"error": "invalid JSON"})
    if not isinstance(body, dict):
        return _json(400, {"error": "invalid body"})

    device_id = body.get("deviceId")
    if device_id is not None and device_id != session.entity_id:
        _LOGGER.debug("session %s: deviceId does not match the session", short)
        return _json(403, {"error": "forbidden"})

    action = body.get("action")
    _LOGGER.debug(
        "session %s: %s received for %s (auth header: %s, origin header: %s, deviceId: %s)",
        short,
        action,
        session.entity_id,
        "present" if bearer is not None else "absent",
        "present" if session.origin_seen else "absent",
        "ok" if device_id is not None else "absent",
    )

    if action == ACTION_END:
        runtime.sessions.close(session, "end")
        return _json(200, {})
    if action == ACTION_ANSWER:
        return _json(400, {"error": "this endpoint only accepts offers"})
    if action != ACTION_OFFER:
        return _json(400, {"error": "unknown action"})

    offer_sdp = body.get("sdp")
    if not isinstance(offer_sdp, str) or not offer_sdp.strip():
        return _json(400, {"error": "missing sdp"})
    if len(offer_sdp) > MAX_SDP_BYTES or "m=" not in offer_sdp:
        return _json(400, {"error": "invalid sdp"})

    now = dt_util.utcnow()
    if session.offer_expired(now):
        _LOGGER.debug("session %s: offer arrived after the offer window", short)
        return _json(403, {"error": "forbidden"})

    camera = runtime.camera(session.entity_id)
    if camera is None:
        runtime.sessions.close(session, "camera_gone")
        return _json(404, {"error": "camera not found"})

    if session.provider_open:  # a second offer: treat as a restart
        _LOGGER.debug("session %s: new offer on an open session, restarting", short)
        runtime.sessions.close_provider(session)
        session.session_id = uuid.uuid4().hex
    session.offer_received = now
    session.offer_addresses = sdp_tools.candidate_addresses(offer_sdp)
    session.offer_ufrag = sdp_tools.ice_ufrag(offer_sdp)
    video, audio = sdp_tools.offer_codecs(offer_sdp)
    _LOGGER.debug(
        "session %s: offer %d bytes, display accepts video %s, audio %s",
        short,
        len(offer_sdp),
        list(video),
        list(audio),
    )
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "session %s: offer SDP (redacted):\n%s", short, sdp_tools.redact(offer_sdp)
        )

    try:
        result = await async_negotiate(camera, session, offer_sdp)
    except NegotiationError as err:
        session.outcome = f"failed: {err.reason}"
        _LOGGER.warning(
            "session %s: WebRTC negotiation for %s failed: %s",
            short,
            session.entity_id,
            err.reason,
        )
        runtime.sessions.close(session, "failed")
        runtime.record_negotiation_failure(session.entity_id, err)
        return _json(err.status, {"error": err.reason})
    except Exception:
        session.outcome = "failed: internal error"
        _LOGGER.exception(
            "session %s: unexpected error negotiating %s", short, session.entity_id
        )
        runtime.sessions.close(session, "failed")
        return _json(500, {"error": "internal error"})

    session.answered = dt_util.utcnow()
    session.outcome = "answered"
    session.answer_ms = result.answer_ms
    session.gather_ms = result.gather_ms
    session.candidates = len(result.candidates)
    session.candidate_types = result.candidate_types
    session.video_codecs = result.info.video_codecs
    session.audio_codecs = result.info.audio_codecs
    _LOGGER.debug(
        "session %s: answer from provider in %d ms, %d candidates gathered in %d ms %s, "
        "video %s, audio %s, bundle=%s rtcp-mux=%s",
        short,
        result.answer_ms,
        len(result.candidates),
        result.gather_ms,
        result.candidate_types,
        list(result.info.video_codecs),
        list(result.info.audio_codecs) or "none",
        result.info.bundle,
        result.info.rtcp_mux,
    )
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "session %s: answer SDP returned to the display (redacted):\n%s",
            short,
            sdp_tools.redact(result.merged_sdp),
        )
    runtime.schedule_media_check(session)
    return _json(200, {"action": ACTION_ANSWER, "sdp": result.merged_sdp})
