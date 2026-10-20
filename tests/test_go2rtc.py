"""go2rtc stream-state reading."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.google_assistant_cameras_webrtc.go2rtc import (
    StreamState,
    _remote_host,
    async_stream_state,
    describe,
)

OFFER = {"192.0.2.20", "203.0.113.5"}


async def _value(payload):
    return payload


def _fake_go2rtc(hass: HomeAssistant, payload, status=200):
    @asynccontextmanager
    async def get(url, timeout=None):
        assert url.endswith("/api/streams")
        yield SimpleNamespace(
            status=status, json=lambda content_type=None: _value(payload)
        )

    hass.data["go2rtc"] = SimpleNamespace(
        url="http://localhost:1984/", session=SimpleNamespace(get=get)
    )


def _camera(domain="go2rtc"):
    cam = MagicMock(
        webrtc_provider=SimpleNamespace(domain=domain),
        entity_id="camera.x",
        unique_id="u1",
    )
    cam.platform.platform_name = "test"
    return cam


def test_remote_host_parsing():
    assert _remote_host("192.0.2.20:51000 host") == "192.0.2.20"
    assert _remote_host("[fe80::1]:51000 host") == "fe80::1"
    assert _remote_host("203.0.113.5:4000 srflx 192.0.2.20:51000") == "203.0.113.5"


async def test_unavailable(hass: HomeAssistant):
    assert await async_stream_state(hass, _camera("other")) is None
    assert await async_stream_state(hass, _camera()) is None
    _fake_go2rtc(hass, {}, status=500)
    assert await async_stream_state(hass, _camera()) is None
    _fake_go2rtc(hass, ["not", "a", "dict"])
    assert await async_stream_state(hass, _camera()) is None
    assert describe(None, OFFER) == "go2rtc not available"


async def test_stream_state(hass: HomeAssistant):
    consumers = [
        {
            "id": 7,
            "protocol": "ws+udp",
            "remote_addr": "198.51.100.1:1 host",
            "bytes_send": 99,
        },
        {
            "id": 9,
            "protocol": "ws+udp",
            "remote_addr": "192.0.2.20:51000 host",
            "bytes_send": 1234,
        },
        {
            "id": 11,
            "protocol": "rtsp",
            "remote_addr": "192.0.2.20:2 host",
            "bytes_send": 5,
        },
        {"id": 12, "protocol": "ws+udp"},
    ]
    producers = [
        {
            "receivers": [
                {"codec": {"codec_name": "h264", "profile": "Main", "level": 31}}
            ]
        }
    ]
    _fake_go2rtc(hass, {"test_u1": {"consumers": consumers, "producers": producers}})
    state = await async_stream_state(hass, _camera())
    assert state.found and state.camera_h264 == "Main level 3.1"
    assert state.connected(OFFER)["id"] == 9
    assert state.connected(set())["id"] == 9  # newest ws consumer with a selected pair
    assert (
        describe(state, OFFER) == "connected, 1234 bytes sent, remote address in offer"
    )
    assert (
        describe(state, set())
        == "connected, 1234 bytes sent, remote address not in offer"
    )
    assert (
        describe(StreamState(True, []), OFFER)
        == "not connected (0 consumer(s) on the stream)"
    )
    _fake_go2rtc(hass, {"other": {}})
    assert (
        describe(await async_stream_state(hass, _camera()), OFFER)
        == "stream unknown to go2rtc"
    )
    _fake_go2rtc(
        hass,
        {
            "test_u1": {
                "producers": [{"receivers": [{"codec": {"codec_name": "h264"}}]}]
            }
        },
    )
    assert (await async_stream_state(hass, _camera())).camera_h264 is None
