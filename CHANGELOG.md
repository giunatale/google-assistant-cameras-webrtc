# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-10-20

### Added

- Google Assistant: cameras Home Assistant can serve over WebRTC are advertised
  as `["webrtc", "hls"]` with `cameraStreamNeedAuthToken: true`; devices that ask
  for WebRTC get a per-request bearer token and one fixed signalling webhook per
  installation, everything else gets the stock HLS result completed with the
  fields Google requires once two protocols are listed.
- Signalling: the provider's answer is returned with every ICE candidate
  embedded (Google's players have no trickle ICE); media is negotiated one-way
  towards the display; the bearer token alone selects the camera, because
  Google's TV player sends the `deviceId` of the camera it was showing when it
  switches to another one, and that display's previous session is ended for
  it, since the player never does.
- A *WebRTC* switch for every camera entity Google can stream over WebRTC, on
  one device per camera device in this integration (named like the camera's
  device and linked to it): off means HLS only for that camera. The user flips it when WebRTC does not work
  on their devices; the integration flips it once, after a negotiation that
  cannot succeed (no video in the answer, no matching codec), with a repair
  issue that says why. The state is persisted across restarts and every flip
  asks Google to re-sync, through Nabu Casa or a self-hosted project's service
  account; a repair issue asks for a manual sync otherwise and clears itself
  when the sync arrives.
- Stand-down with repair issues when Home Assistant changes the camera trait or
  gains native WebRTC support, and when the URL handed to Google is not HTTPS.
- Debug logging of every step of a request with redacted SDPs, the camera's
  H.264 profile and go2rtc's view of the connection; diagnostics without URLs,
  tokens or addresses; English and Italian translations.
- Verified on Nest Hub, Pixel Tablet (hub mode and Google Home app), the
  Google Home app on Android and the Google TV / Android TV cast path (Sony
  Bravia, Mi Box 4).

[Unreleased]: https://github.com/giunatale/google-assistant-cameras-webrtc/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/giunatale/google-assistant-cameras-webrtc/releases/tag/v1.0.0
