# Installing DMRlink3

## Prerequisites

- Python **3.10** or newer
- Linux (developed and run on Linux; other platforms are untested)
- Install and run as the **same user account** throughout
- [dmr_utils3](https://github.com/n0mjs710/dmr_utils3) — install this first (see step 1)

---

## 1. Install dmr_utils3

DMRlink3 depends on dmr_utils3, which is not yet on PyPI and must be installed
from source:

```bash
git clone https://github.com/n0mjs710/dmr_utils3.git
cd dmr_utils3
pip install -e .
cd ..
```

---

## 2. Clone DMRlink3 and create a virtual environment

```bash
git clone https://github.com/n0mjs710/dmrlink3.git
cd dmrlink3
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The dashboard shares this same virtualenv — no extra install step needed.

---

## 3. Configure DMRlink3

Copy the sample config (the live filename is git-ignored so `git pull` won't
overwrite your edits):

```bash
cp dmrlink-SAMPLE.cfg dmrlink.cfg
```

Edit `dmrlink.cfg` and fill in at minimum:

- **[REPORTS]** — set `REPORT_NETWORKS: NETWORK`, `REPORT_PORT`, and
  `REPORT_CLIENTS` if you will run the dashboard or any external monitoring. For a
  same-host dashboard, use `REPORT_TRANSPORT: unix` with a `REPORT_SOCKET` path
  instead of a TCP port (immune to NIC flaps / conntrack eviction).
- **[LOGGER]** — choose log handlers and level. Use `console-timed` for
  foreground testing; `file-timed` or `syslog` for production.
- **System stanzas** — one `[SECTION]` block per IPSC network.
  - Set `RADIO_ID` to the DMR ID DMRlink3 should present to that network.
  - Set `MASTER_PEER: True` if DMRlink3 is the IPSC master for that network;
    `False` to join as a peer (and fill in `MASTER_IP`/`MASTER_PORT`).
  - `PORT` must be unique per stanza.

Every system name used in `bridge_rules.py` must match a stanza name in
`dmrlink.cfg` exactly (case-sensitive).

---

## 4. Configure the bridge (if using bridge.py)

```bash
cp bridge_rules_SAMPLE.py bridge_rules.py
```

Edit `bridge_rules.py` to define your `BRIDGES`, `TRUNKS`, and `BRIDGE_CONF`.
See the comments in the file and [README.md](README.md) for a full explanation
of bridge rules.

Optionally, create a subscriber ACL:

```bash
cp sub_acl_SAMPLE.py sub_acl.py   # edit to PERMIT or DENY specific radio IDs
```

---

## 5. Configure the dashboard (if using the dashboard)

```bash
cp dashboard/config_sample.py dashboard/config.py
```

Edit `dashboard/config.py`:

- `DMRLINK_IP` / `DMRLINK_PORT` — point at dmrlink3's `REPORT_PORT` (TCP transport).
- `DMRLINK_TRANSPORT` / `DMRLINK_SOCKET` — set to `'unix'` and the daemon's
  `REPORT_SOCKET` path for a same-host feed over a Unix socket (must match the
  daemon's `REPORT_TRANSPORT`); leave as `'tcp'` for a remote dashboard.
- `WEB_PORT` — port the dashboard web server listens on (default `8080`).
- `PATH` — directory containing alias files (`peer_ids.json`, etc.).
- `LAST_HEARD` / `LAST_HEARD_COUNT` — default state (`'open'`/`'closed'`/`'off'`)
  and length of the Last Heard table.
- `SYSTEM_PEERS` — default state (`'open'`/`'closed'`) of each system's
  collapsible peer list; `'closed'` gives a compact view for many-system installs.

---

## 6. Run

### Verify the core first

Always confirm the protocol engine works before running bridge.py:

```bash
source venv/bin/activate
python dmrlink.py -c dmrlink.cfg -ll DEBUG
```

Watch the log output. You should see registration, keep-alives, and peer-list
exchange. Once that is clean, stop it and move to the bridge.

### Run the bridge

```bash
source venv/bin/activate
python bridge.py -c dmrlink.cfg -b bridge_rules -ll INFO
```

### Run the dashboard (separate terminal)

```bash
source venv/bin/activate
python dashboard/server.py
```

Open `http://localhost:8080` (or the host and port from `dashboard/config.py`)
in a browser.

---

## 7. Run as services (systemd)

Two unit files are provided in the `systemd/` directory.

### bridge.py service

```bash
sudo cp systemd/dmrlink3-bridge.service /etc/systemd/system/
sudoedit /etc/systemd/system/dmrlink3-bridge.service   # set User/Group and paths
sudo systemctl daemon-reload
sudo systemctl enable --now dmrlink3-bridge
journalctl -u dmrlink3-bridge -f
```

### Dashboard service

```bash
sudo cp systemd/dmrlink3-dash.service /etc/systemd/system/
sudoedit /etc/systemd/system/dmrlink3-dash.service     # set User/Group and paths
sudo systemctl daemon-reload
sudo systemctl enable --now dmrlink3-dash
journalctl -u dmrlink3-dash -f
```

The dashboard can be started and stopped independently of the bridge. It
reconnects automatically if the bridge restarts.

---

## Updating

```bash
git pull
source venv/bin/activate
pip install -r requirements.txt          # in case dependencies changed
sudo systemctl restart dmrlink3-bridge
sudo systemctl restart dmrlink3-dash
```

`dmrlink.cfg`, `bridge_rules.py`, and `dashboard/config.py` are git-ignored and
will not be touched by `git pull`.

---

## Logging

DMRlink3 uses Python's standard `logging` module. The `[LOGGER]` stanza in
`dmrlink.cfg` controls where output goes:

| Handler | Output |
|---------|--------|
| `console` | stdout, no timestamp |
| `console-timed` | stdout, with timestamp |
| `file` | log file, no timestamp |
| `file-timed` | log file, with timestamp |
| `syslog` | system syslog |
| `null` | discard |

Specify multiple handlers as a comma-separated list (no spaces), e.g.:
`LOG_HANDLERS: file-timed,syslog`

When running under systemd with `StandardOutput=journal`, use `console-timed`
or `console` — journal adds its own timestamp.

---

## Firewall

DMRlink3 opens one UDP port per configured IPSC system (the `PORT` value in each
stanza). If you are behind a firewall or NAT, these ports must be reachable from
the IPSC master and all other peers.

The reporting TCP port (`REPORT_PORT`, default `4321`) only needs to be reachable
from the dashboard host. If both run on the same machine, no firewall change is
needed.

The dashboard web port (`WEB_PORT`, default `8080`) needs to be reachable from
your browser.
