"""Shared fixtures: a camera with a fake WebRTC provider, an HTTPS URL, the entry."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components import camera
from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    CameraWebRTCProvider,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.components.google_assistant import trait as ga_trait
from homeassistant.core import HomeAssistant, callback
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)
from webrtc_models import RTCIceCandidateInit

from custom_components.google_assistant_cameras_webrtc.const import DOMAIN

from .sdp_fixtures import ANSWER_SDP, ANSWER_SDP_AUDIO_ONLY, CANDIDATES

ENTITY_ID = "camera.test_camera"
STREAM_SOURCE = "rtsp://198.51.100.10/stream"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom integrations from the repo."""


@pytest.fixture(autouse=True)
def restore_trait() -> AsyncGenerator[None]:
    """Guarantee the stock trait is back after every test, whatever happened."""
    cls = ga_trait.CameraStreamTrait
    original = dict(cls.__dict__)
    yield
    for name in ("sync_attributes", "execute"):
        if cls.__dict__.get(name) is not original[name]:
            setattr(cls, name, original[name])
    for name in list(cls.__dict__):
        if name not in original:
            delattr(cls, name)


@dataclass(eq=False)
class FakeProvider(CameraWebRTCProvider):
    """A scripted WebRTC provider."""

    mode: str = "ok"
    answer_sdp: str = ANSWER_SDP
    candidates: list[str] = field(default_factory=lambda: list(CANDIDATES))
    delay: float = 0.0
    offers: list[tuple[str, str]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    @property
    def domain(self) -> str:
        """Provider domain."""
        return "fake"

    @callback
    def async_is_supported(self, stream_source: str) -> bool:
        """Support rtsp sources."""
        return stream_source.startswith("rtsp://")

    async def async_handle_async_webrtc_offer(
        self,
        cam: Camera,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Script the answer according to mode."""
        self.offers.append((session_id, offer_sdp))
        if self.mode == "error":
            send_message(WebRTCError("fake_error", "boom"))
            return
        if self.mode == "codec":
            send_message(
                WebRTCError(
                    "go2rtc_webrtc_offer_failed",
                    "streams: codecs not matched: video:H265 => video:H264,video:VP8",
                )
            )
            return
        if self.mode == "timeout":
            return
        sdp = ANSWER_SDP_AUDIO_ONLY if self.mode == "audio_only" else self.answer_sdp

        async def _deliver() -> None:
            await asyncio.sleep(self.delay)
            send_message(WebRTCAnswer(sdp))
            if self.mode == "no_candidates":
                return
            for cand in self.candidates:
                await asyncio.sleep(0.01)
                send_message(WebRTCCandidate(RTCIceCandidateInit(cand)))

        cam.hass.async_create_task(_deliver())

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Ignore remote candidates."""

    @callback
    def async_close_session(self, session_id: str) -> None:
        """Record closes."""
        self.closed.append(session_id)


class MockCamera(Camera):
    """A camera with a stream source."""

    _attr_name = "Test camera"
    _attr_unique_id = "test-camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    async def stream_source(self) -> str | None:
        """Return the source."""
        return STREAM_SOURCE

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """No images."""
        return None


@pytest.fixture
def provider() -> FakeProvider:
    """The scripted provider."""
    return FakeProvider()


@pytest.fixture
async def https_url(hass: HomeAssistant) -> None:
    """Give HA an https URL for Google."""
    await async_process_ha_core_config(hass, {"internal_url": "https://ha.example"})


@pytest.fixture
async def camera_entity(hass: HomeAssistant, provider: FakeProvider) -> MockCamera:
    """A camera entity served by the fake provider."""
    entity = MockCamera()
    setup_test_component_platform(hass, camera.DOMAIN, [entity])
    assert await async_setup_component(
        hass, camera.DOMAIN, {camera.DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    camera.async_register_webrtc_provider(hass, provider)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID) is not None
    assert entity.webrtc_provider is provider
    return entity


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Our single entry."""
    return MockConfigEntry(
        domain=DOMAIN, title="Google Assistant WebRTC Cameras", data={}
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    https_url: None,
    camera_entity: MockCamera,
) -> AsyncGenerator[MockConfigEntry]:
    """Set the integration up and tear it down."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    if config_entry.state.recoverable:
        await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
def mock_request_stream() -> AsyncGenerator[AsyncMock]:
    """Stock HLS path without a real stream."""
    with patch(
        "homeassistant.components.google_assistant.trait.camera.async_request_stream",
        new=AsyncMock(return_value="/api/hls/token/master_playlist.m3u8"),
    ) as mocked:
        yield mocked


def make_trait(hass: HomeAssistant, entity_id: str = ENTITY_ID) -> Any:
    """Instantiate HA's trait for a camera state (config is unused by it)."""
    state = hass.states.get(entity_id)
    assert state is not None
    return ga_trait.CameraStreamTrait(hass, state, None)


@pytest.fixture
def trait(hass: HomeAssistant) -> Callable[[], Any]:
    """Factory for a fresh trait instance."""
    return lambda: make_trait(hass)
