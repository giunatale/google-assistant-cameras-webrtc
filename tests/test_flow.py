"""SYNC, EXECUTE and the signalling endpoint end to end, through a real HTTP client."""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest

from custom_components.google_assistant_cameras_webrtc import sdp
from custom_components.google_assistant_cameras_webrtc.const import (
    DOMAIN,
    ISSUE_CAMERA_UNSUPPORTED_PREFIX,
    OFFER_WINDOW,
    SESSION_MAX_LIFETIME,
    TOKEN_GRACE,
)

from .conftest import (
    CAST_PARAMS,
    ENTITY_ID,
    GET_STREAM,
    HUB_PARAMS,
    OTHER_ENTITY_ID,
    FakeProvider,
    auth,
    execute,
    make_trait,
    signalling_path,
)
from .sdp_fixtures import CANDIDATES, OFFER_SDP

OFFER = {"action": "offer", "deviceId": ENTITY_ID, "sdp": OFFER_SDP}


async def test_sync_attributes(hass: HomeAssistant, setup_integration, trait):
    attrs = trait().sync_attributes()
    assert attrs["cameraStreamSupportedProtocols"] == ["webrtc", "hls"]
    assert attrs["cameraStreamNeedAuthToken"] is True
    assert attrs["cameraStreamNeedDrmEncryption"] is False
    hass.states.async_set("camera.plain", "idle", {"supported_features": 2})
    plain = make_trait(hass, "camera.plain").sync_attributes()
    assert (
        plain["cameraStreamSupportedProtocols"] == ["hls"]
        and plain["cameraStreamNeedAuthToken"] is False
    )


async def test_sync_stock_without_https(
    hass: HomeAssistant, config_entry, cameras, trait
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert trait().sync_attributes()["cameraStreamSupportedProtocols"] == ["hls"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, "no_https") is not None
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_execute_webrtc_and_hls(
    hass: HomeAssistant, setup_integration, trait, mock_request_stream
):
    info = await execute(trait)
    assert info["cameraStreamProtocol"] == "webrtc"
    assert (
        info["cameraStreamSignalingUrl"]
        == f"https://ha.example/api/webhook/{setup_integration.data['webhook_id']}"
    )
    assert len(info["cameraStreamAuthToken"]) == 64
    assert all(
        u.startswith("stun:")
        for s in json.loads(info["cameraStreamIceServers"])
        for u in s["urls"]
    )
    other = await execute(trait, entity_id=OTHER_ENTITY_ID)
    assert other["cameraStreamSignalingUrl"] == info["cameraStreamSignalingUrl"]
    assert other["cameraStreamAuthToken"] != info["cameraStreamAuthToken"]
    hls = await execute(trait, CAST_PARAMS)
    assert (
        hls["cameraStreamAccessUrl"]
        == "https://ha.example/api/hls/token/master_playlist.m3u8"
    )
    assert (
        hls["cameraStreamProtocol"] == "hls" and len(hls["cameraStreamAuthToken"]) == 64
    )
    hass.states.async_set("camera.plain", "idle", {"supported_features": 2})
    trt = make_trait(hass, "camera.plain")
    await trt.execute(GET_STREAM, {}, CAST_PARAMS, {})
    assert set(trt.stream_info) == {
        "cameraStreamAccessUrl",
        "cameraStreamReceiverAppId",
    }


async def test_offer_answer_end(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
    caplog,
):
    info = await execute(trait)
    client = await hass_client_no_auth()
    resp = await client.post(signalling_path(info), json=OFFER, headers=auth(info))
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["action"] == body["type"] == "answer"
    answer = sdp.inspect_answer(body["sdp"])
    assert answer.embedded_candidates == len(CANDIDATES) and answer.video_ok
    assert "a=end-of-candidates" in body["sdp"] and "trickle" not in body["sdp"]
    runtime = setup_integration.runtime_data
    (session,) = runtime.sessions.sessions.values()
    assert session.outcome == "answered" and session.origin_seen
    resp = await client.post(
        signalling_path(info),
        json={"action": "end", "deviceId": ENTITY_ID},
        headers=auth(info),
    )
    assert resp.status == 200 and await resp.json() == {}
    assert provider.closed == [session.session_id] and not runtime.sessions.sessions
    assert runtime.sessions.history[-1]["outcome"] == "end"
    resp = await client.post(
        signalling_path(info), json={"action": "end"}, headers=auth(info)
    )
    assert resp.status == 200
    ours = "\n".join(
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("custom_components.")
    )
    assert (
        info["cameraStreamAuthToken"] not in ours
        and "198.51.100" not in ours
        and "192.0.2" not in ours
    )


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({}, OFFER, 403),
        ({"Authorization": "Bearer wrong"}, OFFER, 403),
        ({"Authentication": "Bearer wrong"}, OFFER, 403),
        ({"Authorization": "Basic abc"}, OFFER, 403),
        ("ok", {"action": "answer", "sdp": OFFER_SDP}, 400),
        ("ok", {"action": "dance"}, 400),
        ("ok", {"action": "offer"}, 400),
        ("ok", {"action": "offer", "sdp": "not an sdp"}, 400),
        ("ok", ["list"], 400),
    ],
)
async def test_signalling_rejections(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider,
    headers,
    body,
    status,
):
    info = await execute(trait)
    client = await hass_client_no_auth()
    resp = await client.post(
        signalling_path(info),
        json=body,
        headers=auth(info) if headers == "ok" else headers,
    )
    assert resp.status == status and "sdp" not in await resp.json()
    assert not provider.offers
    resp = await client.post(
        signalling_path(info),
        data=b"{not json",
        headers={**auth(info), "Content-Type": "application/json"},
    )
    assert resp.status == 400
    resp = await client.post(
        signalling_path(info), data=b"x" * (64 * 1024 + 1), headers=auth(info)
    )
    assert resp.status == 413


