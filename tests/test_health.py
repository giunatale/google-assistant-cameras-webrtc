"""go2rtc media-flow verification."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.google_assistant_cameras_webrtc.health import (
    _remote_host,
    async_check_media_flow,
)


def _fake_go2rtc(hass: HomeAssistant, payload, status=200):
    @asynccontextmanager
    async def get(url, timeout=None):
        assert url.endswith("/api/streams")
        yield SimpleNamespace(
            status=status, json=lambda content_type=None: _coro(payload)
        )

    hass.data["go2rtc"] = SimpleNamespace(
        url="http://localhost:1984/", session=SimpleNamespace(get=get)
    )


async def _coro(value):
    return value


def _camera(domain="go2rtc"):
    return MagicMock(webrtc_provider=SimpleNamespace(domain=domain))


async def test_remote_host_parsing():
    assert _remote_host("192.0.2.20:51000 host") == "192.0.2.20"
    assert _remote_host("[fe80::1]:51000 host") == "fe80::1"
    assert _remote_host("203.0.113.5:4000 srflx 192.0.2.20:51000") == "203.0.113.5"


async def test_media_flow_verdicts(hass: HomeAssistant):
    offer = {"192.0.2.20", "203.0.113.5"}
    assert (await async_check_media_flow(hass, _camera("other"), offer))[0] is None
    assert (await async_check_media_flow(hass, _camera(), set()))[0] is None
    assert (await async_check_media_flow(hass, _camera(), offer))[
        0
    ] is None  # no go2rtc data
    _fake_go2rtc(
        hass,
        {
            "camera.x": {
                "consumers": [
                    {"remote_addr": "192.0.2.20:51000 host", "bytes_send": 1234}
                ]
            }
        },
    )
    assert (await async_check_media_flow(hass, _camera(), offer)) == (
        True,
        "connected, 1234 bytes sent",
    )
    _fake_go2rtc(
        hass,
        {
            "camera.x": {
                "consumers": [{"remote_addr": "192.0.2.20:51000 host", "bytes_send": 0}]
            }
        },
    )
    verdict, detail = await async_check_media_flow(hass, _camera(), offer)
    assert verdict is False and "no bytes" in detail
    _fake_go2rtc(
        hass,
        {
            "camera.x": {
                "consumers": [
                    {"remote_addr": "198.51.100.1:1 host", "bytes_send": 99},
                    {"user_agent": "x"},
                ]
            }
        },
    )
    verdict, detail = await async_check_media_flow(hass, _camera(), offer)
    assert verdict is False and "no connected consumer" in detail
    _fake_go2rtc(hass, {"camera.x": {"consumers": None, "producers": None}})
    assert (await async_check_media_flow(hass, _camera(), offer))[0] is False
    _fake_go2rtc(hass, {}, status=500)
    assert (await async_check_media_flow(hass, _camera(), offer))[0] is None
    _fake_go2rtc(hass, ["not", "a", "dict"])
    assert (await async_check_media_flow(hass, _camera(), offer))[0] is None
