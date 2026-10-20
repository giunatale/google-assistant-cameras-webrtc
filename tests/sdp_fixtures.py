"""SDP fixtures shaped like pion/go2rtc answers and Chromium offers.

Addresses are documentation ranges (RFC 5737) and nothing is captured from a
real network.
"""

CRLF = "\r\n"

OFFER_SDP = CRLF.join(
    [
        "v=0",
        "o=- 4611731400430051336 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0 1",
        "a=extmap-allow-mixed",
        "a=msid-semantic: WMS",
        "m=video 9 UDP/TLS/RTP/SAVPF 96 97 102",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        "a=ice-ufrag:displ",
        "a=ice-pwd:displaypwddisplaypwddisplay",
        "a=ice-options:trickle",
        "a=fingerprint:sha-256 AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99",
        "a=setup:actpass",
        "a=mid:0",
        "a=recvonly",
        "a=rtcp-mux",
        "a=rtpmap:96 VP8/90000",
        "a=rtpmap:97 rtx/90000",
        "a=rtpmap:102 H264/90000",
        "a=fmtp:102 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
        "a=candidate:1 1 udp 2130706431 192.0.2.20 50000 typ host generation 0",
        "a=candidate:2 1 udp 1694498815 203.0.113.5 50000 typ srflx raddr 192.0.2.20 rport 50000 generation 0",
        "a=end-of-candidates",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111 0",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        "a=ice-ufrag:displ",
        "a=ice-pwd:displaypwddisplaypwddisplay",
        "a=ice-options:trickle",
        "a=fingerprint:sha-256 AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99",
        "a=setup:actpass",
        "a=mid:1",
        "a=recvonly",
        "a=rtcp-mux",
        "a=rtpmap:111 opus/48000/2",
        "a=rtpmap:0 PCMU/8000",
        "",
    ]
)

ANSWER_SDP = CRLF.join(
    [
        "v=0",
        "o=- 2452190233285812300 1733000000 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        "a=msid-semantic:WMS*",
        "a=fingerprint:sha-256 11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00",
        "a=extmap-allow-mixed",
        "a=group:BUNDLE 0 1",
        "m=video 9 UDP/TLS/RTP/SAVPF 102",
        "c=IN IP4 0.0.0.0",
        "a=setup:active",
        "a=mid:0",
        "a=ice-ufrag:gortc",
        "a=ice-pwd:go2rtcpwdgo2rtcpwdgo2rtc",
        "a=rtcp-mux",
        "a=rtcp-rsize",
        "a=rtpmap:102 H264/90000",
        "a=fmtp:102 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
        "a=ssrc:1111 cname:go2rtc",
        "a=sendonly",
        "m=audio 9 UDP/TLS/RTP/SAVPF 0",
        "c=IN IP4 0.0.0.0",
        "a=setup:active",
        "a=mid:1",
        "a=ice-ufrag:gortc",
        "a=ice-pwd:go2rtcpwdgo2rtcpwdgo2rtc",
        "a=rtcp-mux",
        "a=rtcp-rsize",
        "a=rtpmap:0 PCMU/8000",
        "a=ssrc:2222 cname:go2rtc",
        "a=sendonly",
        "",
    ]
)

ANSWER_SDP_AUDIO_ONLY = ANSWER_SDP.replace(
    "m=video 9 UDP/TLS/RTP/SAVPF 102", "m=video 0 UDP/TLS/RTP/SAVPF 102"
)

CANDIDATES = [
    "candidate:4123 1 udp 2130706431 192.0.2.10 41234 typ host",
    "candidate:4124 1 udp 2130706431 198.51.100.7 41235 typ host",
    "candidate:4125 1 tcp 1518280447 192.0.2.10 18555 typ host tcptype passive",
    "candidate:4126 1 udp 1694498815 203.0.113.9 41234 typ srflx raddr 192.0.2.10 rport 41234",
]