async def test_offer_after_window_is_rejected(
    hass: HomeAssistant, setup_integration, trait, hass_client_no_auth
):
    info = await execute(trait)
    client = await hass_client_no_auth()
    later = dt_util.utcnow() + OFFER_WINDOW + timedelta(seconds=1)
    with patch(
        "custom_components.google_assistant_cameras_webrtc.signaling.dt_util.utcnow",
        return_value=later,
    ):
        resp = await client.post(signalling_path(info), json=OFFER, headers=auth(info))
    assert resp.status == 403


async def test_token_decides_the_camera(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
    caplog,
):
    """Google's TV player sends the new token with the deviceId of the camera it shows."""
    runtime = setup_integration.runtime_data
    client = await hass_client_no_auth()
    playing = await execute(trait)
    assert (
        await client.post(signalling_path(playing), json=OFFER, headers=auth(playing))
    ).status == 200
    # the user asks for the other camera: fresh token, stale deviceId
    other = await execute(trait, entity_id=OTHER_ENTITY_ID)
    resp = await client.post(
        signalling_path(other),
        json={**OFFER, "deviceId": ENTITY_ID},
        headers=auth(other),
    )
    assert resp.status == 200
    assert (
        f"offer carried deviceId {ENTITY_ID}; the token selects {OTHER_ENTITY_ID}"
        in caplog.text
    )
    assert len(provider.offers) == 2
    session = runtime.sessions.resolve(other["cameraStreamAuthToken"])
    assert session.entity_id == OTHER_ENTITY_ID and session.active
    # the display left ENTITY_ID: its session there is ended for it (same host
    # candidates, stale deviceId), as the TV player never sends end itself
    assert playing["cameraStreamAuthToken"] not in {
        s.key for s in runtime.sessions.sessions.values()
    }
    assert "display moved on to" in caplog.text
    # another display still watching ENTITY_ID is untouched
    elsewhere = await execute(trait)
    other_display = {
        **OFFER,
        "sdp": OFFER_SDP.replace("192.0.2.20", "192.0.2.77"),
    }
    assert (
        await client.post(
            signalling_path(elsewhere), json=other_display, headers=auth(elsewhere)
        )
    ).status == 200
    third = await execute(trait, entity_id=OTHER_ENTITY_ID)
    assert (
        await client.post(
            signalling_path(third),
            json={**OFFER, "deviceId": ENTITY_ID},
            headers=auth(third),
        )
    ).status == 200
    assert runtime.sessions.resolve(elsewhere["cameraStreamAuthToken"]).active
    # end with a mismatching deviceId still ends the token's session
    assert (
        await client.post(
            signalling_path(other),
            json={"action": "end", "deviceId": ENTITY_ID},
            headers=auth(other),
        )
    ).status == 200
    assert session.key not in runtime.sessions.sessions
    # the token of a closed session: end is quiet, an offer is refused
    assert (
        await client.post(
            signalling_path(other), json={"action": "end"}, headers=auth(other)
        )
    ).status == 200
    resp = await client.post(signalling_path(other), json=OFFER, headers=auth(other))
    assert resp.status == 403 and "token of a closed session" in caplog.text
    # grace over: unknown
    runtime.sessions.resolve(other["cameraStreamAuthToken"]).closed -= (
        TOKEN_GRACE + timedelta(seconds=1)
    )
    resp = await client.post(signalling_path(other), json=OFFER, headers=auth(other))
    assert resp.status == 403 and "unknown token" in caplog.text


