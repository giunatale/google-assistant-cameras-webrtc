"""Pure SDP helpers: no Home Assistant imports, fully unit-tested.

Google's players do not support trickle ICE, so the provider's trickled
candidates are folded back into its answer. Video is negotiated one-way
(camera to display); the display's microphone track is kept only with
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
_PROFILE_LEVEL_ID = re.compile(r"profile-level-id=([0-9A-Fa-f]{6})")

DIRECTIONS = ("sendrecv", "sendonly", "recvonly", "inactive")
VIDEO_CODECS_ACCEPTED = ("H264", "VP8")
OFFER_DIRECTIONS = {"sendrecv": "recvonly", "sendonly": "inactive"}
ANSWER_DIRECTIONS = {"sendrecv": "sendonly", "recvonly": "inactive"}


@dataclass
class Candidate:
    """A trickled ICE candidate as delivered by a Home Assistant provider."""

    candidate: str
    sdp_mid: str | None = None
    sdp_m_line_index: int | None = None

    @property
    def line(self) -> str:
        """Return the SDP attribute line."""
        value = self.candidate.strip()
        if value.startswith("a="):
            value = value[2:]
        if not value.startswith("candidate:"):
            value = f"candidate:{value}"
        return f"a={value}"

    @property
    def typ(self) -> str:
        """Return host, srflx, relay... or ''."""
        match = re.search(r"\btyp (\S+)", self.candidate)
        return match.group(1) if match else ""

    @property
    def transport(self) -> str:
        """Return udp or tcp in lower case, or ''."""
        parts = self.candidate.split()
        return parts[2].lower() if len(parts) >= 3 else ""


@dataclass
class MediaSection:
    """One m= section; the first line is the m= line."""

    lines: list[str]

    @property
    def kind(self) -> str:
        """Return audio, video or application."""
        return self.lines[0][2:].split(" ", 1)[0]

    @property
    def port(self) -> int:
        """Return the m= line port (0 means rejected)."""
        parts = self.lines[0].split()
        try:
            return int(parts[1].split("/", 1)[0])
        except IndexError, ValueError:
            return 0

    @property
    def rejected(self) -> bool:
        """Return True for a section that carries no media at all."""
        return self.port == 0 and not any(
            line.strip().lower() == "a=bundle-only" for line in self.lines
        )

    @property
    def mid(self) -> str | None:
        """Return the a=mid value if present."""
        for line in self.lines:
            if line.startswith("a=mid:"):
                return line[6:].strip()
        return None

    @property
    def direction(self) -> str | None:
        """Return the explicit direction attribute if present."""
        return next((d for d in map(direction_of, self.lines) if d), None)

    @property
    def codecs(self) -> list[str]:
        """Return the codec names from rtpmap lines, upper case."""
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
        """Return the mids of the first a=group:BUNDLE line."""
        for line in self.session:
            if line.startswith("a=group:BUNDLE"):
                return line.split()[1:]
        return []

    def render(self) -> str:
        """Render with CRLF line endings and a trailing CRLF."""
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


def direction_of(line: str) -> str | None:
    """Return the direction named by an attribute line, or None."""
    value = line.strip().lower()
    if value.startswith("a=") and value[2:] in DIRECTIONS:
        return value[2:]
    return None


def _target_sections(parsed: ParsedSdp, candidate: Candidate) -> list[int]:
    """Return the media sections a candidate belongs to.

    Explicit mid or m-line index wins. Without either (go2rtc trickles bare
    candidate strings) a bundled SDP puts them in the first accepted bundled
    section; an unbundled one needs them in every accepted section.
    """
    if candidate.sdp_m_line_index is not None:
        index = candidate.sdp_m_line_index
        return [index] if 0 <= index < len(parsed.media) else []
    if candidate.sdp_mid is not None:
        return [i for i, s in enumerate(parsed.media) if s.mid == candidate.sdp_mid]
    accepted = [i for i, s in enumerate(parsed.media) if not s.rejected]
    bundle = parsed.bundle_mids
    if not bundle:
        return accepted
    for mid in bundle:
        for i in accepted:
            if parsed.media[i].mid == mid:
                return [i]
    return accepted[:1]


def merge_candidates(answer_sdp: str, candidates: list[Candidate]) -> str:
    """Return the answer with every candidate embedded and trickle disabled.

    Duplicates are dropped, ``a=end-of-candidates`` is added to each section
    that carries candidates and ``a=ice-options:trickle`` is removed.
    """
    parsed = parse(answer_sdp)
    parsed.session = [line for line in parsed.session if not _is_trickle_option(line)]
    for section in parsed.media:
        section.lines = [line for line in section.lines if not _is_trickle_option(line)]
    for candidate in candidates:
        line = candidate.line
        for index in _target_sections(parsed, candidate):
            section = parsed.media[index]
            if line not in section.lines:
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
    """Insert after the last candidate, else after the ICE credentials."""
    last_candidate = last_ice = -1
    for index, existing in enumerate(lines):
        if existing.startswith("a=candidate:"):
            last_candidate = index
        elif existing.startswith(("a=ice-ufrag:", "a=ice-pwd:", "a=ice-options:")):
            last_ice = index
    position = last_candidate + 1 if last_candidate >= 0 else last_ice + 1
    if position <= 0:
        position = len(lines)
    return [*lines[:position], line, *lines[position:]]


def map_directions(
    sdp: str, mapping: dict[str, str], kinds: tuple[str, ...] | None = None
) -> str:
    """Give every accepted media section one explicit, mapped direction.

    A section without a direction inherits the session-level one, else
    ``sendrecv`` (RFC 8866); session-level direction lines are dropped and
    rejected sections are left alone. ``kinds`` limits the mapping to those
    media kinds (the others only get their direction made explicit). Returns
    the input unchanged when nothing needs to change.
    """
    parsed = parse(sdp)
    original = parsed.render()
    session_direction = next(
        (d for d in map(direction_of, parsed.session) if d), "sendrecv"
    )
    parsed.session = [line for line in parsed.session if direction_of(line) is None]
    for section in parsed.media:
        if section.rejected:
            continue
        current = section.direction or session_direction
        mapped = (
            mapping.get(current, current)
            if kinds is None or section.kind in kinds
            else current
        )
        target = f"a={mapped}"
        lines: list[str] = []
        placed = False
        for line in section.lines:
            if direction_of(line) is None:
                lines.append(line)
            elif not placed:
                lines.append(target)
                placed = True
        if not placed:
            lines.append(target)
        section.lines = lines
    rendered = parsed.render()
    return sdp if rendered == original else rendered


def one_way_offer(offer_sdp: str) -> str:
    """Make a display's offer receive-only: its microphone track is declined."""
    return map_directions(offer_sdp, OFFER_DIRECTIONS)


