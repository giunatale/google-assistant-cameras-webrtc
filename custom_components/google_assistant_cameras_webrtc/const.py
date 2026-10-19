"""Constants for the Google Assistant WebRTC Cameras integration."""

from __future__ import annotations

from datetime import timedelta
import logging

DOMAIN = "google_assistant_cameras_webrtc"
NAME = "Google Assistant WebRTC Cameras"

LOGGER = logging.getLogger(__package__)

# hass.data keys
DATA_RUNTIME = DOMAIN  # Runtime, present only while the entry is loaded
DATA_SYNC = f"{DOMAIN}_sync"  # SyncRequester, survives reloads

STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

# Google CameraStream trait vocabulary
TRAIT_CAMERA_STREAM = "action.devices.traits.CameraStream"
COMMAND_GET_CAMERA_STREAM = "action.devices.commands.GetCameraStream"
PROTOCOL_WEBRTC = "webrtc"
PROTOCOL_HLS = "hls"
ATTR_SUPPORTED_PROTOCOLS = "cameraStreamSupportedProtocols"
ATTR_NEED_AUTH_TOKEN = "cameraStreamNeedAuthToken"
PARAM_SUPPORTED_PROTOCOLS = "SupportedStreamProtocols"
PARAM_STREAM_TO_CHROMECAST = "StreamToChromecast"
RESULT_PROTOCOL = "cameraStreamProtocol"
RESULT_SIGNALING_URL = "cameraStreamSignalingUrl"
RESULT_AUTH_TOKEN = "cameraStreamAuthToken"
RESULT_ICE_SERVERS = "cameraStreamIceServers"
RESULT_ACCESS_URL = "cameraStreamAccessUrl"

# Signalling protocol
ACTION_OFFER = "offer"
ACTION_ANSWER = "answer"
ACTION_END = "end"
MAX_BODY_BYTES = 64 * 1024
MAX_SDP_BYTES = 48 * 1024

# Session timing
OFFER_WINDOW = timedelta(seconds=90)  # offer must arrive within this after EXECUTE
SESSION_MAX_LIFETIME = timedelta(hours=1)
SESSION_SWEEP_INTERVAL = timedelta(seconds=30)
# A closed session's URL keeps answering 403 quietly for a while, so a late
# POST from the display never hits HA's "unregistered webhook" INFO log.
TOMBSTONE_TTL = timedelta(minutes=5)
MAX_SESSIONS_PER_CAMERA = 4
MAX_SESSIONS_TOTAL = 16

# Candidate gathering (seconds)
ANSWER_TIMEOUT = 10.0
GATHER_MIN = 0.4
GATHER_QUIET = 0.5
GATHER_CAP = 3.0

# Media-flow verification (go2rtc only), seconds after the answer
MEDIA_CHECK_DELAYS = (8.0, 16.0)
MEDIA_CHECK_TIMEOUT = 5.0

# Circuit breaker
TRIP_AFTER_FAILURES = 2
TRIP_BASE = timedelta(minutes=30)
TRIP_MAX = timedelta(hours=24)

# Google device sync
SYNC_DEBOUNCE = 5.0

# Repair issue ids
ISSUE_TRAIT_CHANGED = "trait_changed"
ISSUE_CORE_HAS_WEBRTC = "core_has_webrtc"
ISSUE_NO_HTTPS = "no_https"
ISSUE_SYNC_NEEDED = "sync_needed"
ISSUE_CAMERA_UNSUPPORTED_PREFIX = "camera_unsupported_"

# Diagnostics
MAX_SESSION_HISTORY = 20