async def test_codec_failure_switches_camera_off(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
    caplog,
):
    provider.mode = "codec"
    runtime = setup_integration.runtime_data
    client = await hass_client_no_auth()
    info = await execute(trait)
    resp = await client.post(signalling_path(info), json=OFFER, headers=auth(info))
    assert resp.status == 502 and (await resp.json()) == {"error": "negotiation failed"}
    assert runtime.disabled == {
        ENTITY_ID: "go2rtc_webrtc_offer_failed: streams: codecs not matched: video:H265 => video:H264,video:VP8"
    }
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{ENTITY_ID}"
    )
    assert issue is not None and not issue.is_fixable
    assert trait().sync_attributes()["cameraStreamSupportedProtocols"] == ["hls"]
    assert trait(OTHER_ENTITY_ID).sync_attributes()[
        "cameraStreamSupportedProtocols"
    ] == ["webrtc", "hls"]
    with patch(
        "homeassistant.components.google_assistant.trait.camera.async_request_stream",
        return_value="/api/hls/x",
    ):
        assert (await execute(trait))["cameraStreamProtocol"] == "hls"
    assert "Google holds a stale device list" in caplog.text
    await runtime.async_save()
    assert (await runtime.store.async_load())["disabled"] == runtime.disabled
    runtime.enable(ENTITY_ID)
    assert not runtime.disabled and trait().sync_attributes()[
        "cameraStreamSupportedProtocols"
    ] == ["webrtc", "hls"]
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_CAMERA_UNSUPPORTED_PREFIX}{ENTITY_ID}"
        )
        is None
    )


@pytest.mark.parametrize(
    ("mode", "status", "permanent"),
    [
        ("audio_only", 502, True),
        ("error", 502, False),
        ("timeout", 504, False),
        ("no_candidates", 502, False),
    ],
)
async def test_other_negotiation_failures(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
    mode,
    status,
    permanent,
):
    provider.mode = mode
    runtime = setup_integration.runtime_data
    client = await hass_client_no_auth()
    info = await execute(trait)
    with (
        patch(
            "custom_components.google_assistant_cameras_webrtc.signaling.ANSWER_TIMEOUT",
            0.2,
        ),
        patch(
            "custom_components.google_assistant_cameras_webrtc.signaling.GATHER_CAP",
            0.3,
        ),
    ):
        resp = await client.post(signalling_path(info), json=OFFER, headers=auth(info))
    assert resp.status == status
    assert (ENTITY_ID in runtime.disabled) is permanent
    assert provider.closed and not runtime.sessions.sessions


