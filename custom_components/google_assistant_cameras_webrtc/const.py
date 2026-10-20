"""Constants."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "google_assistant_cameras_webrtc"
NAME = "Google Assistant WebRTC Cameras"

DATA_RUNTIME = DOMAIN
DATA_SYNC = f"{DOMAIN}_sync"
CONF_WEBHOOK_ID = "webhook_id"

STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1
# Bumped when the SYNC attributes change, so Google is re-synced after an upgrade.
SYNC_SCHEMA_VERSION = 2

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

ACTION_OFFER = "offer"
ACTION_ANSWER = "answer"
ACTION_END = "end"
MAX_BODY_BYTES = 64 * 1024
MAX_SDP_BYTES = 48 * 1024

OFFER_WINDOW = timedelta(seconds=90)
SESSION_MAX_LIFETIME = timedelta(hours=1)
SESSION_SWEEP_INTERVAL = timedelta(seconds=30)
# Tokens of closed sessions stay resolvable this long: a late "end" is answered
# quietly and a late offer gets a plain refusal instead of "unknown token" noise.
TOKEN_GRACE = timedelta(minutes=10)
MAX_PENDING_PER_CAMERA = 4
MAX_SESSIONS_TOTAL = 32

ANSWER_TIMEOUT = 10.0
GATHER_MIN = 0.4
GATHER_QUIET = 0.5
GATHER_CAP = 3.0

MEDIA_CHECK_DELAY = 8.0
GO2RTC_TIMEOUT = 3.0

SYNC_DEBOUNCE = 5.0

ISSUE_TRAIT_CHANGED = "trait_changed"
ISSUE_CORE_HAS_WEBRTC = "core_has_webrtc"
ISSUE_NO_HTTPS = "no_https"
ISSUE_SYNC_NEEDED = "sync_needed"
ISSUE_CAMERA_UNSUPPORTED_PREFIX = "camera_unsupported_"

SIGNAL_CAMERA_SYNCED = f"{DOMAIN}_camera_synced"
SIGNAL_SWITCH_STATE = f"{DOMAIN}_switch_state"
REASON_USER = "switched off by the user"

MAX_SESSION_HISTORY = 20
