#!/usr/bin/env python
#
###############################################################################
#   Copyright (C) 2016-2026  Cortney T. Buffington, N0MJS <n0mjs@me.com>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 3 of the License, or
#   (at your option) any later version.
###############################################################################

'''
DMRlink3 dashboard backend.

Connects to dmrlink3's NDJSON reporting feed (one JSON object per line), keeps
authoritative display state, and serves a single-page UI that receives live
JSON events over a WebSocket.

Run: python server.py  (from the dashboard/ directory, or set PYTHONPATH)
'''

import asyncio
import socket
import json
import logging
import os
import ssl
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from urllib.request import urlopen

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import ijson
import uvicorn

from dmr_utils3.utils import mk_id_dict, get_alias

HERE   = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, 'static')
sys.path.insert(0, HERE)

try:
    from config import (REPORT_NAME, DMRLINK_IP, DMRLINK_PORT,
                        WEB_HOST, WEB_PORT, LOG_LINES,
                        PATH, PEER_FILE, SUBSCRIBER_FILE, TGID_FILE,
                        LOCAL_SUB_FILE, LOCAL_PEER_FILE)
except ImportError:
    sys.exit('No config.py found — copy config_sample.py to config.py and edit it.')

# Feed transport: 'tcp' (default, connect to DMRLINK_IP:DMRLINK_PORT) or 'unix'
# (connect to the daemon's local Unix socket DMRLINK_SOCKET). Optional in config.
try:
    from config import DMRLINK_TRANSPORT
except ImportError:
    DMRLINK_TRANSPORT = 'tcp'
try:
    from config import DMRLINK_SOCKET
except ImportError:
    DMRLINK_SOCKET = ''

try:
    from config import TRY_DOWNLOAD, PEER_URL, SUBSCRIBER_URL, STALE_DAYS
except ImportError:
    TRY_DOWNLOAD = False
    PEER_URL = 'https://www.radioid.net/static/rptrs.json'
    SUBSCRIBER_URL = 'https://www.radioid.net/static/users.json'
    STALE_DAYS = 7

try:
    from config import FILTER_COUNTRIES
except ImportError:
    FILTER_COUNTRIES = None

try:
    from config import LAST_HEARD, LAST_HEARD_COUNT
except ImportError:
    LAST_HEARD = 'open'
    LAST_HEARD_COUNT = 10

try:
    from config import SYSTEM_PEERS
except ImportError:
    SYSTEM_PEERS = 'open'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('dmrdash')

# Drop stale "active" calls this many seconds after START if no END arrives.
# Backstop only: a dropped feed already clears active calls on disconnect, so
# this just guards against a lost/never-sent terminator on a still-connected
# feed. Keep it well above any real transmission (amateur TOT is ~3 min) so
# legitimate long calls are never clipped mid-stream.
CALL_STALE = 300


# ---- alias resolution --------------------------------------------------------

def _abs(p):
    return p if os.path.isabs(p) else os.path.join(HERE, p)

def _stream_id_file(url, path, json_key, countries, stale_secs):
    now = time.time()
    if os.path.isfile(path) and (os.path.getmtime(path) + stale_secs) >= now:
        logger.info('ID ALIAS MAPPER: %s is current, not downloaded', os.path.basename(path))
        return
    no_verify = ssl._create_unverified_context()
    tmp = path + '.tmp'
    try:
        with urlopen(url, context=no_verify) as response, \
             open(tmp, 'w', encoding='utf-8') as out:
            out.write('{"%s":[' % json_key)
            first = True
            for record in ijson.items(response, json_key + '.item'):
                if not countries or record.get('country') in countries:
                    if not first:
                        out.write(',')
                    json.dump({'id': record['id'], 'callsign': record['callsign']}, out)
                    first = False
            out.write(']}')
        os.replace(tmp, path)
        label = ', '.join(sorted(countries)) if countries else 'all countries'
        logger.info('ID ALIAS MAPPER: %s downloaded (%s)', os.path.basename(path), label)
    except IOError as e:
        logger.error('ID ALIAS MAPPER: download of %s failed: %s', os.path.basename(path), e)
        if os.path.exists(tmp):
            os.remove(tmp)

def _download_aliases():
    if not TRY_DOWNLOAD:
        return
    base = _abs(PATH)
    stale_secs = int(STALE_DAYS) * 86400
    countries = set(FILTER_COUNTRIES) if FILTER_COUNTRIES else None
    _stream_id_file(PEER_URL,       base + PEER_FILE,       'rptrs', countries, stale_secs)
    _stream_id_file(SUBSCRIBER_URL, base + SUBSCRIBER_FILE, 'users', countries, stale_secs)

