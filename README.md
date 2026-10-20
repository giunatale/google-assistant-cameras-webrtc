<p align="center">
  <img src="https://raw.githubusercontent.com/giunatale/google-assistant-cameras-webrtc/main/custom_components/google_assistant_cameras_webrtc/brand/logo.png" alt="Google Assistant WebRTC Cameras" width="420">
</p>

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/giunatale/google-assistant-cameras-webrtc/actions/workflows/validate.yml/badge.svg)](https://github.com/giunatale/google-assistant-cameras-webrtc/actions/workflows/validate.yml)
[![Tests](https://github.com/giunatale/google-assistant-cameras-webrtc/actions/workflows/tests.yml/badge.svg)](https://github.com/giunatale/google-assistant-cameras-webrtc/actions/workflows/tests.yml)

A Home Assistant custom integration that makes your cameras play over **WebRTC** on Google devices — Nest displays, the Google Home app, Google TV — instead of HLS.

Stock Home Assistant only offers Google the HLS stream: 15–20 seconds to the first picture and a similar delay behind live, on every device and for every camera brand. Google's players also support WebRTC, and every camera with a WebRTC player in Home Assistant (anything the built-in go2rtc serves — most RTSP cameras) can deliver it. This integration tells Google about it and does the signalling. HLS stays the fallback for whatever asks for it.

**Install = on.** The only setting is a per-camera switch to turn WebRTC off where it does not work for you. Remove the integration and Google Assistant goes back to stock behaviour.

## How it works

```
"Hey Google, show the gate"
        │
        ▼
Google cloud ── EXECUTE GetCameraStream ──▶ Home Assistant (google_assistant / Nabu Casa)
                                               │ camera trait extended by this integration
                                               │ WebRTC requested → per-request token + the signalling URL
                                               │ anything else     → stock HLS
                                               ▼
Google player ── POST offer (Bearer token) ──▶ /api/webhook/<id>
                                               │ the camera's WebRTC provider (go2rtc) answers;
                                               │ the trickled ICE candidates are folded into the answer
             ◀── {action: answer, sdp} ─────────┘
Google player ◀═══ video/audio over ICE/DTLS/SRTP, straight from go2rtc on your LAN ═══
```

1. On every Google **SYNC**, cameras Home Assistant can serve over WebRTC are advertised as `["webrtc", "hls"]` with `cameraStreamNeedAuthToken: true`. Google picks the first protocol the target supports.
2. When a device asks for WebRTC, the integration returns a 256-bit token for that request and the signalling URL — one webhook, fixed per installation, created when the integration is added.
3. The player posts its offer with the token. The camera's WebRTC provider inside Home Assistant produces the answer; since Google's players do not support trickle ICE, the candidates are collected and returned inside the answer. Media is negotiated one-way towards the display; the microphone track every Google player offers is declined.
4. Media flows directly between the device and go2rtc.
5. Each camera has a **WebRTC** switch (see [Configuration](#configuration)); off means Google is told HLS only for that camera.

## Requirements

- **Home Assistant 2026.10 or newer** (developed and tested on 2026.10 with HA OS). The integration verifies the internals it relies on at startup and stands down with a repair issue if a future release changes them.
- **Google Assistant linked** through **Nabu Casa** or a **self-hosted project** (the `google_assistant:` YAML integration). Both use the same code path. For automatic device syncs a self-hosted project needs `service_account:` (what enables `google_assistant.request_sync` and report state); without it a repair issue asks you to say "Hey Google, sync my devices" whenever the advertised cameras change.
- **Cameras with WebRTC in Home Assistant.** If the camera's more-info dialog plays WebRTC (the default with go2rtc for RTSP cameras), it qualifies. Cameras without WebRTC keep HLS, untouched.
- **Video Google's players accept**: H.264 (Baseline, Main and High verified) or VP8, **at least 480p** and at most 1080p. HEVC streams fail at the first attempt and switch the camera to HLS; use an H.264 stream. Audio is played if it is Opus, G.711 PCMU or G.722; other codecs (AAC is common) give video without sound.
- **An audio track, for the Google Home app.** The app's WebRTC player shows no video until a remote audio track arrives (verified with logcat: the stream is decoded and rendered, the player never leaves its loading state). Nest displays and Google TV don't care. A camera with no microphone at all needs a silent audio track added where its stream is produced (the camera integration, or a go2rtc source), after which the app plays it like any other.
- **An HTTPS URL for Google.** Home Assistant hands Google the same URL it uses for HLS (Settings → System → Network). It must be `https://` with a certificate the device trusts — the requirement casting a camera already has. Without HTTPS the integration keeps stock behaviour and raises a repair issue.
- **A network path for media**: the device connects to the Home Assistant host's LAN addresses on random UDP ports (plus TCP 18555 for the built-in go2rtc).

## Google devices

| Device | Protocol | Verified behaviour |
| --- | --- | --- |
| Nest Hub (1st and 2nd gen), Nest Hub Max | **WebRTC** | near real time; `cameraStreamNeedAuthToken: true` is what makes the Hub's player negotiate |
| Pixel Tablet | **WebRTC** | hub mode behaves like a Nest Hub; the Google Home app on it behaves like the phone app |
| Google Home app on Android | **WebRTC** | requests streams for every visible camera when the Cameras page opens, and retries while a slow camera wakes |
| Google TV / Android TV (Cast, `StreamToChromecast: true`) | **WebRTC** | Google asks for WebRTC on this path too; when it switches cameras while one plays, its player sends the new request's token with the `deviceId` of the camera it was showing and never ends the previous stream, so the token decides which camera is served and the previous session of that display is ended for it. A Sony Bravia (Android TV) shows a black screen on the third camera in a row even though media arrives, however long the gaps; a Mi Box 4 plays any sequence. Going back to the home screen between cameras resets the Bravia |
| Chromecast with Google TV | — | Google reports camera streaming unsupported on the device itself |
| Anything else | what it asks for | HLS, as stock, when WebRTC is not requested |

## Installation

1. HACS → Integrations → ⋮ → **Custom repositories** → add `https://github.com/giunatale/google-assistant-cameras-webrtc` as an **Integration**.
2. Download **Google Assistant WebRTC Cameras**, restart Home Assistant.
3. Settings → Devices & services → **Add integration** → Google Assistant WebRTC Cameras. Confirm.
4. A Google device sync is requested (automatic with Nabu Casa or a service account); otherwise a repair issue asks you to say **"Hey Google, sync my devices"**.

## Configuration

Home Assistant keeps devices per integration, so the switches cannot live on your cameras' own devices. Instead this integration has **one device per camera device**, named like it and listed under *Linked devices* on both sides, carrying **one switch per camera entity** Google exposes and Home Assistant can serve over WebRTC: *WebRTC* when the device has one camera entity, *WebRTC for &lt;stream&gt;* when it has several (`switch.<device>_webrtc_for_<stream>`). Switches are on by default and go away with their camera entity, when it is disabled, or when it stops being exposed to Google (the next Google SYNC tells).

**The switch**

- **Off** tells Google the camera supports HLS only, on every Google device. Use it when WebRTC does not work for a camera on your devices, whatever the reason: Google's players want H.264 or VP8 between 480p and 1080p, and the HLS path has no such limits.
- The integration turns a switch off by itself in exactly one case: Google's player could not negotiate the camera's video at all (an answer without video, or go2rtc reporting no matching codec). A repair issue says why; the switch's `reason` attribute carries the same text.
- **On** again is the retry: after changing the camera's stream, turn the switch on.
- Every change is persisted and asks Google to re-sync its device list (automatic with Nabu Casa or a service account; otherwise the repair issue asks you to say **"Hey Google, sync my devices"**).

Cameras Home Assistant streams only over HLS, and cameras not exposed to Google Assistant, have no switch. The switches are ordinary Home Assistant entities: do not expose them to Google Assistant.

## Verifying it works

Enable debug logging for the integration:

```yaml
logger:
  logs:
    custom_components.google_assistant_cameras_webrtc: debug
```

or temporarily with the `logger.set_level` action. At the default level the integration is quiet: INFO when it activates or restores the stock trait, WARNING when a negotiation fails, a camera's switch is turned off by the integration or it stands down. At DEBUG one request reads:

```
SYNC camera.gate: advertising ['webrtc', 'hls']
EXECUTE camera.gate: target wants ['webrtc'] (StreamToChromecast=False) -> WebRTC session 7c1d20aa (1 STUN server(s) passed)
session 7c1d20aa: offer received for camera.gate (origin header: present, 5559 bytes), display accepts video [...] (H264 profile-level-id ['42001f', '42e01f']), audio [...]
session 7c1d20aa: offer directions ['audio sendrecv', 'video recvonly'] -> ['audio recvonly', 'video recvonly']
session 7c1d20aa: answer from provider in 251 ms, 10 candidates gathered in 560 ms {'host/udp': 4, 'host/tcp': 4, 'srflx/udp': 2}, video ['H264'], audio ['OPUS', 'PCMU', 'PCMA'], bundle=True rtcp-mux=True
session 7c1d20aa: camera stream: H264 Main level 3.1
session 7c1d20aa: media check at +8 s: connected, 371605 bytes sent, remote address in offer
session 7c1d20aa: end received for camera.gate; go2rtc consumer: connected, 1140086 bytes sent, remote address in offer
session 7c1d20aa: closed (end)
```

The offer and answer SDPs are logged too, with addresses and ICE secrets redacted. Tokens are never logged. The media check and the end line come from go2rtc's API and are informational.

**Diagnostics** (Settings → Devices & services → the integration → ⋮ → Download diagnostics) show the patch state, which Google link was found, every camera with the reason it is or isn't offered WebRTC, the cameras switched off and why, counters and the last 20 sessions with timings and outcomes. No URLs, hostnames, tokens or addresses are included.

## Fallbacks and stand-downs

| Situation | Behaviour |
| --- | --- |
| The device doesn't ask for WebRTC | stock HLS, per request |
| Camera has no WebRTC in Home Assistant | advertised as stock, HLS |
| HA's URL for Google is not HTTPS | all cameras stock; repair issue *WebRTC for Google Assistant needs an HTTPS URL*, clears itself once fixed |
| Google's players cannot decode the camera's video | one failed attempt, then the camera's switch goes off (HLS); repair issue *&lt;camera&gt;: WebRTC switched off*; Google is re-synced |
| WebRTC does not work for a camera on your devices | turn the camera's *WebRTC* switch off ([Configuration](#configuration)); Google is re-synced |
| Google asks for WebRTC for a camera whose switch is off (stale SYNC) | HLS anyway, and a device sync is requested |
| Home Assistant changed the camera trait | integration inactive; repair issue *is inactive* |
| Home Assistant gained native WebRTC for Google | integration inactive; repair issue *is no longer needed* |
| Device sync could not be requested | repair issue *Say "Hey Google, sync my devices"*; clears when the sync arrives |

There is no other heuristic: WebRTC for a camera either works or it doesn't, what changes that (its codec, its resolution, your network) is nothing the integration can learn by counting failures, and the switch is the knob for your judgement.

## Troubleshooting

- **Nothing changed, the device still takes 15 s.** Look for `SYNC camera.xxx: advertising ['webrtc', 'hls']`. `stock protocols` carries the reason. No SYNC lines at all: Google hasn't synced since the install — "Hey Google, sync my devices".
- **EXECUTE shows `target wants ['progressive_mp4', 'hls', ...]`.** Google chose its Cast path for that device, or Google's device list still says HLS-only for that camera (a stale SYNC after its switch went off); a re-sync is requested automatically when the integration notices.
- **Offer received, answer returned, no picture.** Check `media check at +8 s`: *not connected* means the device never reached go2rtc's candidates (routing or a firewall between the device and Home Assistant). *connected* with bytes flowing and still no picture: compare the camera's stream (`camera stream: H264 ...`) with what the device accepts (`display accepts video ...`), and check the resolution: Google's players need 480p to 1080p, and a stream outside that range gives a black picture with media flowing.
- **`negotiation ... failed: ... codecs not matched` or `answer carries no video`.** The camera streams HEVC (or VP9/AV1). Use an H.264 stream, then turn the camera's switch back on.
- **The first picture takes a few seconds.** The camera's keyframe interval: WebRTC decoders start on a keyframe. Shorten the I-frame interval in the camera if it allows it.
- **Video but no audio.** The camera's audio codec isn't one Google's players take (AAC is the usual case). Switch the camera to G.711 if it can.
- **The Google Home app shows black for one camera that plays everywhere else.** Seen with two cameras whose stream has no audio track at all (the Hub and Google TV play them). Enable audio on the camera's stream if it has a microphone.
- **Google HomeGraph returns 429** when a sync is requested: too many sync requests in a short time; the next one succeeds. A repair issue appears until then.
- **Something looks wrong and you want stock back right now.** Remove the integration; it requests a final sync.

## Limitations

- One-way media, no talk-back. Google's players offer a microphone track and the integration declines it; the Google Home app's talk button stays disabled for these cameras even when the microphone is accepted (verified), and Nest displays and Google TV have no talk control at all.
- Google decides which devices use WebRTC (the table above) and chooses the protocol at SYNC time; a camera whose switch is off stays on HLS on all devices until you turn it on.
- First-frame latency is bounded by the camera's keyframe interval.
- The Google Home app needs an audio track in the stream (see Requirements); a Sony Bravia's Cast receiver stops rendering after two consecutive camera switches (see the device table). Both are on Google's side of the connection.
- The media check and the stream profile in the log require go2rtc as the provider (the default).
- This integration extends a class inside Home Assistant's `google_assistant` integration at runtime; there is no official hook for this. It verifies the exact shape it expects before touching anything, restores it on unload, and stands down with a repair issue if the class changes. Updating Home Assistant is always safe.

## Security and privacy

- One signalling URL per installation (`/api/webhook/<256-bit random id>`), created when the integration is added; it accepts only POST and only with a bearer token.
- Each stream request gets a fresh 256-bit bearer token bound to one camera, valid 90 s for the offer and until the player ends the stream (one hour at most). The token alone selects the camera; the `deviceId` in a signalling request is never trusted for that (Google's TV player sends a stale one when it switches cameras; it is only used, together with the display's own host candidates, to end that display's previous session). The token of a closed session is refused for ten minutes, then unknown.
- The endpoint never reveals whether a token exists, caps bodies at 64 KB and SDPs at 48 KB, and hands the provider the display's offer only after making it receive-only.
- Only STUN server URLs are passed to Google (`cameraStreamIceServers`); TURN credentials registered in Home Assistant never leave your instance. Media stays on your network.
- Logs of this integration never contain tokens, addresses or hostnames (provider error texts are sanitised before they are logged or stored). Home Assistant's own `homeassistant.components.webhook` and `homeassistant.components.http` loggers print webhook ids and client addresses when *they* are set to debug; leave those at their default level.

## Uninstall

Settings → Devices & services → Google Assistant WebRTC Cameras → Delete. The stock trait is restored, open sessions are closed, stored state is deleted and a device sync is requested so Google goes back to HLS. If the sync can't be requested, a notification asks you to say "Hey Google, sync my devices".

## FAQ

**Does it work with Nabu Casa?** Yes. Nabu Casa's Google Assistant uses the same camera trait; the signalling URL is whatever URL Home Assistant already hands Google for HLS, and media goes directly between the device and go2rtc on your LAN.

**Does it change what the Home Assistant frontend does?** No. Only what Google is told and the signalling for Google's players.

**Why a webhook and not a dedicated API endpoint?** Google's players run as web pages, so the endpoint must answer CORS preflights from `https://www.gstatic.com` and accept an `Authorization` header. Home Assistant's webhook endpoint already does exactly that.

**Can I pick which cameras get WebRTC?** Yes: each camera's *WebRTC* switch. Cameras that shouldn't reach Google at all are handled by Google Assistant's own expose settings, as before.

## Contributing

Issues and pull requests are welcome. Please attach the diagnostics download and a debug log excerpt (they contain no secrets) to bug reports. Tests run with `pip install -r requirements_test.txt && pytest`; `ruff check` and `ruff format` keep the style.

## Trademarks and credits

This is an independent, community project. It is not affiliated with, endorsed by or sponsored by Google LLC, the WebRTC project, Open Home Foundation / Home Assistant, or Nabu Casa. Google, Google Assistant, Google Home, Nest and Chromecast are trademarks of Google LLC; Home Assistant is a trademark of the Open Home Foundation; WebRTC is a name and mark of its respective owners. They are used here only to say what this integration works with. The WebRTC logo mark in the icon and logo is © The WebRTC project authors; the license of the WebRTC project's distribution is reproduced in [`brand/NOTICE`](custom_components/google_assistant_cameras_webrtc/brand/NOTICE), and use of the mark implies no endorsement by Google or the WebRTC project.

## License

MIT — see [LICENSE](LICENSE). This covers the code and the brand images in `custom_components/google_assistant_cameras_webrtc/brand/`, except the WebRTC mark they contain, which stays with its owners (see above).
