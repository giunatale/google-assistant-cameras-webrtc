"""Pure SDP tests."""

from custom_components.google_assistant_cameras_webrtc import sdp

from .sdp_fixtures import ANSWER_SDP, ANSWER_SDP_AUDIO_ONLY, CANDIDATES, OFFER_SDP


def _cands(**kwargs):
    return [sdp.Candidate(c, **kwargs) for c in CANDIDATES]


def test_parse_roundtrip_crlf():
    parsed = sdp.parse(ANSWER_SDP)
    assert [s.kind for s in parsed.media] == ["video", "audio"]
    assert parsed.bundle_mids == ["0", "1"]
    assert parsed.render() == ANSWER_SDP
    assert sdp.parse(ANSWER_SDP.replace("\r\n", "\n")).render() == ANSWER_SDP


def test_merge_without_mid_goes_to_first_bundled_section():
    video, audio = sdp.parse(sdp.merge_candidates(ANSWER_SDP, _cands())).media
    assert sum(line.startswith("a=candidate:") for line in video.lines) == len(
        CANDIDATES
    )
    assert "a=end-of-candidates" in video.lines
    assert not audio.has_candidates and "a=end-of-candidates" not in audio.lines
    idx = video.lines.index("a=ice-pwd:go2rtcpwdgo2rtcpwdgo2rtc")
    assert video.lines[idx + 1].startswith("a=candidate:")


def test_merge_skips_a_rejected_first_section():
    answer = ANSWER_SDP.replace("m=video 9 UDP", "m=video 0 UDP")
    video, audio = sdp.parse(sdp.merge_candidates(answer, _cands())).media
    assert not video.has_candidates and audio.has_candidates


def test_merge_with_mid_and_index():
    merged = sdp.merge_candidates(
        ANSWER_SDP,
        [
            sdp.Candidate(CANDIDATES[0], sdp_mid="1"),
            sdp.Candidate(CANDIDATES[1], sdp_m_line_index=0),
        ],
    )
    video, audio = sdp.parse(merged).media
    assert any(CANDIDATES[1] in line for line in video.lines)
    assert any(CANDIDATES[0] in line for line in audio.lines)
    assert "a=end-of-candidates" in video.lines and "a=end-of-candidates" in audio.lines


def test_merge_is_idempotent_strips_trickle_and_handles_no_bundle():
    once = sdp.merge_candidates(ANSWER_SDP, _cands() + _cands())
    assert once == sdp.merge_candidates(once, _cands())
    assert once.count("a=end-of-candidates") == 1 and once.count("typ srflx") == 1
    with_trickle = ANSWER_SDP.replace(
        "a=group:BUNDLE 0 1", "a=group:BUNDLE 0 1\r\na=ice-options:trickle"
    )
    assert "trickle" not in sdp.merge_candidates(
        with_trickle, [sdp.Candidate("a=" + CANDIDATES[0])]
    )
    no_bundle = ANSWER_SDP.replace("a=group:BUNDLE 0 1\r\n", "")
    for section in sdp.parse(sdp.merge_candidates(no_bundle, _cands()[:1])).media:
        assert section.has_candidates and "a=end-of-candidates" in section.lines
    assert "a=candidate:" not in sdp.merge_candidates(
        ANSWER_SDP, [sdp.Candidate(CANDIDATES[0], sdp_mid="zz")]
    )


def test_inspect_answer():
    info = sdp.inspect_answer(ANSWER_SDP)
    assert (
        info.has_video
        and info.has_audio
        and info.video_ok
        and info.bundle
        and info.rtcp_mux
    )
    assert info.video_codecs == ("H264",) and info.audio_codecs == ("PCMU",)
    assert info.embedded_candidates == 0
    assert not sdp.inspect_answer(ANSWER_SDP_AUDIO_ONLY).has_video
    assert not sdp.inspect_answer(
        ANSWER_SDP.replace("a=sendonly\r\nm=audio", "a=inactive\r\nm=audio")
    ).has_video
    hevc = sdp.inspect_answer(ANSWER_SDP.replace("H264/90000", "H265/90000"))
    assert hevc.has_video and not hevc.video_ok
    assert sdp.inspect_answer(
        sdp.merge_candidates(ANSWER_SDP, _cands())
    ).embedded_candidates == len(CANDIDATES)