def _reload_aliases():
    global PEER_IDS, SUBSCRIBER_IDS, TALKGROUP_IDS
    base = _abs(PATH)
    PEER_IDS       = mk_id_dict(base, PEER_FILE)
    SUBSCRIBER_IDS = mk_id_dict(base, SUBSCRIBER_FILE)
    TALKGROUP_IDS  = mk_id_dict(base, TGID_FILE)
    if LOCAL_PEER_FILE:
        PEER_IDS.update(mk_id_dict(base, LOCAL_PEER_FILE))
    if LOCAL_SUB_FILE:
        SUBSCRIBER_IDS.update(mk_id_dict(base, LOCAL_SUB_FILE))
    logger.info('aliases loaded: %d peers, %d subscribers, %d talkgroups',
                len(PEER_IDS), len(SUBSCRIBER_IDS), len(TALKGROUP_IDS))

PEER_IDS = {}
SUBSCRIBER_IDS = {}
TALKGROUP_IDS = {}

async def _alias_refresh_loop():
    while True:
        await asyncio.sleep(86400)
        logger.info('aliases: starting daily refresh')
        await asyncio.to_thread(_download_aliases)
        _reload_aliases()

# Logo
try:
    from config import LOGO_FILE
except ImportError:
    LOGO_FILE = ''
_logo_path = _abs(LOGO_FILE) if LOGO_FILE else ''
_logo_exists = bool(_logo_path and os.path.isfile(_logo_path))
_logo_media_type = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp',
}.get(os.path.splitext(_logo_path)[1].lower(), 'image/png') if _logo_exists else 'image/png'
LOGO_HTML = '<img src="/logo" alt="" class="logo">' if _logo_exists else ''

def alias(_id, _dict):
    a = get_alias(_id, _dict)
    return None if a == _id else a


# ---- shared state ------------------------------------------------------------

class State:
    def __init__(self):
        self.systems  = {}               # last 'config' payload
        self.bridges  = {}               # last 'bridge' payload (enriched)
        self.active   = {}               # call_key -> START bridge_event
        self.log      = deque(maxlen=LOG_LINES)
        self.dmrlink  = False
        self.clients  = set()            # connected WebSockets
        self.ping_loss_warn = 5          # PING_LOSS_WARN %: dashboard flags peer/master gold at/above this

STATE = State()


def call_key(evt):
    return '{}|{}|{}'.format(evt.get('system', ''), evt.get('ts', 0), evt.get('call_id', 0))

def enrich_call(evt):
    evt['src_alias']  = alias(evt.get('src'),  SUBSCRIBER_IDS)
    evt['peer_alias'] = alias(evt.get('peer'), PEER_IDS)
    evt['tgid_alias'] = alias(evt.get('tgid'), TALKGROUP_IDS)
    return evt

def enrich_systems(systems):
    now = time.time()
    for sys in systems.values():
        m = sys.get('MASTER', {})
        ct = m.get('CONNECT_TIME', 0)
        m['connected_secs'] = int(max(0, now - ct)) if ct else None
        for p in sys.get('PEERS', {}).values():
            ct = p.get('CONNECT_TIME', 0)
            p['connected_secs'] = int(max(0, now - ct)) if ct else None
    return systems

def enrich_bridges(bridges):
    now = time.time()
    for members in bridges.values():
        for m in members:
            m['TGID_NAME'] = alias(m.get('TGID'), TALKGROUP_IDS)
            if m.get('TO_TYPE') in ('ON', 'OFF'):
                m['remaining'] = int(m.get('TIMER', now) - now)
            else:
                m['remaining'] = None
    return bridges


async def broadcast(obj):
    dead = set()
    for ws in STATE.clients:
        try:
            await ws.send_json(obj)
        except Exception:
            dead.add(ws)
    STATE.clients -= dead


async def handle_event(evt):
    global FEED_READ_TIMEOUT
    t = evt.get('type')
    if t == 'config':
        interval = evt.get('report_interval')
        if interval:
            FEED_READ_TIMEOUT = max(float(interval) * 3, 30.0)
        STATE.ping_loss_warn = evt.get('ping_loss_warn', STATE.ping_loss_warn)
        STATE.systems = enrich_systems(evt.get('systems', {}))
        await broadcast({'type': 'config', 'systems': STATE.systems,
                         'ping_loss_warn': STATE.ping_loss_warn})

    elif t in ('peer_connected', 'peer_disconnected'):
        # Granular IPSC peer connect/disconnect delta. Apply it to the in-memory
        # systems view and re-broadcast the (enriched) config so the browser
        # renders it without waiting for the next slow resync. Ignored if the
        # system isn't known yet -- the on-connect snapshot will carry it.
        sysview = STATE.systems.get(evt.get('system'))
        if sysview is not None:
            peers = sysview.setdefault('PEERS', {})
            rid = str(evt.get('radio_id'))
            if t == 'peer_connected' and evt.get('info') is not None:
                peers[rid] = evt['info']
            elif t == 'peer_disconnected':
                peers.pop(rid, None)
            STATE.systems = enrich_systems(STATE.systems)
            await broadcast({'type': 'config', 'systems': STATE.systems,
                             'ping_loss_warn': STATE.ping_loss_warn})

    elif t == 'ping':
        pass   # liveness heartbeat; receiving it already reset the feed read timeout

    elif t == 'bridge':
        STATE.bridges = enrich_bridges(evt.get('bridges', {}))
        await broadcast({'type': 'bridge', 'bridges': STATE.bridges})

    elif t == 'bridge_event':
        data = evt.get('data', {})
        enrich_call(data)
        data['_arrived'] = time.time()
        key = call_key(data)
        event_name = data.get('event', '')
        if 'START' in event_name:
            STATE.active[key] = data
        else:
            STATE.active.pop(key, None)
        STATE.log.appendleft(data)
        await broadcast({'type': 'bridge_event', 'data': data})

    else:
        logger.debug('ignoring unknown event type: %s', t)


