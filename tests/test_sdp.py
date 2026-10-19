"""Pure SDP tests."""

from custom_components.google_assistant_cameras_webrtc import sdp

from .sdp_fixtures import ANSWER_SDP, ANSWER_SDP_AUDIO_ONLY, CANDIDATES, OFFER_SDP


def _cands(**kwargs):
    return [sdp.Candidate(c, **kwargs) for c in CANDIDATES]


def test_parse_roundtrip_crlf():
    parsed = sdp.parse(ANSWER_SDP)
    assert len(parsed.media) == 2
    assert parsed.bundle_mids == ["0", "1"]
    assert parsed.render() == ANSWER_SDP
    # LF input renders as CRLF
    assert sdp.parse(ANSWER_SDP.replace("\r\n", "\n")).render() == ANSWER_SDP


def test_merge_without_mid_goes_to_first_bundled_section():
    merged = sdp.merge_candidates(ANSWER_SDP, _cands())
    parsed = sdp.parse(merged)
    video, audio = parsed.media
    cand_lines = [line for line in video.lines if line.startswith("a=candidate:")]
    assert len(cand_lines) == len(CANDIDATES)
    assert "a=end-of-candidates" in video.lines
    assert not audio.has_candidates
    assert "a=end-of-candidates" not in audio.lines
    # candidates sit right after the ICE credentials
    idx = video.lines.index("a=ice-pwd:go2rtcpwdgo2rtcpwdgo2rtc")
    assert video.lines[idx + 1].startswith("a=candidate:")
    assert "\r\n" in merged and "\n\n" not in merged
    assert "a=ice-options:trickle" not in merged


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
    assert "a=end-of-candidates" in video.lines
    assert "a=end-of-candidates" in audio.lines


def test_merge_dedupes_and_is_idempotent():
    once = sdp.merge_candidates(ANSWER_SDP, _cands() + _cands())
    twice = sdp.merge_candidates(once, _cands())
    assert once == twice
    assert once.count("a=end-of-candidates") == 1
    assert once.count("typ srflx") == 1


def test_merge_strips_trickle_and_accepts_a_prefix():
    sdp_in = ANSWER_SDP.replace(
        "a=group:BUNDLE 0 1", "a=group:BUNDLE 0 1\r\na=ice-options:trickle"
    )
    merged = sdp.merge_candidates(sdp_in, [sdp.Candidate("a=" + CANDIDATES[0])])
    assert "trickle" not in merged
    assert f"a={CANDIDATES[0]}" in merged


def test_merge_without_bundle_adds_to_every_section():
    no_bundle = ANSWER_SDP.replace("a=group:BUNDLE 0 1\r\n", "")
    merged = sdp.merge_candidates(no_bundle, _cands()[:1])
    for section in sdp.parse(merged).media:
        assert section.has_candidates
        assert "a=end-of-candidates" in section.lines


def test_unknown_mid_is_ignored():
    merged = sdp.merge_candidates(
        ANSWER_SDP, [sdp.Candidate(CANDIDATES[0], sdp_mid="zz")]
    )
    assert "a=candidate:" not in merged


def test_inspect_answer():
    info = sdp.inspect_answer(ANSWER_SDP)
    assert info.has_video and info.has_audio
    assert info.video_codecs == ("H264",) and info.audio_codecs == ("PCMU",)
    assert info.video_ok and info.bundle and info.rtcp_mux
    assert info.embedded_candidates == 0
    audio_only = sdp.inspect_answer(ANSWER_SDP_AUDIO_ONLY)
    assert not audio_only.has_video and audio_only.has_audio and not audio_only.video_ok
    inactive = sdp.inspect_answer(
        ANSWER_SDP.replace("a=sendonly\r\nm=audio", "a=inactive\r\nm=audio")
    )
    assert not inactive.has_video
    hevc = sdp.inspect_answer(ANSWER_SDP.replace("H264/90000", "H265/90000"))
    assert hevc.has_video and not hevc.video_ok
    merged = sdp.inspect_answer(sdp.merge_candidates(ANSWER_SDP, _cands()))
    assert merged.embedded_candidates == len(CANDIDATES)


def test_offer_helpers():
    assert sdp.offer_codecs(OFFER_SDP) == (("VP8", "RTX", "H264"), ("OPUS", "PCMU"))
    assert sdp.ice_ufrag(OFFER_SDP) == "displ"
    assert sdp.candidate_addresses(OFFER_SDP) == {"192.0.2.20", "203.0.113.5"}
    assert sdp.candidate_addresses(ANSWER_SDP) == set()


def test_candidate_properties():
    cand = sdp.Candidate(CANDIDATES[2])
    assert cand.typ == "host" and cand.transport == "tcp"
    assert sdp.Candidate(CANDIDATES[3]).typ == "srflx"
    assert sdp.Candidate("  " + CANDIDATES[0] + "  ").line == f"a={CANDIDATES[0]}"
    assert sdp.Candidate("1 1 udp 1 192.0.2.1 1 typ host").line.startswith(
        "a=candidate:1 1 udp"
    )


def test_redact_hides_addresses_and_secrets():
    text = sdp.redact(
        sdp.merge_candidates(ANSWER_SDP, _cands())
        + "a=candidate:9 1 udp 1 fe80::1a2b:3c4d:5e6f:7a8b 1 typ host\r\na=candidate:8 1 udp 1 abcd-1234.local 1 typ host\r\n"
    )
    assert "192.0.2.10" not in text and "203.0.113.9" not in text
    assert "fe80::" not in text and "abcd-1234.local" not in text
    assert "go2rtcpwd" not in text and "11:22:33:44" not in text
    assert "a=ice-ufrag:gortc" in text
    assert "a=rtpmap:102 H264/90000" in text
