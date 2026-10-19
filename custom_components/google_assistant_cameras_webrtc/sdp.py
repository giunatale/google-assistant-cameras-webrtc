"""Pure SDP helpers.

Google's smart-display player does not support trickle ICE: the answer we
return must already contain every ICE candidate. Home Assistant's WebRTC
providers (go2rtc) send the answer first and the candidates afterwards, so
this module folds the trickled candidates back into the answer SDP.

Everything here is pure (no Home Assistant imports) and unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

CRLF = "\r\n"
_LINE_SPLIT = re.compile(r"\r\n|\n")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"\b(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{1,4}\b")
_FQDN = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:local|lan|home|internal)\b", re.IGNORECASE)

_URL_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE)

VIDEO_CODECS_ACCEPTED = ("H264", "VP8")


@dataclass
class Candidate:
    """A trickled ICE candidate as delivered by a Home Assistant provider."""

    candidate: str
    sdp_mid: str | None = None
    sdp_m_line_index: int | None = None

    @property
    def line(self) -> str:
        """Return the SDP attribute line for this candidate."""
        value = self.candidate.strip()
        if value.startswith("a="):
            value = value[2:]
        if not value.startswith("candidate:"):
            value = f"candidate:{value}"
        return f"a={value}"

    @property
    def typ(self) -> str:
        """Return the candidate type (host, srflx, relay, ...) or ''."""
        match = re.search(r"\btyp (\S+)", self.candidate)
        return match.group(1) if match else ""

    @property
    def transport(self) -> str:
        """Return the transport (udp/tcp) in lower case or ''."""
        parts = self.candidate.split()
        if len(parts) >= 3:
            return parts[2].lower()
        return ""


@dataclass
class MediaSection:
    """One m= section of an SDP."""

    lines: list[str]  # first line is the m= line

    @property
    def kind(self) -> str:
        """Return the media kind (audio/video/application)."""
        return self.lines[0][2:].split(" ", 1)[0]

    @property
    def port(self) -> int:
        """Return the port of the m= line (0 means rejected)."""
        parts = self.lines[0].split()
        try:
            return int(parts[1].split("/", 1)[0])
        except IndexError, ValueError:
            return 0

    @property
    def mid(self) -> str | None:
        """Return the a=mid value if present."""
        for line in self.lines:
            if line.startswith("a=mid:"):
                return line[6:].strip()
        return None

    @property
    def direction(self) -> str | None:
        """Return the media direction attribute if present."""
        for line in self.lines:
            if line in ("a=sendonly", "a=recvonly", "a=sendrecv", "a=inactive"):
                return line[2:]
        return None

    @property
    def codecs(self) -> list[str]:
        """Return the codec names from rtpmap lines."""
        names: list[str] = []
        for line in self.lines:
            if line.startswith("a=rtpmap:"):
                rest = line.split(" ", 1)
                if len(rest) == 2:
                    names.append(rest[1].split("/")[0].upper())
        return names

    @property
    def has_candidates(self) -> bool:
        """Return True if the section carries candidate lines."""
        return any(line.startswith("a=candidate:") for line in self.lines)


@dataclass
class ParsedSdp:
    """An SDP split into its session part and media sections."""

    session: list[str]
    media: list[MediaSection] = field(default_factory=list)

    @property
    def bundle_mids(self) -> list[str]:
        """Return the mids listed in the first a=group:BUNDLE line."""
        for line in self.session:
            if line.startswith("a=group:BUNDLE"):
                return line.split()[1:]
        return []

    def render(self) -> str:
        """Render back to text with CRLF line endings and a trailing CRLF."""
        lines = [*self.session]
        for section in self.media:
            lines.extend(section.lines)
        return CRLF.join(lines) + CRLF


def parse(sdp: str) -> ParsedSdp:
    """Split an SDP into session and media sections (line endings normalised)."""
    lines = [line.rstrip("\r") for line in _LINE_SPLIT.split(sdp)]
    while lines and lines[-1] == "":
        lines.pop()
    parsed = ParsedSdp(session=[])
    current: MediaSection | None = None
    for line in lines:
        if line.startswith("m="):
            current = MediaSection(lines=[line])
            parsed.media.append(current)
        elif current is None:
            parsed.session.append(line)
        else:
            current.lines.append(line)
    return parsed


def _target_sections(parsed: ParsedSdp, candidate: Candidate) -> list[int]:
    """Return the indexes of the media sections a candidate belongs to."""
    if candidate.sdp_m_line_index is not None:
        if 0 <= candidate.sdp_m_line_index < len(parsed.media):
            return [candidate.sdp_m_line_index]
        return []
    if candidate.sdp_mid is not None:
        for index, section in enumerate(parsed.media):
            if section.mid == candidate.sdp_mid:
                return [index]
        return []
    # No mid/index (go2rtc trickles bare candidate strings): with BUNDLE every
    # media section shares the first bundled transport, so that section carries
    # the candidates; without BUNDLE each section needs them.
    bundle = parsed.bundle_mids
    if bundle:
        for index, section in enumerate(parsed.media):
            if section.mid == bundle[0]:
                return [index]
        return [0] if parsed.media else []
    return list(range(len(parsed.media)))


def merge_candidates(answer_sdp: str, candidates: list[Candidate]) -> str:
    """Return the answer SDP with all candidates embedded (no trickle).

    - candidates go into the media section they belong to (mid / m-line index),
      or into the first bundled section when the provider gave neither;
    - duplicates are dropped;
    - ``a=end-of-candidates`` is added to every section that has candidates;
    - ``a=ice-options:trickle`` is removed at session and media level;
    - line endings are normalised to CRLF.
    A provider answer that already embeds candidates is left intact.
    """
    parsed = parse(answer_sdp)
    parsed.session = [line for line in parsed.session if not _is_trickle_option(line)]
    for section in parsed.media:
        section.lines = [line for line in section.lines if not _is_trickle_option(line)]

    for candidate in candidates:
        line = candidate.line
        for index in _target_sections(parsed, candidate):
            section = parsed.media[index]
            if line in section.lines:
                continue
            section.lines = _insert_candidate(section.lines, line)

    for section in parsed.media:
        if section.has_candidates and "a=end-of-candidates" not in section.lines:
            section.lines = _insert_candidate(section.lines, "a=end-of-candidates")
    return parsed.render()


def _is_trickle_option(line: str) -> bool:
    return (
        line.startswith("a=ice-options:") and "trickle" in line.split(":", 1)[1].split()
    )


def _insert_candidate(lines: list[str], line: str) -> list[str]:
    """Insert a candidate/end-of-candidates line after the last candidate line.

    If the section has no candidate yet, insert right after the ICE credential
    lines so the result reads like a browser-generated complete SDP.
    """
    last_candidate = -1
    last_ice = -1
    for index, existing in enumerate(lines):
        if existing.startswith("a=candidate:"):
            last_candidate = index
        elif existing.startswith(("a=ice-ufrag:", "a=ice-pwd:", "a=ice-options:")):
            last_ice = index
    if last_candidate >= 0:
        position = last_candidate + 1
    elif last_ice >= 0:
        position = last_ice + 1
    else:
        position = len(lines)
    return [*lines[:position], line, *lines[position:]]


@dataclass(frozen=True)
class AnswerInfo:
    """What a provider answer actually offers to the display."""

    has_video: bool
    has_audio: bool
    video_codecs: tuple[str, ...]
    audio_codecs: tuple[str, ...]
    bundle: bool
    rtcp_mux: bool
    embedded_candidates: int

    @property
    def video_ok(self) -> bool:
        """Return True if an accepted video section uses a codec Google plays."""
        return self.has_video and any(
            codec in VIDEO_CODECS_ACCEPTED for codec in self.video_codecs
        )


def inspect_answer(answer_sdp: str) -> AnswerInfo:
    """Summarise an answer: accepted media, codecs, bundle, candidates."""
    parsed = parse(answer_sdp)
    has_video = False
    has_audio = False
    video_codecs: list[str] = []
    audio_codecs: list[str] = []
    rtcp_mux = False
    embedded = 0
    for section in parsed.media:
        accepted = section.port != 0 and section.direction not in (
            "inactive",
            "recvonly",
        )
        embedded += sum(1 for line in section.lines if line.startswith("a=candidate:"))
        if "a=rtcp-mux" in section.lines:
            rtcp_mux = True
        if section.kind == "video" and accepted:
            has_video = True
            video_codecs.extend(section.codecs)
        elif section.kind == "audio" and accepted:
            has_audio = True
            audio_codecs.extend(section.codecs)
    return AnswerInfo(
        has_video=has_video,
        has_audio=has_audio,
        video_codecs=tuple(dict.fromkeys(video_codecs)),
        audio_codecs=tuple(dict.fromkeys(audio_codecs)),
        bundle=bool(parsed.bundle_mids),
        rtcp_mux=rtcp_mux,
        embedded_candidates=embedded,
    )


def offer_codecs(offer_sdp: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (video codecs, audio codecs) a remote offer is willing to receive."""
    parsed = parse(offer_sdp)
    video: list[str] = []
    audio: list[str] = []
    for section in parsed.media:
        if section.kind == "video":
            video.extend(section.codecs)
        elif section.kind == "audio":
            audio.extend(section.codecs)
    return tuple(dict.fromkeys(video)), tuple(dict.fromkeys(audio))


