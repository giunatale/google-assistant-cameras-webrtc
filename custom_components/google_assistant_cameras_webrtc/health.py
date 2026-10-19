"""Media-flow verification through go2rtc.

Signalling can succeed while no media ever flows (a display that cannot reach
the LAN addresses in the ICE candidates, a firewall between VLANs). Home
Assistant's camera API does not surface the peer-connection state, but when
the provider is go2rtc its ``/api/streams`` API lists every consumer with the
remote address of the selected ICE pair and the bytes sent so far. Our
consumer is the one whose remote address is one of the display's own
candidate addresses from the offer.

Any other provider, or any error, yields ``None`` ("unknown") and never a
breaker failure.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout
from homeassistant.core import HomeAssistant

from .const import MEDIA_CHECK_TIMEOUT

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera

_LOGGER = logging.getLogger(__name__)

GO2RTC_DOMAIN = "go2rtc"


def _go2rtc_access(hass: HomeAssistant) -> tuple[str, Any] | None:
    """Return (url, aiohttp session) of HA's go2rtc, if present."""
    config = hass.data.get(GO2RTC_DOMAIN)
    url = getattr(config, "url", None)
    session = getattr(config, "session", None)
    if not isinstance(url, str) or session is None:
        return None
    return url.rstrip("/"), session


async def async_check_media_flow(
    hass: HomeAssistant, camera: Camera, offer_addresses: set[str]
) -> tuple[bool | None, str]:
    """Return (flowing, detail).

    flowing is True when go2rtc shows our consumer connected and sending,
    False when it is absent or idle, None when the check is not possible.
    """
    provider = getattr(camera, "webrtc_provider", None)
    if getattr(provider, "domain", None) != GO2RTC_DOMAIN:
        return None, "provider is not go2rtc"
    if not offer_addresses:
        return None, "offer carried no candidate addresses"
    access = _go2rtc_access(hass)
    if access is None:
        return None, "go2rtc API not reachable from HA"
    url, session = access
    try:
        async with session.get(
            f"{url}/api/streams", timeout=ClientTimeout(total=MEDIA_CHECK_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                return None, f"go2rtc /api/streams returned {resp.status}"
            data = await resp.json(content_type=None)
    except Exception as err:
        return None, f"go2rtc query failed: {type(err).__name__}"
    if not isinstance(data, dict):
        return None, "unexpected go2rtc response"

    best: tuple[bool, str] | None = None
    for stream in data.values():
        if not isinstance(stream, dict):
            continue
        for consumer in stream.get("consumers") or []:
            if not isinstance(consumer, dict):
                continue
            remote = str(consumer.get("remote_addr") or "")
            if not remote:
                continue
            host = _remote_host(remote)
            if host not in offer_addresses:
                continue
            sent = int(consumer.get("bytes_send") or 0)
            if sent > 0:
                return True, f"connected, {sent} bytes sent"
            best = (False, "connected but no bytes sent yet")
    if best is not None:
        return best
    return False, "no connected consumer matches the display's candidates"


def _remote_host(remote: str) -> str:
    """Extract the host from go2rtc's '<host>:<port> <typ> ...' format."""
    first = remote.split(" ", 1)[0]
    if first.startswith("["):  # [ipv6]:port
        return first[1 : first.find("]")]
    host, sep, _port = first.rpartition(":")
    return host if sep else first
