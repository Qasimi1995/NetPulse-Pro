# Changelog

All notable changes to NetPulse Pro are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-14

### Added

- **220° Dual-Arc Speedometer Gauge** — a custom-drawn canvas instrument with a
  dark obsidian track, glowing Electric Emerald download ring and Sapphire
  upload inner ring. Logarithmic auto-ranging (100 Mbps floor, doubling up to a
  10 Gbps ceiling) keeps both casual browsing and multi-hundred-Mbps transfers
  animated without pegging the needle.
- **Live Telemetry tab** — the gauge sits beside the rolling throughput graph,
  with a live ticking clock (`YYYY-MM-DD | HH:MM:SS`) in the header and session
  peak download/upload readouts on the instrument.
- **Cycle start tracking** — the usage banner shows the exact local date and
  time the current cycle started (`Tracking since: Nov 12, 2026 09:15 AM`, or
  `All-time (Epoch)` before any reset).
- **Non-destructive usage reset** — `RESET USAGE` writes a UTC baseline to
  `settings.json` instead of deleting history, so `TOTAL MONITORED` /
  `CYCLE USAGE` restart from zero while range analytics and CSV exports keep
  every recorded byte.
- **Range Analytics & CSV export** — preset ranges (Today, Last 7 Days, This
  Month) plus custom start/end windows, GB result cards, and a one-click
  `Export to CSV` button.
- **Settings** — interface zoom, graph window duration, ping host, data quota
  with an amber/red progress bar, and Windows startup registration.
- **System tray integration** — close-to-tray, double-click restore, and a
  Start/Stop/Show/Exit menu.
- **High-DPI support** — deterministic CustomTkinter scaling pinned to the
  detected display scale, verified at 150% on Windows 11.

### Fixed

- UTC timestamp alignment: `reset_timestamp` is now written and compared in UTC
  to match SQLite `CURRENT_TIMESTAMP`, eliminating timezone-offset drift in
  tracked totals.

### Security

- Telemetry settings and the SQLite database are stored under the per-user
  `%LOCALAPPDATA%` directory; no data leaves the machine.

[1.0.0]: https://github.com/Qasimi1995/NetPulse-Pro/releases/tag/v1.0.0
