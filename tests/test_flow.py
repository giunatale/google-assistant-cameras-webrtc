"""SYNC, EXECUTE and the signalling endpoint end to end (fake provider)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from unittest.mock import patch

from homeassistant.components.google_assistant import trait as ga_trait
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.google_assistant_cameras_webrtc import sdp
from custom_components.google_assistant_cameras_webrtc.const import (
    DOMAIN,
    ISSUE_CAMERA_UNSUPPORTED_PREFIX,
    OFFER_WINDOW,
    SESSION_MAX_LIFETIME,
    TOMBSTONE_TTL,
)

from .conftest import ENTITY_ID, FakeProvider
from .sdp_fixtures import CANDIDATES, OFFER_SDP

GET_STREAM = "action.devices.commands.GetCameraStream"
HUB_PARAMS = {"StreamToChromecast": False, "SupportedStreamProtocols": ["webrtc"]}
CAST_PARAMS = {
    "StreamToChromecast": True,
    "SupportedStreamProtocols": ["progressive_mp4", "hls", "dash", "smooth_stream"],
}


async def _execute(trait, params=HUB_PARAMS):
    trt = trait()
    await trt.execute(GET_STREAM, {}, params, {})
    return trt.stream_info


def _path(info):
    return info["cameraStreamSignalingUrl"].split("https://ha.example", 1)[1]


async def test_sync_advertises_webrtc_first(
    hass: HomeAssistant, setup_integration, trait
):
    attrs = trait().sync_attributes()
    assert attrs["cameraStreamSupportedProtocols"] == ["webrtc", "hls"]
    assert attrs["cameraStreamNeedAuthToken"] is False
    assert attrs["cameraStreamNeedDrmEncryption"] is False


async def test_sync_stock_without_https(
    hass: HomeAssistant, config_entry, camera_entity, trait
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert trait().sync_attributes()["cameraStreamSupportedProtocols"] == ["hls"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, "no_https") is not None
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_sync_stock_for_camera_without_webrtc(
    hass: HomeAssistant, setup_integration, trait
):
    hass.states.async_set("camera.plain", "idle", {"supported_features": 2})
    trt = ga_trait.CameraStreamTrait(hass, hass.states.get("camera.plain"), None)
    assert trt.sync_attributes()["cameraStreamSupportedProtocols"] == ["hls"]


async def test_execute_hub_gets_webrtc(hass: HomeAssistant, setup_integration, trait):
    info = await _execute(trait)
    assert info["cameraStreamProtocol"] == "webrtc"
    assert info["cameraStreamSignalingUrl"].startswith(
        "https://ha.example/api/webhook/"
    )
    token = info["cameraStreamAuthToken"]
    assert len(token) == 64 and info["cameraStreamSignalingUrl"].endswith(token)
    ice = json.loads(info["cameraStreamIceServers"])
    assert all(url.startswith("stun:") for server in ice for url in server["urls"])
    assert "cameraStreamAccessUrl" not in info


async def test_execute_cast_target_stays_hls(
    hass: HomeAssistant, setup_integration, trait, mock_request_stream
):
    info = await _execute(trait, CAST_PARAMS)
    assert info == {
        "cameraStreamAccessUrl": "https://ha.example/api/hls/token/master_playlist.m3u8",
        "cameraStreamReceiverAppId": "B45F4572",
        "cameraStreamProtocol": "hls",
    }
    mock_request_stream.assert_awaited_once()


async def test_hls_result_untouched_for_plain_camera(
    hass: HomeAssistant, setup_integration, mock_request_stream
):
    hass.states.async_set("camera.plain", "idle", {"supported_features": 2})
    trt = ga_trait.CameraStreamTrait(hass, hass.states.get("camera.plain"), None)
    await trt.execute(GET_STREAM, {}, CAST_PARAMS, {})
    assert "cameraStreamProtocol" not in trt.stream_info


async def test_signalling_offer_answer_end(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
    caplog,
):
    info = await _execute(trait)
    client = await hass_client_no_auth()
    resp = await client.post(
        _path(info),
        json={"action": "offer", "deviceId": ENTITY_ID, "sdp": OFFER_SDP},
        headers={
            "Authorization": f"Bearer {info['cameraStreamAuthToken']}",
            "Origin": "https://www.gstatic.com",
        },
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["action"] == "answer"
    answer = sdp.inspect_answer(body["sdp"])
    assert answer.embedded_candidates == len(CANDIDATES) and answer.video_ok
    assert "a=end-of-candidates" in body["sdp"]
    assert "trickle" not in body["sdp"]
    assert provider.offers and provider.offers[0][1] == OFFER_SDP
    runtime = setup_integration.runtime_data
    session = next(iter(runtime.sessions.sessions.values()))
    assert (
        session.outcome == "answered"
        and session.auth_header_seen
        and session.origin_seen
    )
    # end closes the provider session and the webhook
    resp = await client.post(_path(info), json={"action": "end", "deviceId": ENTITY_ID})
    assert resp.status == 200 and await resp.json() == {}
    assert provider.closed == [session.session_id]
    assert not runtime.sessions.sessions
    assert runtime.sessions.history[-1]["outcome"] == "end"
    # the secret is dead now: our handler keeps answering 403 (tombstone), quietly
    resp = await client.post(_path(info), json={"action": "offer", "sdp": OFFER_SDP})
    assert resp.status == 403
    ours = "\n".join(
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("custom_components.")
    )
    assert info["cameraStreamAuthToken"] not in ours
    assert (
        "198.51.100" not in ours and "192.0.2" not in ours
    )  # no addresses in our logs


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({"Authorization": "Bearer wrong"}, {"action": "offer", "sdp": OFFER_SDP}, 403),
        (
            {"Authentication": "Bearer wrong"},
            {"action": "offer", "sdp": OFFER_SDP},
            403,
        ),
        ({"Authorization": "Basic abc"}, {"action": "offer", "sdp": OFFER_SDP}, 403),
        ({}, {"action": "offer", "deviceId": "camera.other", "sdp": OFFER_SDP}, 403),
        ({}, {"action": "answer", "sdp": OFFER_SDP}, 400),
        ({}, {"action": "dance"}, 400),
        ({}, {"action": "offer"}, 400),
        ({}, {"action": "offer", "sdp": "not an sdp"}, 400),
        ({}, ["list"], 400),
    ],
)
async def test_signalling_rejections(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    headers,
    body,
    status,
):
    info = await _execute(trait)
    client = await hass_client_no_auth()
    resp = await client.post(_path(info), json=body, headers=headers)
    assert resp.status == status
    assert "sdp" not in await resp.json()


async def test_signalling_bad_json_and_oversize(
    hass: HomeAssistant, setup_integration, trait, hass_client_no_auth
):
    info = await _execute(trait)
    client = await hass_client_no_auth()
    resp = await client.post(
        _path(info), data=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400
    resp = await client.post(
        _path(info),
        data=b"x" * (64 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 413


async def test_authentication_header_accepted_without_bearer(
    hass: HomeAssistant, setup_integration, trait, hass_client_no_auth
):
    """No auth header at all: the per-session URL secret is enough."""
    info = await _execute(trait)
    client = await hass_client_no_auth()
    resp = await client.post(_path(info), json={"action": "offer", "sdp": OFFER_SDP})
    assert resp.status == 200


async def test_offer_after_window_is_rejected(
    hass: HomeAssistant, setup_integration, trait, hass_client_no_auth
):
    info = await _execute(trait)
    client = await hass_client_no_auth()
    with patch(
        "custom_components.google_assistant_cameras_webrtc.signaling.dt_util.utcnow",
        return_value=dt_util.utcnow() + OFFER_WINDOW + timedelta(seconds=1),
    ):
        resp = await client.post(
            _path(info), json={"action": "offer", "sdp": OFFER_SDP}
        )
    assert resp.status == 403


async def test_provider_error_counts_as_failure_and_trips(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
    caplog,
):
    provider.mode = "error"
    runtime = setup_integration.runtime_data
    client = await hass_client_no_auth()
    for _ in range(2):
        info = await _execute(trait)
        resp = await client.post(
            _path(info), json={"action": "offer", "sdp": OFFER_SDP}
        )
        assert resp.status == 502
        assert await resp.json() == {"error": "negotiation failed"}
    assert runtime.breaker.health(ENTITY_ID).state == "tripped"
    assert "tripped to HLS" in caplog.text
    # SYNC now advertises stock, EXECUTE for a webrtc-only target falls back and asks for a sync
    assert trait().sync_attributes()["cameraStreamSupportedProtocols"] == ["hls"]
    with patch(
        "homeassistant.components.google_assistant.trait.camera.async_request_stream",
        return_value="/api/hls/x",
    ):
        info = await _execute(trait)
    assert info["cameraStreamProtocol"] == "hls"
    assert "stale device list" in caplog.text


async def test_codec_mismatch_marks_unsupported_with_repair(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
):
    provider.mode = "codec"
    runtime = setup_integration.runtime_data
    client = await hass_client_no_auth()
    info = await _execute(trait)
    resp = await client.post(_path(info), json={"action": "offer", "sdp": OFFER_SDP})
    assert resp.status == 502
    assert runtime.breaker.health(ENTITY_ID).state == "unsupported"
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{ENTITY_ID}"
    )
    assert issue is not None and issue.is_fixable
    assert trait().sync_attributes()["cameraStreamSupportedProtocols"] == ["hls"]
    # persisted
    await runtime.async_save()
    stored = await runtime.store.async_load()
    assert stored["cameras"][ENTITY_ID]["state"] == "unsupported"
    # the repair flow resets it
    runtime.retry_camera(ENTITY_ID)
    assert runtime.breaker.health(ENTITY_ID).state == "ok"
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{ENTITY_ID}"
        )
        is None
    )
    assert trait().sync_attributes()["cameraStreamSupportedProtocols"] == [
        "webrtc",
        "hls",
    ]


async def test_audio_only_answer_is_unsupported(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
):
    provider.mode = "audio_only"
    runtime = setup_integration.runtime_data
    client = await hass_client_no_auth()
    info = await _execute(trait)
    resp = await client.post(_path(info), json={"action": "offer", "sdp": OFFER_SDP})
    assert resp.status == 502
    assert "no video" in runtime.sessions.history[-1]["outcome"]
    assert runtime.breaker.health(ENTITY_ID).state == "unsupported"


async def test_answer_timeout(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
):
    provider.mode = "timeout"
    client = await hass_client_no_auth()
    info = await _execute(trait)
    with patch(
        "custom_components.google_assistant_cameras_webrtc.signaling.ANSWER_TIMEOUT",
        0.2,
    ):
        resp = await client.post(
            _path(info), json={"action": "offer", "sdp": OFFER_SDP}
        )
    assert resp.status == 504
    runtime = setup_integration.runtime_data
    assert runtime.breaker.health(ENTITY_ID).failures == 1
    assert provider.closed  # provider side closed after the failure


async def test_no_candidates_is_failure(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
):
    provider.mode = "no_candidates"
    client = await hass_client_no_auth()
    info = await _execute(trait)
    with patch(
        "custom_components.google_assistant_cameras_webrtc.signaling.GATHER_CAP", 0.3
    ):
        resp = await client.post(
            _path(info), json={"action": "offer", "sdp": OFFER_SDP}
        )
    assert resp.status == 502
    assert (
        "candidates" in setup_integration.runtime_data.sessions.history[-1]["outcome"]
    )


async def test_second_offer_restarts_session(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
):
    client = await hass_client_no_auth()
    info = await _execute(trait)
    assert (
        await client.post(_path(info), json={"action": "offer", "sdp": OFFER_SDP})
    ).status == 200
    first = provider.offers[0][0]
    assert (
        await client.post(_path(info), json={"action": "offer", "sdp": OFFER_SDP})
    ).status == 200
    assert provider.closed == [first]
    assert provider.offers[1][0] != first


async def test_session_caps_and_sweep(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
):
    runtime = setup_integration.runtime_data
    infos = [await _execute(trait) for _ in range(5)]
    assert len(runtime.sessions.sessions) == 4  # per-camera cap evicted the oldest
    assert runtime.sessions.counters["evicted"] == 1
    client = await hass_client_no_auth()
    assert (
        await client.post(_path(infos[0]), json={"action": "offer", "sdp": OFFER_SDP})
    ).status == 403  # evicted -> tombstone
    assert (
        await client.post(_path(infos[-1]), json={"action": "offer", "sdp": OFFER_SDP})
    ).status == 200
    assert (
        await client.post(_path(infos[-1]), json={"action": "offer", "sdp": OFFER_SDP})
    ).status == 200
    # sweep: sessions without an offer expire after the window, answered ones after the lifetime
    now = dt_util.utcnow()
    with patch(
        "custom_components.google_assistant_cameras_webrtc.sessions.dt_util.utcnow",
        return_value=now + OFFER_WINDOW + timedelta(seconds=1),
    ):
        runtime.sessions.sweep()
    assert set(runtime.sessions.sessions) == {infos[-1]["cameraStreamAuthToken"]}
    assert runtime.sessions.counters["closed_no_offer"] == 3
    assert infos[0]["cameraStreamAuthToken"] in runtime.sessions.tombstones
    with patch(
        "custom_components.google_assistant_cameras_webrtc.sessions.dt_util.utcnow",
        return_value=now + TOMBSTONE_TTL + timedelta(seconds=1),
    ):
        runtime.sessions.sweep()
    assert infos[0]["cameraStreamAuthToken"] not in runtime.sessions.tombstones
    assert (
        await client.post(_path(infos[0]), json={"action": "offer", "sdp": OFFER_SDP})
    ).status == 200  # HA: unknown hook
    with patch(
        "custom_components.google_assistant_cameras_webrtc.sessions.dt_util.utcnow",
        return_value=now + SESSION_MAX_LIFETIME + timedelta(seconds=1),
    ):
        runtime.sessions.sweep()
    assert not runtime.sessions.sessions
    assert provider.closed
    # the periodic sweep is wired
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()


async def test_execute_falls_back_when_camera_gone(
    hass: HomeAssistant, setup_integration, trait, mock_request_stream
):
    hass.states.async_set("camera.ghost", "idle", {"supported_features": 2})
    trt = ga_trait.CameraStreamTrait(hass, hass.states.get("camera.ghost"), None)
    await trt.execute(GET_STREAM, {}, HUB_PARAMS, {})
    assert trt.stream_info["cameraStreamAccessUrl"].endswith("master_playlist.m3u8")


async def test_execute_uses_stock_when_our_code_raises(
    hass: HomeAssistant, setup_integration, trait, mock_request_stream, caplog
):
    with patch.object(
        setup_integration.runtime_data,
        "webrtc_execute",
        side_effect=RuntimeError("boom"),
    ):
        info = await _execute(trait)
    assert info["cameraStreamAccessUrl"].endswith("master_playlist.m3u8")
    assert "using stock HLS" in caplog.text


async def test_provider_error_details_never_leak(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
    caplog,
):
    provider.mode = "error"
    provider.error_message = (
        "dial rtsp://admin:hunter2@198.51.100.77:554/live: connection refused"
    )
    runtime = setup_integration.runtime_data
    client = await hass_client_no_auth()
    info = await _execute(trait)
    resp = await client.post(_path(info), json={"action": "offer", "sdp": OFFER_SDP})
    assert resp.status == 502
    assert await resp.json() == {"error": "negotiation failed"}
    health = runtime.breaker.health(ENTITY_ID)
    everything = "\n".join(
        [
            caplog.text,
            str(runtime.sessions.history[-1]),
            str(health.as_dict()),
            await resp.text(),
        ]
    )
    assert "hunter2" not in everything and "198.51.100.77" not in everything
    assert "connection refused" in health.last_failure


async def test_end_during_offer_closes_provider(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
):
    provider.delay = 0.5
    runtime = setup_integration.runtime_data
    client = await hass_client_no_auth()
    info = await _execute(trait)
    offer = hass.async_create_task(
        client.post(_path(info), json={"action": "offer", "sdp": OFFER_SDP})
    )
    while not provider.offers:
        await asyncio.sleep(0.01)
    session = next(iter(runtime.sessions.sessions.values()))
    runtime.sessions.close(session, "end")
    resp = await offer
    assert resp.status == 403
    assert provider.closed == [session.session_id]
    assert not runtime.sessions.sessions
    assert session.media_check is None and not session.tasks
