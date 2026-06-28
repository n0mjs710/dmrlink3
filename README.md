## DMRlink3 — Python 3 / asyncio IPSC Network Bridge

**PURPOSE:** Connect multiple Motorola MOTOTRBO IPSC (IP Site Connect) networks together, bridge
talkgroup traffic between them, and provide a live web dashboard for monitoring.
DMRlink3 is also useful for understanding and troubleshooting IPSC, or as a
starting point for writing custom IPSC applications.

**ORIGIN:** DMRlink3 is a Python 3 / asyncio port of
[dmrlink](https://github.com/n0mjs710/dmrlink) (Python 2 / Twisted), aligned
in style with the [HBlink3](https://github.com/n0mjs710/hblink3) 2026_cleanup
branch. Only the core files and the conference-bridge application are carried
forward; the retired sample applications are not included.

---

### Protocol Background

**IMPACT:** Potential concern from Motorola Solutions, as IPSC is a proprietary
protocol.

**METHOD:** Reverse engineering by pattern matching and process of elimination.

**PROPERTY:**
This work represents the author's interpretation of the Motorola™ MOTOTRBO™
IPSC protocol. It is intended for academic purposes and not for commercial gain.
It is not guaranteed to work, or be useful in any way, though it is intended to
help IPSC users better understand, and thus maintain and operate, IPSC networks.
This work is not affiliated with Motorola Solutions™, Inc. in any way. Motorola,
Motorola Solutions, MOTOTRBO, IPSC and other terms in this document are
registered trademarks of Motorola Solutions, Inc. Other registered trademark
terms may be used. These are owned and held by their respective owners.

**PRE-REQUISITE KNOWLEDGE:**
This document assumes the reader is familiar with the concepts presented in the
Motorola Solutions™, Inc. MOTOTRBO™ Systems Planner.

---

### How IPSC Works

IPSC is a **full-mesh peer-to-peer** protocol, not a client/server protocol.
Every device — including the "master" — is functionally a peer. The master peer
differs only in that it coordinates joins from newly registering devices and
distributes the authoritative peer list. Once a new peer has registered and
received the peer list, it establishes direct UDP connections to every other
peer and exchanges keep-alives and traffic with each of them independently.

This means:
- Voice and data packets flow **directly between peers**, not through the master.
- Every peer tracks keep-alive state for every other peer, not just the master.
- Two DMRlink3 instances connected to each other with no other peers form a
  full-duplex trunk and can carry as many simultaneous packet streams as they
  have bandwidth for.

**CONVENTIONS:**
`PEER → MASTER` denotes communication from the peer to the master. The initiator
of each exchange is always shown on the left.

---

### How to Use This Software

`dmrlink.py` is the IPSC protocol engine and the prerequisite for everything
else. It handles registration, keep-alives, peer-list management, and packet
dispatch. On its own it only logs traffic; applications are written by
subclassing the `IPSC` class and overriding the packet-type callbacks.

**Always verify that `dmrlink.py` runs and connects cleanly before working with
application files.** Set `LOG_LEVEL: DEBUG` and watch the output.

`bridge.py` is the conference-bridge router. It subclasses `IPSC` and bridges
voice traffic between IPSC networks according to a rule file (`bridge_rules.py`).

The `dashboard/` directory contains a FastAPI web server (`server.py`) that
connects to dmrlink3's NDJSON reporting feed and serves a live monitoring UI
over WebSocket.

---

### Files

| File | Purpose |
|------|---------|
| `dmrlink.py` | IPSC protocol engine; base class for all applications |
| `bridge.py` | Conference-bridge router (the primary application) |
| `const.py` | IPSC packet-type constants and bitmasks |
| `config.py` | `.cfg` file parser |
| `log.py` | Logging configuration |
| `dmrlink-SAMPLE.cfg` | Main configuration template — copy to `dmrlink.cfg` |
| `bridge_rules_SAMPLE.py` | Bridge rule template — copy to `bridge_rules.py` |
| `dashboard/server.py` | Web dashboard backend (FastAPI + WebSocket) |
| `dashboard/static/dashboard.html` | Dashboard single-page UI |
| `dashboard/config_sample.py` | Dashboard config template — copy to `dashboard/config.py` |
| `systemd/dmrlink3-bridge.service` | systemd unit for `bridge.py` |
| `systemd/dmrlink3-dash.service` | systemd unit for the dashboard |

Files whose names contain `SAMPLE` are templates. Remove `_SAMPLE` (or `_sample`)
from the name and customize. `dmrlink.cfg`, `bridge_rules.py`, and
`dashboard/config.py` are git-ignored so that `git pull` never overwrites your
live configuration.

---

### bridge.py — Conference Bridge

`bridge.py` connects multiple IPSC networks together using a rules file
(`bridge_rules.py`). A "bridge" is a named group: any system that is `ACTIVE`
on a bridge will receive all voice traffic arriving on that bridge from any other
active system on the same bridge.

**Key features:**
- Per-bridge per-system timeslot and talkgroup matching
- In-band activation and deactivation via TGID triggers — both fire on
  **key-down (VOICE_HEAD)**, so the bridge activates for the entire call
  including its first packet, and deactivates the moment an OFF tgid is keyed
  without waiting for unkey
- Each system manages its own bridge entry independently; a trigger received
  on one system activates only that system's entry
- Optional timeouts: `TO_TYPE: ON` deactivates an entry after `TIMEOUT` minutes
  of inactivity (timer resets on each ON-trigger key-down); `TO_TYPE: OFF`
  reactivates an entry after `TIMEOUT` minutes
- Entries that start `ACTIVE: True` with `TO_TYPE: ON` begin their timeout
  countdown on load; use `TO_TYPE: NONE` for entries that should stay active
  indefinitely without a timer
- Contention handling: group-hangtime and timeslot-clear-time rules prevent
  simultaneous overlapping calls from being bridged destructively
- Trunk bypass: systems listed in `TRUNKS` skip contention handling entirely —
  useful when two DMRlink3 instances are the only devices on an IPSC and you
  want full-duplex trunking
- Subscriber ACL: optional `sub_acl.py` can PERMIT or DENY specific radio IDs

---

### Reporting and Dashboard

DMRlink3 uses **NDJSON over TCP** for reporting (one JSON object per line). The
pickle/binary-opcode protocol from the original dmrlink is not implemented.

Enable reporting in `dmrlink.cfg`:
```ini
[REPORTS]
REPORT_NETWORKS: NETWORK
REPORT_PORT: 4321
REPORT_CLIENTS: 127.0.0.1
```

The dashboard connects to this feed and serves a browser UI at the configured
`WEB_PORT`. See `dashboard/config_sample.py` and [INSTALL.md](INSTALL.md) for
setup instructions.

**Demo mode** — append `?demo` to the dashboard URL (e.g. `http://localhost:8080/?demo`)
to load a static pre-populated scenario showing all visual elements: MASTER and PEER
systems with connected repeaters, a TRUNK endpoint with active streams, active calls
bridged coherently across systems, a slot in group-hangtime, and a populated call log.
No dmrlink3 connection is required. This is useful for understanding what the dashboard
looks like under normal operating conditions before any traffic has been seen.

**Dashboard features:**
- Live IPSC system status: peer table with radio IDs, addresses, and connection state
- Per-system **TS1/TS2 activity pills** showing real-time slot state:
  - **RX** (green) — receiving a call from a peer, with source radio, peer ID, TGID, and elapsed time
  - **TX** (orange) — forwarding a bridged call, with originating radio, source peer, TGID, and elapsed time
  - **Hang** (blue) — slot recently freed but still in group-hangtime; shows TGID and countdown to when the slot is available
  - **Idle** (gray) — slot is free
- Conference bridge table with per-entry active/inactive state, configured timeout duration, live timer countdown, and trigger TGID lists; bridge state changes push to the dashboard immediately on activation or deactivation rather than waiting for the periodic refresh
- Call log with timestamps, event type, system, timeslot, source radio, and TGID

---

### Configuration Notes

The configuration file is in `.ini` format and is self-documented in
`dmrlink-SAMPLE.cfg`. A few important points:

- **Do not enable features you do not understand.** IPSC exposes options that
  DMRlink3 does not implement (XNL/XCMP, for example). Enabling them may confuse
  other devices on your IPSC or produce unpredictable results.
- DMRlink3 **cannot brick a repeater or subscriber** because it does not
  implement XNL/XCMP, through which those dangerous operations flow.
- `PORT` must be unique per IPSC system stanza.
- Leave `IP:` blank to bind all interfaces; specify it only when DMRlink3 bridges
  between private/VPN and public networks.
- `RCM: True` and `CON_APP: True` are both required if you want Repeater Call
  Monitor packets forwarded to reporting clients.

---

### dmr_utils3

DMRlink3 depends on [dmr_utils3](https://github.com/n0mjs710/dmr_utils3), the
Python 3 port of dmr_utils. Install it from source before installing DMRlink3.
See [INSTALL.md](INSTALL.md).

---

### This Software Is Not a Commercial Product

DMRlink3 is not an out-of-the-box replacement for c-Bridge, SmartPTT, GenWatch,
RDAC, or similar commercial products. It does not do everything those products
do, and will likely never be something you can "just run" without understanding
what is happening under the hood. If you want a commercial-grade IPSC bridge,
buy one — they work great. If you want to understand IPSC, write your own
application on top of it, or need a capable open-source bridge without
commercial overhead, then DMRlink3 might be right for you.

Using DMRlink3 requires a basic understanding of Python 3.

---

*0x49 DE N0MJS*

Copyright (C) 2013-2026 Cortney T. Buffington, N0MJS <n0mjs@me.com>

This program is free software; you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation; either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program; if not, write to the Free Software Foundation, Inc.,
51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
