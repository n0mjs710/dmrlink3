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
import json
import logging
import os
import sys
import time
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('dmrdash')

# Drop stale "active" calls this many seconds after START if no END arrives.
CALL_STALE = 60


# ---- alias resolution --------------------------------------------------------

def _abs(p):
    return p if os.path.isabs(p) else os.path.join(HERE, p)

def load_aliases():
    base = _abs(PATH)
    peer_ids       = mk_id_dict(base, PEER_FILE)
    subscriber_ids = mk_id_dict(base, SUBSCRIBER_FILE)
    talkgroup_ids  = mk_id_dict(base, TGID_FILE)
    if LOCAL_PEER_FILE:
        peer_ids.update(mk_id_dict(base, LOCAL_PEER_FILE))
    if LOCAL_SUB_FILE:
        subscriber_ids.update(mk_id_dict(base, LOCAL_SUB_FILE))
    logger.info('aliases loaded: %d peers, %d subscribers, %d talkgroups',
                len(peer_ids), len(subscriber_ids), len(talkgroup_ids))
    return peer_ids, subscriber_ids, talkgroup_ids

PEER_IDS, SUBSCRIBER_IDS, TALKGROUP_IDS = load_aliases()

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

STATE = State()


def call_key(evt):
    return '{}|{}|{}'.format(evt.get('system', ''), evt.get('ts', 0), evt.get('call_id', 0))

def enrich_call(evt):
    evt['src_alias']  = alias(evt.get('src'),  SUBSCRIBER_IDS)
    evt['peer_alias'] = alias(evt.get('peer'), PEER_IDS)
    evt['tgid_alias'] = alias(evt.get('tgid'), TALKGROUP_IDS)
    return evt

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
    t = evt.get('type')
    if t == 'config':
        STATE.systems = evt.get('systems', {})
        await broadcast({'type': 'config', 'systems': STATE.systems})

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


# ---- dmrlink3 feed client (with reconnect) -----------------------------------

async def dmrlink_feed():
    while True:
        try:
            reader, writer = await asyncio.open_connection(DMRLINK_IP, DMRLINK_PORT)
            logger.info('connected to dmrlink3 feed at %s:%s', DMRLINK_IP, DMRLINK_PORT)
            STATE.dmrlink = True
            await broadcast({'type': 'dmrlink', 'connected': True})
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    await handle_event(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning('bad JSON line from dmrlink3: %r', line[:120])
        except (ConnectionRefusedError, OSError) as e:
            logger.warning('dmrlink3 feed unavailable (%s); retrying in 3s', e)
        finally:
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
    feed   = asyncio.create_task(dmrlink_feed())
    reaper = asyncio.create_task(reap_calls())
    yield
    feed.cancel()
    reaper.cancel()

app = FastAPI(lifespan=lifespan)


# ---- HTTP + WebSocket --------------------------------------------------------

@app.get('/', response_class=HTMLResponse)
async def index():
    with open(os.path.join(STATIC, 'dashboard.html'), encoding='utf-8') as f:
        html = f.read()
    return html.replace('{{REPORT_NAME}}', REPORT_NAME)

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