def ice_ufrag(sdp: str) -> str | None:
    """Return the first a=ice-ufrag value of an SDP."""
    for line in _LINE_SPLIT.split(sdp):
        if line.startswith("a=ice-ufrag:"):
            return line[12:].strip()
    return None


def candidate_addresses(sdp: str) -> set[str]:
    """Return the connection addresses of all a=candidate lines in an SDP."""
    addresses: set[str] = set()
    for line in _LINE_SPLIT.split(sdp):
        if line.startswith("a=candidate:"):
            parts = line.split()
            if len(parts) >= 5:
                addresses.add(parts[4])
    return addresses


def redact(sdp: str) -> str:
    """Return an SDP safe for debug logs: no addresses, no ICE password."""
    out: list[str] = []
    for line in _LINE_SPLIT.split(sdp):
        if line.startswith("a=ice-pwd:"):
            line = "a=ice-pwd:<redacted>"
        elif line.startswith("a=fingerprint:"):
            line = line.split(" ", 1)[0] + " <redacted>"
        line = _IPV4.sub("<ipv4>", line)
        line = _IPV6.sub("<ipv6>", line)
        line = _FQDN.sub("<host>", line)
        out.append(line)
    return "\n".join(out)


def sanitize_reason(text: str, limit: int = 200) -> str:
    """Return provider error text safe for logs, repairs and storage.

    Strips URL credentials and IP/host addresses, collapses whitespace and
    caps the length.
    """
    text = _URL_USERINFO.sub(r"\1<redacted>@", text)
    text = _IPV4.sub("<ipv4>", text)
    text = _IPV6.sub("<ipv6>", text)
    text = _FQDN.sub("<host>", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
