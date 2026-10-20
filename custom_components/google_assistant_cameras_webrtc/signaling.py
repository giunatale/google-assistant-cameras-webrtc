"""The signalling endpoint behind the one webhook.

Google's players POST ``{"action": "offer"|"end", "deviceId", "sdp"}`` with
``Authorization: Bearer <cameraStreamAuthToken>``. The token selects the
session and therefore the camera; ``deviceId`` is informational. Google's TV
player, when it switches cameras, sends the fresh token of the new request
together with the ``deviceId`` of the camera it was showing, so the token is
what decides and a differing ``deviceId`` is only logged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
from .sessions import Session

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera

    from .runtime import Runtime

_LOGGER = logging.getLogger(__name__)

CODEC_MISMATCH_MARKERS = ("codecs not matched",)


class NegotiationError(Exception):
    """A failed offer/answer exchange."""

    def __init__(
        self, reason: str, *, status: int = 502, permanent: bool = False
    ) -> None:
        """Store the (sanitised) reason, HTTP status and whether it is deterministic."""
        self.reason = sdp_tools.sanitize_reason(reason)
        self.status = status
        self.permanent = permanent
        super().__init__(self.reason)


@dataclass
class Negotiation:
    """Result of a successful exchange."""

    merged_sdp: str
    info: sdp_tools.AnswerInfo
    candidates: list[sdp_tools.Candidate]
    answer_ms: int
    gather_ms: int

    @property
    def candidate_types(self) -> dict[str, int]:
        """Count candidates per type/transport."""
        counts: dict[str, int] = {}
        for candidate in self.candidates:
            key = f"{candidate.typ or 'unknown'}/{candidate.transport or '?'}"
            counts[key] = counts.get(key, 0) + 1
        return counts


@dataclass
class _Collector:
    """Collects the provider's messages for one offer."""

    loop: asyncio.AbstractEventLoop
    answer: asyncio.Future[str]
    error: asyncio.Future[WebRTCError]
    candidates: list[sdp_tools.Candidate] = field(default_factory=list)
    last_candidate_at: float | None = None

    def __call__(self, message: WebRTCMessage) -> None:
        """Receive a provider message."""
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
        elif isinstance(message, WebRTCError) and not self.error.done():
            self.error.set_result(message)