async def test_provider_error_text_is_sanitised(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
    caplog,
):
    provider.mode = "codec"
    with patch.object(
        provider,
        "async_handle_async_webrtc_offer",
        side_effect=lambda cam, offer, sid, send: send(
            __import__(
                "homeassistant.components.camera", fromlist=["WebRTCError"]
            ).WebRTCError(
                "go2rtc_webrtc_offer_failed",
                "streams: codecs not matched: rtsp://admin:pw@192.0.2.9:554/x",
            )
        ),
    ):
        info = await execute(trait)
        client = await hass_client_no_auth()
        await client.post(signalling_path(info), json=OFFER, headers=auth(info))
    assert "pw@" not in caplog.text and "192.0.2.9" not in caplog.text
    assert "<redacted>@" in setup_integration.runtime_data.disabled[ENTITY_ID]


async def test_second_offer_restarts_and_media_check_runs(
    hass: HomeAssistant,
    setup_integration,
    trait,
    hass_client_no_auth,
    provider: FakeProvider,
    caplog,
):
    client = await hass_client_no_auth()
    info = await execute(trait)
    with patch(
        "custom_components.google_assistant_cameras_webrtc.runtime.MEDIA_CHECK_DELAY",
        0.05,
    ):
        assert (
            await client.post(signalling_path(info), json=OFFER, headers=auth(info))
        ).status == 200
        first = provider.offers[0][0]
        assert (
            await client.post(signalling_path(info), json=OFFER, headers=auth(info))
        ).status == 200
        await hass.async_block_till_done(wait_background_tasks=True)
    assert provider.closed == [first] and provider.offers[1][0] != first
    assert "media check at +0 s: go2rtc not available" in caplog.text


async def test_pending_sessions_are_evicted_live_ones_never(
    hass: HomeAssistant, setup_integration, trait, hass_client_no_auth
):
    runtime = setup_integration.runtime_data
    client = await hass_client_no_auth()
    live = await execute(trait)
    assert (
        await client.post(signalling_path(live), json=OFFER, headers=auth(live))
    ).status == 200
    infos = [await execute(trait) for _ in range(6)]
    # the live one does not count: four pending stay, the two oldest went
    assert len(runtime.sessions.sessions) == 5
    assert runtime.sessions.counters["evicted"] == 2
    assert runtime.sessions.resolve(live["cameraStreamAuthToken"]).active
    assert (
        runtime.sessions.resolve(infos[0]["cameraStreamAuthToken"]).__class__.__name__
        == "ClosedToken"
    )
    assert (
        runtime.sessions.resolve(infos[2]["cameraStreamAuthToken"]).__class__.__name__
        == "Session"
    )
    now = dt_util.utcnow()
    with patch(
        "custom_components.google_assistant_cameras_webrtc.sessions.dt_util.utcnow",
        return_value=now + OFFER_WINDOW + timedelta(seconds=1),
    ):
        runtime.sessions.sweep()
    assert [s.active for s in runtime.sessions.sessions.values()] == [True]
    with patch(
        "custom_components.google_assistant_cameras_webrtc.sessions.dt_util.utcnow",
        return_value=now + SESSION_MAX_LIFETIME + timedelta(seconds=1),
    ):
        runtime.sessions.sweep()
    assert not runtime.sessions.sessions


async def test_execute_falls_back_when_our_code_raises(
    hass: HomeAssistant, setup_integration, trait, mock_request_stream, caplog
):
    with patch.object(
        setup_integration.runtime_data,
        "webrtc_execute",
        side_effect=RuntimeError("boom"),
    ):
        info = await execute(trait)
    assert (
        info["cameraStreamAccessUrl"].endswith("master_playlist.m3u8")
        and "using stock HLS" in caplog.text
    )
    hass.states.async_set("camera.ghost", "idle", {"supported_features": 2})
    trt = make_trait(hass, "camera.ghost")
    await trt.execute(GET_STREAM, {}, HUB_PARAMS, {})
    assert trt.stream_info["cameraStreamAccessUrl"].endswith("master_playlist.m3u8")