# dmrlink3 pushes a config snapshot at least every REPORT_INTERVAL, so if we go
# dmrlink3 pushes a config snapshot every REPORT_INTERVAL seconds -- that push is
# the de-facto heartbeat. If we go long enough with no line at all, the link is
# dead (whether or not a clean FIN arrived); without this, a silently-severed
# connection leaves readline() blocked forever and the reconnect loop never runs.
# The timeout must be safely LARGER than the push interval or it false-trips on a
# healthy idle link -- so we size it to 3x the interval the daemon advertises (see
# handle_event), not a fixed value. TCP keepalive (above) stays the primary
# detector of a truly dead socket. The default applies only until the first
# config arrives (which the daemon sends immediately on connect).
FEED_READ_TIMEOUT = 180.0


def _enable_tcp_keepalive(writer):
    sock = writer.get_extra_info('socket')
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, 'TCP_KEEPIDLE'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
    except OSError as e:
        logger.warning('could not set TCP keepalive on dmrlink3 feed: %s', e)


# ---- dmrlink3 feed client (with reconnect) -----------------------------------

async def dmrlink_feed():
    while True:
        writer = None
        try:
            if DMRLINK_TRANSPORT == 'unix':
                reader, writer = await asyncio.open_unix_connection(DMRLINK_SOCKET)
                logger.info('connected to dmrlink3 feed at unix socket %s', DMRLINK_SOCKET)
            else:
                reader, writer = await asyncio.open_connection(DMRLINK_IP, DMRLINK_PORT)
                _enable_tcp_keepalive(writer)
                logger.info('connected to dmrlink3 feed at %s:%s', DMRLINK_IP, DMRLINK_PORT)
            STATE.dmrlink = True
            await broadcast({'type': 'dmrlink', 'connected': True})
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), FEED_READ_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning('no data from dmrlink3 for %ss; assuming link dead, reconnecting',
                                   FEED_READ_TIMEOUT)
                    break
                if not line:
                    break
                try:
                    await handle_event(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning('bad JSON line from dmrlink3: %r', line[:120])
        except (ConnectionRefusedError, OSError) as e:
            logger.warning('dmrlink3 feed unavailable (%s); retrying in 3s', e)
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            if STATE.dmrlink:
                STATE.dmrlink = False
                STATE.systems = {}
                STATE.bridges = {}
                STATE.active  = {}
                await broadcast({'type': 'dmrlink', 'connected': False})
        await asyncio.sleep(3)


async def reap_calls():
    while True:
        await asyncio.sleep(10)
        now   = time.time()
        stale = [k for k, e in STATE.active.items()
                 if now - e.get('_arrived', now) > CALL_STALE]
        for k in stale:
            STATE.active.pop(k, None)
        if stale:
            logger.debug('reaped %d stale active call(s)', len(stale))


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(_download_aliases)
    _reload_aliases()
    refresher = asyncio.create_task(_alias_refresh_loop())
    feed      = asyncio.create_task(dmrlink_feed())
    reaper    = asyncio.create_task(reap_calls())
    yield
    feed.cancel()
    reaper.cancel()
    refresher.cancel()

app = FastAPI(lifespan=lifespan)


# ---- HTTP + WebSocket --------------------------------------------------------

@app.get('/', response_class=HTMLResponse)
async def index():
    with open(os.path.join(STATIC, 'dashboard.html'), encoding='utf-8') as f:
        html = f.read()
    return (html.replace('{{REPORT_NAME}}', REPORT_NAME)
                .replace('{{LOGO_HTML}}', LOGO_HTML)
                .replace('{{LAST_HEARD}}', str(LAST_HEARD))
                .replace('{{LAST_HEARD_COUNT}}', str(LAST_HEARD_COUNT))
                .replace('{{SYSTEM_PEERS}}', str(SYSTEM_PEERS)))

@app.get('/logo')
async def serve_logo():
    if not _logo_exists:
        raise HTTPException(status_code=404)
    return FileResponse(_logo_path, media_type=_logo_media_type)

@app.get('/api/state')
async def api_state():
    return JSONResponse(current_state())

def current_state():
    return {
        'report_name': REPORT_NAME,
        'dmrlink':     STATE.dmrlink,
        'systems':     STATE.systems,
        'bridges':     STATE.bridges,
        'active':      list(STATE.active.values()),
        'log':         list(STATE.log),
        'ping_loss_warn': STATE.ping_loss_warn,
    }

@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    STATE.clients.add(ws)
    try:
        await ws.send_json({'type': 'initial', **current_state()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        STATE.clients.discard(ws)


def main():
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level='warning')

if __name__ == '__main__':
    main()