async def async_negotiate(
    camera: Camera, session: Session, offer_sdp: str
) -> Negotiation:
    """Hand the offer to the provider and return the complete answer.

    Media is one-way towards the display: the microphone track every Google
    player offers is declined before the provider sees the offer.
    """
    loop = asyncio.get_running_loop()
    collector = _Collector(loop, loop.create_future(), loop.create_future())
    started = loop.time()
    one_way = sdp_tools.one_way_offer(offer_sdp)
    if one_way != offer_sdp:
        _LOGGER.debug(
            "session %s: offer directions %s -> %s",
            session.short,
            sdp_tools.directions(offer_sdp),
            sdp_tools.directions(one_way),
        )
    session.provider_open = True
    try:
        await camera.async_handle_async_webrtc_offer(
            one_way, session.session_id, collector
        )
    except HomeAssistantError as err:
        raise NegotiationError(f"provider refused the offer: {err}") from err

    done, _pending = await asyncio.wait(
        {collector.answer, collector.error},
        timeout=ANSWER_TIMEOUT,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if collector.error.done():
        _raise_provider_error(collector.error.result())
    if collector.answer not in done:
        raise NegotiationError(f"no answer within {ANSWER_TIMEOUT:.0f}s", status=504)
    answer_sdp = sdp_tools.one_way_answer(collector.answer.result())
    if sdp_tools.directions(answer_sdp) != sdp_tools.directions(
        collector.answer.result()
    ):
        _LOGGER.debug(
            "session %s: answer directions %s -> %s",
            session.short,
            sdp_tools.directions(collector.answer.result()),
            sdp_tools.directions(answer_sdp),
        )
    answered_at = loop.time()

    # The provider never signals the end of gathering: stop after a quiet period
    # or a hard cap.
    while True:
        if collector.error.done():
            _raise_provider_error(collector.error.result())
        now = loop.time()
        if now - answered_at >= GATHER_CAP:
            break
        if (
            now - answered_at >= GATHER_MIN
            and collector.last_candidate_at is not None
            and now - collector.last_candidate_at >= GATHER_QUIET
        ):
            break
        await asyncio.sleep(0.05)

    info = sdp_tools.inspect_answer(answer_sdp)
    if not info.has_video:
        raise NegotiationError(
            "answer carries no video the display accepts", permanent=True
        )
    if not info.video_ok:
        raise NegotiationError(
            f"video codec {'/'.join(info.video_codecs) or 'unknown'} is not H.264/VP8",
            permanent=True,
        )
    if not collector.candidates and info.embedded_candidates == 0:
        raise NegotiationError("provider produced no ICE candidates")
    return Negotiation(
        merged_sdp=sdp_tools.merge_candidates(answer_sdp, collector.candidates),
        info=info,
        candidates=collector.candidates,
        answer_ms=int((answered_at - started) * 1000),
        gather_ms=int((loop.time() - answered_at) * 1000),
    )


def _raise_provider_error(error: WebRTCError) -> None:
    permanent = any(marker in error.message for marker in CODEC_MISMATCH_MARKERS)
    raise NegotiationError(f"{error.code}: {error.message}", permanent=permanent)


def _json(status: int, payload: dict[str, Any]) -> web.Response:
    return web.json_response(payload, status=status)


def _bearer(request: web.Request) -> str | None:
    """Return the bearer token from Authorization or Authentication."""
    for header in ("Authorization", "Authentication"):
        if value := request.headers.get(header):
            scheme, _sep, token = value.strip().partition(" ")
            return token.strip() if scheme.lower() == "bearer" else None
    return None


async def _read_json(request: web.Request) -> dict[str, Any] | web.Response:
    """Return the JSON object body, or the error response to send."""
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        return _json(413, {"error": "body too large"})
    raw = await request.content.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        return _json(413, {"error": "body too large"})
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except UnicodeDecodeError, ValueError:
        return _json(400, {"error": "invalid JSON"})
    return body if isinstance(body, dict) else _json(400, {"error": "invalid body"})


def _end_superseded(runtime: Runtime, session: Session, left: str) -> None:
    """Close the display's open session on the camera it is switching away from.

    Google's TV player does not send ``end`` when it switches cameras: it posts
    the new request's token with the ``deviceId`` of the camera it was showing.
    That ``deviceId`` names the camera being left and the offer's host
    candidates identify the display (devices behind one NAT share a reflexive
    address, never a host one), so its open session on that camera is closed as
    if ``end`` had arrived. Other displays on the same camera are untouched.
    """
    for other in runtime.sessions.for_camera(left):
        if other is session or not other.offer_received:
            continue
        if not (other.host_addresses & session.host_addresses):
            continue
        _LOGGER.debug(
            "session %s: display moved on to %s (session %s); ending",
            other.short,
            session.entity_id,
            session.short,
        )
        runtime.sessions.close(other, "superseded")


def _rejected(runtime: Runtime, why: str, action: Any) -> web.Response:
    _LOGGER.debug(
        "signalling POST rejected: %s, action=%s, %d live sessions",
        why,
        action if action in (ACTION_OFFER, ACTION_END) else "other",
        len(runtime.sessions.sessions),
    )
    return _json(403, {"error": "forbidden"})


async def async_handle_webhook(runtime: Runtime, request: web.Request) -> web.Response:
    """Handle a signalling POST."""
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    action = body.get("action")
    device_id = body.get("deviceId")
    bearer = _bearer(request)
    if not bearer:
        return _rejected(runtime, "missing bearer token", action)
    source = runtime.sessions.resolve(bearer) if runtime.active else None
    if source is None:
        if action == ACTION_END:
            return _json(200, {})  # nothing to end; same answer as a real end
        return _rejected(runtime, "unknown token", action)

    if action == ACTION_END:
        if isinstance(source, Session):
            if device_id not in (None, source.entity_id):
                _LOGGER.debug(
                    "session %s: end carried deviceId %s", source.short, device_id
                )
            await runtime.log_stream_state(source)
            runtime.sessions.close(source, "end")
        return _json(200, {})
    if action == ACTION_ANSWER:
        return _json(400, {"error": "this endpoint only accepts offers"})
    if action != ACTION_OFFER:
        return _json(400, {"error": "unknown action"})

    if not isinstance(source, Session):
        return _rejected(runtime, "token of a closed session", action)
    session = source
    if device_id not in (None, session.entity_id):
        _LOGGER.debug(
            "session %s: offer carried deviceId %s; the token selects %s",
            session.short,
            device_id,
            session.entity_id,
        )

    offer_sdp = body.get("sdp")
    if not isinstance(offer_sdp, str) or "m=" not in offer_sdp:
        return _json(400, {"error": "missing or invalid sdp"})
    if len(offer_sdp) > MAX_SDP_BYTES:
        return _json(400, {"error": "sdp too large"})
    now = dt_util.utcnow()
    if session.offer_expired(now):
        _LOGGER.debug("session %s: offer arrived after the offer window", session.short)
        return _json(403, {"error": "forbidden"})
    camera = runtime.camera(session.entity_id)
    if camera is None:
        runtime.sessions.close(session, "camera_gone")
        return _json(404, {"error": "camera not found"})
    if session.provider_open:
        _LOGGER.debug(
            "session %s: new offer on an open session, restarting", session.short
        )
        runtime.sessions.close_provider(session)
        session.session_id = uuid.uuid4().hex

    session.offer_received = now
    session.origin_seen = request.headers.get("Origin") is not None
    session.offer_addresses = sdp_tools.candidate_addresses(offer_sdp)
    session.host_addresses = sdp_tools.candidate_addresses(offer_sdp, host_only=True)
    if device_id not in (None, session.entity_id):
        _end_superseded(runtime, session, device_id)
    session.display_h264_profiles = sdp_tools.h264_profiles(offer_sdp)
    video, audio = sdp_tools.offer_codecs(offer_sdp)
    _LOGGER.debug(
        "session %s: offer received for %s (origin header: %s, %d bytes), display "
        "accepts video %s (H264 profile-level-id %s), audio %s",
        session.short,
        session.entity_id,
        "present" if session.origin_seen else "absent",
        len(offer_sdp),
        list(video),
        list(session.display_h264_profiles),
        list(audio),
    )
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "session %s: offer SDP (redacted):\n%s",
            session.short,
            sdp_tools.redact(offer_sdp),
        )

    try:
        result = await async_negotiate(camera, session, offer_sdp)
    except NegotiationError as err:
        session.outcome = f"failed: {err.reason}"
        _LOGGER.warning(
            "session %s: WebRTC negotiation for %s failed: %s",
            session.short,
            session.entity_id,
            err.reason,
        )
        runtime.sessions.close(session, "failed")
        if err.permanent:
            runtime.disable(session.entity_id, err.reason)
        return _json(err.status, {"error": "negotiation failed"})
    except Exception:
        session.outcome = "failed: internal error"
        _LOGGER.exception(
            "session %s: unexpected error negotiating %s",
            session.short,
            session.entity_id,
        )
        runtime.sessions.close(session, "failed")
        return _json(500, {"error": "internal error"})

    if session.key not in runtime.sessions.sessions:
        runtime.sessions.close_provider(session)  # closed while the provider answered
        return _json(403, {"error": "forbidden"})
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
        session.short,
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
            session.short,
            sdp_tools.redact(result.merged_sdp),
        )
    runtime.schedule_media_check(session)
    return _json(
        200, {"action": ACTION_ANSWER, "type": ACTION_ANSWER, "sdp": result.merged_sdp}
    )