def test_one_way_offer_and_answer():
    offer = sdp.one_way_offer(OFFER_SDP)
    assert sdp.directions(offer) == ["video recvonly", "audio recvonly"]
    assert sdp.one_way_offer(offer) == offer
    sendrecv_answer = ANSWER_SDP.replace("a=sendonly\r\n", "a=sendrecv\r\n", 1)
    recvonly_answer = ANSWER_SDP.replace("a=sendonly\r\n", "a=recvonly\r\n")
    assert sdp.directions(sdp.one_way_answer(sendrecv_answer)) == [
        "video sendonly",
        "audio sendonly",
    ]
    silent = sdp.one_way_answer(recvonly_answer)
    assert sdp.directions(silent) == ["video inactive", "audio inactive"]
    assert sdp.one_way_answer(ANSWER_SDP) == ANSWER_SDP
    # a camera without audio: go2rtc answers audio inactive, which stays as is
    no_audio = ANSWER_SDP.replace("a=sendonly\r\n", "a=inactive\r\n").replace(
        "a=inactive\r\n", "a=sendonly\r\n", 1
    )
    answer = sdp.one_way_answer(no_audio)
    assert sdp.directions(answer) == ["video sendonly", "audio inactive"]
    assert sdp.inspect_answer(answer).has_audio is False


def test_implicit_and_session_level_directions_are_sendrecv():
    bare = "\r\n".join(
        line for line in OFFER_SDP.split("\r\n") if sdp.direction_of(line) is None
    )
    assert sdp.directions(sdp.one_way_offer(bare)) == [
        "video recvonly",
        "audio recvonly",
    ]
    session_level = bare.replace("t=0 0\r\n", "t=0 0\r\na=SENDONLY\r\n")
    rewritten = sdp.one_way_offer(session_level)
    assert sdp.directions(rewritten) == ["video inactive", "audio inactive"]
    assert "a=SENDONLY" not in rewritten
    rejected = ANSWER_SDP.replace("m=audio 9 UDP", "m=audio 0 UDP").replace(
        "a=sendonly\r\n", "a=recvonly\r\n"
    )
    assert sdp.directions(sdp.one_way_answer(rejected)) == [
        "video inactive",
        "audio rejected",
    ]


def test_offer_helpers():
    assert sdp.offer_codecs(OFFER_SDP) == (("VP8", "RTX", "H264"), ("OPUS", "PCMU"))
    assert sdp.h264_profiles(OFFER_SDP) == ("42e01f",)
    assert sdp.candidate_addresses(OFFER_SDP) == {"192.0.2.20", "203.0.113.5"}
    assert sdp.candidate_addresses(ANSWER_SDP) == set()
    cand = sdp.Candidate(CANDIDATES[2])
    assert cand.typ == "host" and cand.transport == "tcp"
    assert sdp.Candidate("1 1 udp 1 192.0.2.1 1 typ host").line.startswith(
        "a=candidate:1 1 udp"
    )


def test_redact_and_sanitize():
    text = sdp.redact(
        sdp.merge_candidates(ANSWER_SDP, _cands())
        + "a=candidate:9 1 udp 1 fe80::1a2b:3c4d:5e6f:7a8b 1 typ host\r\na=candidate:8 1 udp 1 abcd-1234.local 1 typ host\r\n"
    )
    for secret in (
        "192.0.2.10",
        "203.0.113.9",
        "fe80::",
        "abcd-1234.local",
        "go2rtcpwd",
        "11:22:33:44",
    ):
        assert secret not in text
    assert "a=ice-ufrag:gortc" in text and "a=rtpmap:102 H264/90000" in text
    reason = sdp.sanitize_reason(
        "streams: rtsp://admin:secret@192.0.2.5:554/live: dial tcp 192.0.2.5:554: i/o timeout  \n x"
        * 3
    )
    assert (
        "secret" not in reason and "192.0.2.5" not in reason and "<redacted>@" in reason
    )
    assert len(reason) <= 200 and "\n" not in reason
