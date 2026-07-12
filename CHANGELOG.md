# Changelog

Notable changes to DMRlink3. This is the first tagged release; it establishes a
baseline of the current state rather than enumerating the project's full history.

## [3.0.0] — 2026-07-12

First tagged release. Highlights of the current state:

### Reporting & dashboard (event-driven overhaul)
- The daemon→dashboard feed is event-driven newline-delimited JSON (the original
  dmrlink pickle/binary-opcode protocol is not implemented). IPSC peer
  connect/disconnect and live call events are pushed **as they happen**; the full
  config/bridge state is resent only as a slow periodic resync + heartbeat.
- Per-peer **ping-loss** quality metric — surfaces a peer that stays connected but
  drops keepalives, over a sliding window (`PING_LOSS_WINDOW` / `PING_LOSS_WARN` in
  `[GLOBAL]`); the dashboard golds the row at/above the warn threshold.
- Canonical event vocabulary: `peer_connected` / `peer_disconnected`.
- **Unix-socket transport** for a same-host dashboard (`REPORT_TRANSPORT=unix` +
  `REPORT_SOCKET`; dashboard side `DMRLINK_TRANSPORT` / `DMRLINK_SOCKET`), retiring
  the silently-severed-link failure class for local dashboards. Remote (TCP)
  dashboards gain TCP keepalive + a feed read-timeout so a dead link is detected.
- Modern FastAPI + WebSocket web dashboard with a live call log, per-system peer
  tables, TRUNK endpoints, and a `?demo` mode.

### Documentation
- README and INSTALL document the Unix-socket feed and the ping-loss settings.

[3.0.0]: https://github.com/n0mjs710/dmrlink3/releases/tag/v3.0.0