def one_way_answer(answer_sdp: str) -> str:
    """Make a provider answer send-only towards the display."""
    return map_directions(answer_sdp, ANSWER_DIRECTIONS)


def directions(sdp: str) -> list[str]:
    """Return "<kind> <direction>" per media section, for logs."""
    return [
        f"{s.kind} {'rejected' if s.rejected else s.direction or 'unspecified'}"
        for s in parse(sdp).media
    ]


@dataclass(frozen=True)
class AnswerInfo:
    """What a provider answer offers to the display."""

    has_video: bool
    has_audio: bool
    video_codecs: tuple[str, ...]
    audio_codecs: tuple[str, ...]
    bundle: bool
    rtcp_mux: bool
    embedded_candidates: int

    @property
    def video_ok(self) -> bool:
        """Return True if the accepted video uses a codec Google's players decode."""
        return self.has_video and any(
            codec in VIDEO_CODECS_ACCEPTED for codec in self.video_codecs
        )


def inspect_answer(answer_sdp: str) -> AnswerInfo:
    """Summarise an answer: accepted media, codecs, bundle, embedded candidates."""
    parsed = parse(answer_sdp)
    has_video = has_audio = rtcp_mux = False
    video_codecs: list[str] = []
    audio_codecs: list[str] = []
    embedded = 0
    for section in parsed.media:
        sending = not section.rejected and section.direction not in (
            "inactive",
            "recvonly",
        )
        embedded += sum(1 for line in section.lines if line.startswith("a=candidate:"))
        rtcp_mux = rtcp_mux or "a=rtcp-mux" in section.lines
        if section.kind == "video" and sending:
            has_video = True
            video_codecs.extend(section.codecs)
        elif section.kind == "audio" and sending:
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
    """Return (video codecs, audio codecs) a remote offer accepts."""
    video: list[str] = []
    audio: list[str] = []
    for section in parse(offer_sdp).media:
        if section.kind == "video":
            video.extend(section.codecs)
        elif section.kind == "audio":
            audio.extend(section.codecs)
    return tuple(dict.fromkeys(video)), tuple(dict.fromkeys(audio))


def h264_profiles(sdp: str) -> tuple[str, ...]:
    """Return the H.264 profile-level-id values an offer accepts."""
    found: list[str] = []
    for section in parse(sdp).media:
        if section.kind != "video":
            continue
        h264 = {
            line[9:].partition(" ")[0]
            for line in section.lines
            if line.startswith("a=rtpmap:")
            and line.split(" ", 1)[1].split("/")[0].upper() == "H264"
        }
        for line in section.lines:
            if line.startswith("a=fmtp:"):
                payload, _sep, params = line[7:].partition(" ")
                match = _PROFILE_LEVEL_ID.search(params)
                if payload in h264 and match:
                    found.append(match.group(1).lower())
    return tuple(dict.fromkeys(found))


def candidate_addresses(sdp: str, host_only: bool = False) -> set[str]:
    """Return the connection addresses of the a=candidate lines.

    ``host_only`` keeps the host candidates: they identify the device itself,
    whereas several devices behind one NAT share their reflexive address.
    """
    addresses: set[str] = set()
    for line in _LINE_SPLIT.split(sdp):
        if line.startswith("a=candidate:"):
            parts = line.split()
            if len(parts) >= 8 and (not host_only or parts[7] == "host"):
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
    """Return provider error text safe for logs, repairs and storage."""
    text = _URL_USERINFO.sub(r"\1<redacted>@", text)
    text = _IPV4.sub("<ipv4>", text)
    text = _IPV6.sub("<ipv6>", text)
    text = _FQDN.sub("<host>", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
