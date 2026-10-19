# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- First implementation: WebRTC offered to Google Assistant (Nest displays) for
  every camera Home Assistant can serve over WebRTC, HLS kept as fallback.
- Per-request signalling sessions on Home Assistant webhooks with single-use
  256-bit secrets; non-trickle answers assembled from the provider's trickled
  candidates.
- Per-camera breaker (codec failures permanent with a retry repair, repeated
  failures 30 min with back-off), persisted across restarts, with automatic
  Google device re-sync through Nabu Casa or a service account.
- Media-flow verification through go2rtc's API.
- Repairs for every stand-down condition, diagnostics, Italian translation.
