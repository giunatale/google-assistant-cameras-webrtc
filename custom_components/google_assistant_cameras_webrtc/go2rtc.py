"""Read go2rtc's stream state for diagnostics.

Home Assistant's camera API does not expose the peer connection, but when the
provider is go2rtc its ``/api/streams`` lists every consumer with the remote
address of the selected ICE pair and the bytes sent, and every producer with
the codec decoded from the camera's SPS. Nothing here affects behaviour: it is
logged and recorded in diagnostics so a report shows what actually happened.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout
from homeassistant.core import HomeAssistant

from .const import GO2RTC_TIMEOUT

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera

_LOGGER = logging.getLogger(__name__)

GO2RTC_DOMAIN = "go2rtc"


@dataclass
class StreamState:
    """What go2rtc reports for one camera stream."""

    found: bool
    consumers: list[dict[str, Any]]
    camera_h264: str | None = None

    def connected(self, offer_addresses: set[str]) -> dict[str, Any] | None:
        """Return the WebRTC consumer most likely ours, if one is connected.

        Only consumers that came through HA's WebSocket path and have a
        selected ICE pair count; one whose remote address is in the display's
        offer is preferred, otherwise the newest.
        """
        candidates = [
            c
            for c in self.consumers
            if str(c.get("protocol", "")).startswith("ws") and c.get("remote_addr")
        ]
        if not candidates:
            return None
        for consumer in candidates:
            if _remote_host(str(consumer["remote_addr"])) in offer_addresses:
                return consumer
        return max(candidates, key=lambda c: _int(c.get("id")))


def stream_name(camera: Camera) -> str:
    """Return the go2rtc stream name HA uses for a camera."""
    try:
        from homeassistant.components.go2rtc.util import (
            get_camera_identifier,
        )

        return get_camera_identifier(camera)
    except Exception:
        if camera.unique_id is not None:
            return f"{camera.platform.platform_name}_{camera.unique_id}"
        return camera.entity_id


async def async_stream_state(hass: HomeAssistant, camera: Camera) -> StreamState | None:
    """Return go2rtc's view of the camera's stream, or None when unavailable."""
    if (
        getattr(getattr(camera, "webrtc_provider", None), "domain", None)
        != GO2RTC_DOMAIN
    ):
        return None
    config = hass.data.get(GO2RTC_DOMAIN)
    url = getattr(config, "url", None)
    session = getattr(config, "session", None)
    if not isinstance(url, str) or session is None:
        return None
    try:
        async with session.get(
            f"{url.rstrip('/')}/api/streams",
            timeout=ClientTimeout(total=GO2RTC_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except Exception as err:
        _LOGGER.debug("go2rtc query failed: %s", type(err).__name__)
        return None
    if not isinstance(data, dict):
        return None
    stream = data.get(stream_name(camera))
    if not isinstance(stream, dict):
        return StreamState(found=False, consumers=[])
    consumers = [c for c in stream.get("consumers") or [] if isinstance(c, dict)]
    return StreamState(
        found=True,
        consumers=consumers,
        camera_h264=_camera_h264(stream),
    )


def _camera_h264(stream: dict[str, Any]) -> str | None:
    """Return "<profile> level <n>" of the producer's H.264 video, if reported."""
    for producer in stream.get("producers") or []:
        if not isinstance(producer, dict):
            continue
        for receiver in producer.get("receivers") or []:
            codec = receiver.get("codec") if isinstance(receiver, dict) else None
            if not isinstance(codec, dict) or codec.get("codec_name") != "h264":
                continue
            profile = codec.get("profile")
            if not isinstance(profile, str):
                return None
            level = codec.get("level")
            if isinstance(level, int) and level > 0:
                return f"{profile} level {level / 10:.1f}"
            return profile
    return None


def describe(state: StreamState | None, offer_addresses: set[str]) -> str:
    """Return a one-line description of the consumer for the log."""
    if state is None:
        return "go2rtc not available"
    if not state.found:
        return "stream unknown to go2rtc"
    if (consumer := state.connected(offer_addresses)) is None:
        return f"not connected ({len(state.consumers)} consumer(s) on the stream)"
    where = (
        "in offer"
        if _remote_host(str(consumer["remote_addr"])) in offer_addresses
        else "not in offer"
    )
    return f"connected, {_int(consumer.get('bytes_send'))} bytes sent, remote address {where}"


def _remote_host(remote: str) -> str:
    """Extract the host from go2rtc's '<host>:<port> <typ> ...' format."""
    first = remote.split(" ", 1)[0]
    if first.startswith("["):
        return first[1 : first.find("]")]
    host, sep, _port = first.rpartition(":")
    return host if sep else first


def _int(value: Any) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return 0
