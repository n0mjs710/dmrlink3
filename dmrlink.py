#!/usr/bin/env python
#
###############################################################################
#   Copyright (C) 2013-2026  Cortney T. Buffington, N0MJS <n0mjs@me.com>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software Foundation,
#   Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA
###############################################################################

import asyncio
import socket
import json
import logging
import signal

from binascii import b2a_hex as ahex
from binascii import a2b_hex as bhex
from hashlib import sha1
from hmac import new as hmac_new, compare_digest
from socket import inet_ntoa as IPAddr
from socket import inet_aton as IPHexStr
from time import time

from const import (
    ANY_PEER_REQUIRED, PEER_REQUIRED, MASTER_REQUIRED, USER_PACKETS,
    GROUP_VOICE, PVT_VOICE, GROUP_DATA, PVT_DATA,
    CALL_MON_STATUS, CALL_MON_RPT, REPEATER_BLOCKED, CALL_INTERRUPT_REQ,
    XCMP_XNL, RPT_WAKE_UP,
    DE_REG_REQ, DE_REG_REPLY,
    MASTER_REG_REQ, MASTER_REG_REPLY,
    PEER_LIST_REQ, PEER_LIST_REPLY,
    PEER_REG_REQ, PEER_REG_REPLY,
    MASTER_ALIVE_REQ, MASTER_ALIVE_REPLY,
    PEER_ALIVE_REQ, PEER_ALIVE_REPLY,
    SYSTEM_MAP_REQ, SYSTEM_MAP_REPLY, UNKNOWN_9E, WIRELINE,
    REMOTE_PROG_REQ, REMOTE_PROG_REPLY, OPCODE_0xF0,
    VOICE_HEAD, VOICE_TERM,
    GV_BURST_TYPE_OFF, GV_CALL_INFO_OFF,
    IPSC_VER,
    PEER_OP_MSK, PEER_MODE_MSK, PEER_MODE_ANALOG, PEER_MODE_DIGITAL,
    IPSC_TS1_MSK, IPSC_TS2_MSK,
    CSBK_MSK, RPT_MON_MSK, CON_APP_MSK,
    XNL_STAT_MSK, XNL_MSTR_MSK, XNL_SLAVE_MSK, PKT_AUTH_MSK,
    DATA_CALL_MSK, VOICE_CALL_MSK, MSTR_PEER_MSK,
    END_MSK, TS_CALL_MSK,
)
from dmr_utils3.utils import bytes_2, bytes_3, bytes_4, int_id, try_download, mk_id_dict

from config import acl_check

__author__      = 'Cortney T. Buffington, N0MJS'
__copyright__   = 'Copyright (c) 2013-2026 Cortney T. Buffington, N0MJS and the K0USY Group'
__credits__     = 'Adam Fast, KC0YLK; Dave Kierzkowski, KD8EYF; Steve Zingman, N4IRS; Mike Zingman, N4IRR'
__license__     = 'GNU GPLv3'
__maintainer__  = 'Cort Buffington, N0MJS'
__email__       = 'n0mjs@me.com'

logger = logging.getLogger(__name__)

# Minimum gap between calls on the same TS — handles Talker Alias VOICE_HEAD churn
TS_CLEAR_TIME = 0.2

# Global systems dict (populated by mk_ipsc_systems)
systems = {}


# ---------------------------------------------------------------------------
# Async utilities
# ---------------------------------------------------------------------------

async def run_periodic(_interval, _func, _name, *_args):
    """Replace twisted's task.LoopingCall. Logs exceptions and keeps running."""
    try:
        while True:
            await asyncio.sleep(_interval)
            try:
                _func(*_args)
            except Exception:
                logger.error('(GLOBAL) Error in periodic task %s', _name, exc_info=True)
    except asyncio.CancelledError:
        raise


# ---------------------------------------------------------------------------
# Reporting server (NDJSON over TCP, replaces pickle/Netstring)
# ---------------------------------------------------------------------------

def _systems_snapshot(_systems):
    """Produce a JSON-serializable view of the live SYSTEMS dict."""
    result = {}
    for name, sys in _systems.items():
        # TRUNK systems have a different config structure; report them separately.
        if sys.get('SYSTEM_TYPE') == 'TRUNK':
            local = sys['LOCAL']
            trunk = sys['TRUNK']
            result[name] = {
                'MODE':           'TRUNK',
                'RADIO_ID':       int_id(local['RADIO_ID']),
                'IP':             local['IP'] or '0.0.0.0',
                'PORT':           local['PORT'],
                'GROUP_HANGTIME': local['GROUP_HANGTIME'],
                'PEER_IP':        trunk['PEER_IP'],
                'PEER_PORT':      trunk['PEER_PORT'],
                'MASTER': {},
                'PEERS':  {},
            }
            continue

        local  = sys['LOCAL']
        master = sys['MASTER']
        peers  = sys['PEERS']
        result[name] = {
            'MODE':           'MASTER' if local['MASTER_PEER'] else 'PEER',
            'RADIO_ID':       int_id(local['RADIO_ID']),
            'IP':             local['IP'] or '0.0.0.0',
            'PORT':           local['PORT'],
            'ALIVE_TIMER':    local['ALIVE_TIMER'],
            'GROUP_HANGTIME': local['GROUP_HANGTIME'],
            'MASTER': {
                'RADIO_ID':               int_id(master['RADIO_ID']) if master['RADIO_ID'] != b'\x00\x00\x00\x00' else None,
                'IP':                     master.get('IP', ''),
                'PORT':                   master.get('PORT', ''),
                'CONNECTED':              master['STATUS']['CONNECTED'],
                'CONNECT_TIME':           master['STATUS']['CONNECT_TIME'],
                'PEER_LIST':              master['STATUS']['PEER_LIST'],
                'KEEP_ALIVES_SENT':       master['STATUS']['KEEP_ALIVES_SENT'],
                'KEEP_ALIVES_RECEIVED':   master['STATUS']['KEEP_ALIVES_RECEIVED'],
                'KEEP_ALIVES_OUTSTANDING':master['STATUS']['KEEP_ALIVES_OUTSTANDING'],
                'KEEP_ALIVES_MISSED':     master['STATUS']['KEEP_ALIVES_MISSED'],
            },
            'PEERS': {
                str(int_id(pid)): {
                    'RADIO_ID':                int_id(pid),
                    'IP':                      p['IP'],
                    'PORT':                    p['PORT'],
                    'CONNECTED':               p['STATUS']['CONNECTED'],
                    'CONNECT_TIME':            p['STATUS']['CONNECT_TIME'],
                    'KEEP_ALIVES_SENT':        p['STATUS']['KEEP_ALIVES_SENT'],
                    'KEEP_ALIVES_RECEIVED':    p['STATUS']['KEEP_ALIVES_RECEIVED'],
                    'KEEP_ALIVES_OUTSTANDING': p['STATUS']['KEEP_ALIVES_OUTSTANDING'],
                    'KEEP_ALIVES_MISSED':      p['STATUS']['KEEP_ALIVES_MISSED'],
                    'KEEP_ALIVE_RX_TIME':      p['STATUS']['KEEP_ALIVE_RX_TIME'],
                }
                for pid, p in peers.items()
            },
        }
    return result


# Turn on TCP keepalive with aggressive timers so a silently-severed connection
# (NIC/interface flap, DHCP renew, firewall/conntrack eviction, router reboot --
# anything that drops the flow without a clean FIN/RST) is detected by the OS
# within ~2 minutes instead of the default ~2 hours. The transport then errors,
# which unblocks the read loop and sheds the client. Only meaningful for
# non-loopback connections; harmless on loopback.
def _enable_tcp_keepalive(_writer, _idle=60, _intvl=15, _cnt=4):
    sock = _writer.get_extra_info('socket')
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, 'TCP_KEEPIDLE'):     # Linux
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, _idle)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _intvl)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _cnt)
    except OSError as e:
        logger.warning('(GLOBAL) Could not set TCP keepalive: %s', e)


# Drop a client whose outbound buffer has grown past this, i.e. it is not
# reading -- a stuck/dead peer we haven't detected yet. Bounds memory so one
# wedged dashboard can't grow dmrlink3 without limit.
_REPORT_WRITE_BUFFER_LIMIT = 1 << 20   # 1 MiB


class ReportServer:
    """Async TCP server that pushes NDJSON events to connected dashboard clients."""

    def __init__(self, _config):
        self._config = _config
        self.clients = []   # list of asyncio.StreamWriter

    async def start(self, port):
        allowed = self._config['REPORTS']['REPORT_CLIENTS']
        try:
            server = await asyncio.start_server(
                lambda r, w: self._client_connected(r, w, allowed),
                host='0.0.0.0',
                port=port,
            )
        except OSError as e:
            logger.error('(GLOBAL) Reporting server could not bind port %s: %s', port, e)
            return
        logger.info('(GLOBAL) DMRlink3 reporting server listening on port %s', port)
        async with server:
            await server.serve_forever()

    async def _client_connected(self, reader, writer, allowed):
        addr = writer.get_extra_info('peername')
        if '*' not in allowed and addr[0] not in allowed:
            logger.warning('(GLOBAL) Reporting connection rejected from %s', addr[0])
            writer.close()
            return
        logger.info('(GLOBAL) Reporting client connected: %s:%s', addr[0], addr[1])
        _enable_tcp_keepalive(writer)
        self.clients.append(writer)
        self.send_config()   # push current state immediately; don't wait for the next periodic tick
        self.send_bridge()   # no-op in base class; BridgeReportServer overrides this
        try:
            while True:
                data = await reader.read(256)
                if not data:
                    break
        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            logger.warning('(GLOBAL) Reporting client error: %s', e)
        finally:
            if writer in self.clients:
                self.clients.remove(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info('(GLOBAL) Reporting client disconnected: %s:%s', addr[0], addr[1])

    def _send_json(self, obj):
        data = (json.dumps(obj) + '\n').encode()
        dead = []
        for writer in self.clients:
            # StreamWriter.write() buffers and almost never raises for a broken
            # peer, so a dead connection is caught two other ways: is_closing()
            # (set once the OS/keepalive has errored the transport) and an
            # over-limit write buffer (the client has stopped reading).
            if writer.is_closing():
                dead.append(writer)
                continue
            try:
                transport = writer.transport
                if transport is not None and \
                        transport.get_write_buffer_size() > _REPORT_WRITE_BUFFER_LIMIT:
                    logger.warning('(GLOBAL) Dropping unresponsive client %s (write buffer over limit)',
                                   writer.get_extra_info('peername'))
                    dead.append(writer)
                    continue
                writer.write(data)
            except Exception as e:
                logger.warning('(GLOBAL) Reporting write error: %s', e)
                dead.append(writer)
        for w in dead:
            if w in self.clients:
                self.clients.remove(w)
            try:
                w.close()
            except Exception:
                pass

    def send_config(self):
        self._send_json({'type': 'config', 'systems': _systems_snapshot(self._config['SYSTEMS'])})

    def send_bridge(self):
        pass  # Overridden by BridgeReportServer in bridge.py

    def send_bridge_event(self, _event):
        pass  # Overridden by BridgeReportServer in bridge.py

    def send_rcm(self, _data):
        pass  # RCM monitoring not forwarded in dmrlink3


# ---------------------------------------------------------------------------
# Reporting setup (called from async context)
# ---------------------------------------------------------------------------

def config_reports(_config, _factory):
    loop = asyncio.get_running_loop()

    if _config['REPORTS']['REPORT_NETWORKS'] == 'PRINT':
        def reporting_loop():
            logger.debug('(GLOBAL) Periodic reporting loop (PRINT)')
            for system in _config['SYSTEMS']:
                print_master(_config, system)
                print_peer_list(_config, system)
        loop.create_task(run_periodic(_config['REPORTS']['REPORT_INTERVAL'], reporting_loop, 'reporting'))
        return None

    elif _config['REPORTS']['REPORT_NETWORKS'] == 'NETWORK':
        def reporting_loop(_server):
            logger.debug('(GLOBAL) Periodic reporting loop (NETWORK)')
            _server.send_config()
            _server.send_bridge()
        report_server = _factory(_config)
        loop.create_task(report_server.start(_config['REPORTS']['REPORT_PORT']))
        loop.create_task(run_periodic(_config['REPORTS']['REPORT_INTERVAL'],
                                      reporting_loop, 'reporting', report_server))
        logger.info('(GLOBAL) DMRlink3 TCP reporting server configured on port %s',
                    _config['REPORTS']['REPORT_PORT'])
        return report_server

    else:
        logger.info('(GLOBAL) TCP reporting disabled')
        return None


# ---------------------------------------------------------------------------
# ID alias helpers
# ---------------------------------------------------------------------------

def build_aliases(_config):
    if not _config['ALIASES'].get('USE_ALIASES', True):
        logger.info('ID ALIAS MAPPER: disabled in configuration')
        return {}, {}, {}, {}
    if _config['ALIASES']['TRY_DOWNLOAD']:
        result = try_download(_config['ALIASES']['PATH'], _config['ALIASES']['PEER_FILE'],
                              _config['ALIASES']['PEER_URL'], _config['ALIASES']['STALE_TIME'])
        logger.info(result)
        result = try_download(_config['ALIASES']['PATH'], _config['ALIASES']['SUBSCRIBER_FILE'],
                              _config['ALIASES']['SUBSCRIBER_URL'], _config['ALIASES']['STALE_TIME'])
        logger.info(result)

    peer_ids = mk_id_dict(_config['ALIASES']['PATH'], _config['ALIASES']['PEER_FILE'])
    if peer_ids:
        logger.info('ID ALIAS MAPPER: peer_ids dictionary is available')

    local_peer_path = _config['ALIASES']['PATH'] + 'local_peer_ids.json'
    try:
        with open(local_peer_path, 'r') as _f:
            local_peer_ids = json.load(_f)
        count = 0
        for _id, _call in local_peer_ids.items():
            peer_ids[int(_id)] = _call
            count += 1
        logger.info('ID ALIAS MAPPER: local_peer_ids.json merged %d entries into peer_ids', count)
    except FileNotFoundError:
        pass

    subscriber_ids = mk_id_dict(_config['ALIASES']['PATH'], _config['ALIASES']['SUBSCRIBER_FILE'])
    if subscriber_ids:
        logger.info('ID ALIAS MAPPER: subscriber_ids dictionary is available')

    talkgroup_ids = mk_id_dict(_config['ALIASES']['PATH'], _config['ALIASES']['TGID_FILE'])
    if talkgroup_ids:
        logger.info('ID ALIAS MAPPER: talkgroup_ids dictionary is available')

    local_ids = mk_id_dict(_config['ALIASES']['PATH'], _config['ALIASES']['LOCAL_FILE'])
    if local_ids:
        logger.info('ID ALIAS MAPPER: local_ids dictionary is available')

    return peer_ids, subscriber_ids, talkgroup_ids, local_ids


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def process_mode_byte(_hex_mode):
    _mode = _hex_mode[0]
    _peer_op   = bool(_mode & PEER_OP_MSK)
    _ts1       = bool(_mode & IPSC_TS1_MSK)
    _ts2       = bool(_mode & IPSC_TS2_MSK)

    if _mode & PEER_MODE_MSK == PEER_MODE_MSK:
        _peer_mode = 'UNKNOWN'
    elif not _mode & PEER_MODE_MSK:
        _peer_mode = 'NO_RADIO'
    elif _mode & PEER_MODE_ANALOG:
        _peer_mode = 'ANALOG'
    elif _mode & PEER_MODE_DIGITAL:
        _peer_mode = 'DIGITAL'
    else:
        _peer_mode = 'UNKNOWN'

    return {'PEER_OP': _peer_op, 'PEER_MODE': _peer_mode, 'TS_1': _ts1, 'TS_2': _ts2}


def process_flags_bytes(_hex_flags):
    _byte3 = _hex_flags[2]
    _byte4 = _hex_flags[3]

    return {
        'CSBK':       bool(_byte3 & CSBK_MSK),
        'RCM':        bool(_byte3 & RPT_MON_MSK),
        'CON_APP':    bool(_byte3 & CON_APP_MSK),
        'XNL_CON':    bool(_byte4 & XNL_STAT_MSK),
        'XNL_MASTER': bool(_byte4 & XNL_MSTR_MSK),
        'XNL_SLAVE':  bool(_byte4 & XNL_SLAVE_MSK),
        'AUTH':       bool(_byte4 & PKT_AUTH_MSK),
        'DATA':       bool(_byte4 & DATA_CALL_MSK),
        'VOICE':      bool(_byte4 & VOICE_CALL_MSK),
        'MASTER':     bool(_byte4 & MSTR_PEER_MSK),
    }


def build_peer_list(_peers):
    concatenated_peers = b''
    for peer in _peers:
        hex_ip   = IPHexStr(_peers[peer]['IP'])
        hex_port = bytes_2(_peers[peer]['PORT'])
        mode     = _peers[peer]['MODE']
        concatenated_peers += peer + hex_ip + hex_port + mode
    return bytes_2(len(concatenated_peers)) + concatenated_peers


def print_peer_list(_config, _network):
    _peers  = _config['SYSTEMS'][_network]['PEERS']
    _status = _config['SYSTEMS'][_network]['MASTER']['STATUS']['PEER_LIST']

    if _status and not _peers:
        print('We are the only peer for: %s' % _network)
        print('')
        return

    print('Peer List for: %s' % _network)
    for peer in _peers.keys():
        _this_peer      = _peers[peer]
        _this_peer_stat = _this_peer['STATUS']
        me = '(self)' if peer == _config['SYSTEMS'][_network]['LOCAL']['RADIO_ID'] else ''
        print('\tRADIO ID: {} {}'.format(int_id(peer), me))
        print('\t\tIP Address: {}:{}'.format(_this_peer['IP'], _this_peer['PORT']))
        if _this_peer['MODE_DECODE'] and _config['REPORTS']['PRINT_PEERS_INC_MODE']:
            print('\t\tMode Values:')
            for name, value in _this_peer['MODE_DECODE'].items():
                print('\t\t\t{}: {}'.format(name, value))
        if _this_peer['FLAGS_DECODE'] and _config['REPORTS']['PRINT_PEERS_INC_FLAGS']:
            print('\t\tService Flags:')
            for name, value in _this_peer['FLAGS_DECODE'].items():
                print('\t\t\t{}: {}'.format(name, value))
        print('\t\tStatus: {},  KA Sent: {},  KA Outstanding: {},  KA Missed: {}'.format(
            _this_peer_stat['CONNECTED'], _this_peer_stat['KEEP_ALIVES_SENT'],
            _this_peer_stat['KEEP_ALIVES_OUTSTANDING'], _this_peer_stat['KEEP_ALIVES_MISSED']))
        print('\t\t                KA Received: {},  Last KA at: {}'.format(
            _this_peer_stat['KEEP_ALIVES_RECEIVED'], _this_peer_stat['KEEP_ALIVE_RX_TIME']))
    print('')


def print_master(_config, _network):
    if _config['SYSTEMS'][_network]['LOCAL']['MASTER_PEER']:
        print('DMRlink3 is the Master for %s' % _network)
    else:
        _master = _config['SYSTEMS'][_network]['MASTER']
        print('Master for %s' % _network)
        print('\tRADIO ID: {}'.format(int_id(_master['RADIO_ID'])))
        if _master['MODE_DECODE'] and _config['REPORTS']['PRINT_PEERS_INC_MODE']:
            print('\t\tMode Values:')
            for name, value in _master['MODE_DECODE'].items():
                print('\t\t\t{}: {}'.format(name, value))
        if _master['FLAGS_DECODE'] and _config['REPORTS']['PRINT_PEERS_INC_FLAGS']:
            print('\t\tService Flags:')
            for name, value in _master['FLAGS_DECODE'].items():
                print('\t\t\t{}: {}'.format(name, value))
        print('\t\tStatus: {},  KA Sent: {},  KA Outstanding: {},  KA Missed: {}'.format(
            _master['STATUS']['CONNECTED'], _master['STATUS']['KEEP_ALIVES_SENT'],
            _master['STATUS']['KEEP_ALIVES_OUTSTANDING'], _master['STATUS']['KEEP_ALIVES_MISSED']))
        print('\t\t                KA Received: {},  Last KA at: {}'.format(
            _master['STATUS']['KEEP_ALIVES_RECEIVED'], _master['STATUS']['KEEP_ALIVE_RX_TIME']))


# ---------------------------------------------------------------------------
# System factory
# ---------------------------------------------------------------------------

async def mk_ipsc_systems(_config, _systems, _ipsc, _report_server):
    loop = asyncio.get_running_loop()
    for system in _config['SYSTEMS']:
        if _config['SYSTEMS'][system].get('SYSTEM_TYPE') == 'TRUNK':
            continue  # TRUNK systems are instantiated separately by bridge.py
        if _config['SYSTEMS'][system]['LOCAL']['ENABLED']:
            _systems[system] = _ipsc(system, _config, _report_server)
            proto = _systems[system]
            ip   = _config['SYSTEMS'][system]['LOCAL']['IP'] or '0.0.0.0'
            port = _config['SYSTEMS'][system]['LOCAL']['PORT']
            await loop.create_datagram_endpoint(
                lambda p=proto: p,
                local_addr=(ip, port),
            )
            logger.info('(%s) UDP endpoint bound on %s:%s', system, ip, port)
    return _systems


# ---------------------------------------------------------------------------
# IPSC Protocol class
# ---------------------------------------------------------------------------

class IPSC(asyncio.DatagramProtocol):
    def __init__(self, _name, _config, _report):
        self._system   = _name
        self._CONFIG   = _config
        self._report   = _report
        self._config   = self._CONFIG['SYSTEMS'][self._system]
        self._rcm      = self._CONFIG['REPORTS']['REPORT_RCM'] and self._report
        self._local    = self._config['LOCAL']
        self._local_id = self._local['RADIO_ID']
        self._master      = self._config['MASTER']
        self._master_stat = self._master['STATUS']
        self._master_sock = (self._master['IP'], self._master['PORT'])
        self._peers       = self._config['PEERS']

        self.TS_FLAGS = self._local['MODE'] + self._local['FLAGS']

        self.MASTER_REG_REQ_PKT     = MASTER_REG_REQ   + self._local_id + self.TS_FLAGS + IPSC_VER
        self.MASTER_ALIVE_PKT       = MASTER_ALIVE_REQ  + self._local_id + self.TS_FLAGS + IPSC_VER
        self.PEER_LIST_REQ_PKT      = PEER_LIST_REQ     + self._local_id
        self.PEER_REG_REQ_PKT       = PEER_REG_REQ      + self._local_id + IPSC_VER
        self.PEER_REG_REPLY_PKT     = PEER_REG_REPLY    + self._local_id + IPSC_VER
        self.PEER_ALIVE_REQ_PKT     = PEER_ALIVE_REQ    + self._local_id + self.TS_FLAGS
        self.PEER_ALIVE_REPLY_PKT   = PEER_ALIVE_REPLY  + self._local_id + self.TS_FLAGS
        self.MASTER_ALIVE_REPLY_PKT = MASTER_ALIVE_REPLY + self._local_id + self.TS_FLAGS + IPSC_VER
        self.PEER_LIST_REPLY_PKT    = PEER_LIST_REPLY   + self._local_id
        self.DE_REG_REQ_PKT         = DE_REG_REQ        + self._local_id
        self.DE_REG_REPLY_PKT       = DE_REG_REPLY      + self._local_id

        self.rx_start = {1: 0, 2: 0}   # per-TS RX call start timestamp (VOICE_HEAD → VOICE_TERM)
        self.tx_start = {1: 0, 2: 0}   # per-TS TX call start timestamp (forwarded HEAD → TERM)

        logger.info('(%s) IPSC Instance Created: %s, %s:%s',
                    self._system, int_id(self._local['RADIO_ID']),
                    self._local['IP'], self._local['PORT'])

    # -----------------------------------------------------------------------
    # asyncio.DatagramProtocol interface
    # -----------------------------------------------------------------------

    def connection_made(self, transport):
        self.transport = transport
        loop = asyncio.get_running_loop()
        if not self._local['MASTER_PEER']:
            loop.create_task(run_periodic(
                self._local['ALIVE_TIMER'], self.peer_maintenance_loop, self._system))
        else:
            loop.create_task(run_periodic(
                self._local['ALIVE_TIMER'], self.master_maintenance_loop, self._system))

    def datagram_received(self, data, addr):
        host, port = addr
        _packettype = data[0:1]
        _peerid     = data[1:5]

        if self._local['AUTH_ENABLED']:
            if not self.validate_auth(self._local['AUTH_KEY'], data):
                logger.warning('(%s) AuthError: packet failed authentication. Type %s: Peer: %s, %s:%s',
                               self._system, ahex(_packettype), int_id(_peerid), host, port)
                return
            data = self.strip_hash(data)

        # Master watchdog: any packet from a registered peer refreshes their keepalive timestamp,
        # preventing false timeouts when keepalive packets are lost but voice/data is flowing.
        if self._local['MASTER_PEER'] and len(data) >= 5 and _peerid in self._peers:
            self._peers[_peerid]['STATUS']['KEEP_ALIVE_RX_TIME'] = int(time())

        if _packettype in ANY_PEER_REQUIRED:
            if not (self.valid_master(_peerid) or self.valid_peer(_peerid)):
                logger.warning('(%s) PeerError: Peer not in peer-list: %s, %s:%s',
                               self._system, int_id(_peerid), host, port)
                return

            if _packettype in USER_PACKETS:
                _src_sub   = data[6:9]
                _dst_sub   = data[9:12]
                _call_info = data[17]
                _end       = bool(_call_info & END_MSK)

                if _packettype == GROUP_VOICE:
                    # Timeslot detection: VOICE_HEAD/VOICE_TERM encode TS in call_info byte;
                    # SLOT1/SLOT2_VOICE encode it in bit 7 of the burst_type byte instead.
                    _burst_type = data[GV_BURST_TYPE_OFF]
                    if _burst_type in (VOICE_HEAD, VOICE_TERM):
                        _ts = 2 if (_call_info & TS_CALL_MSK) else 1
                    else:
                        _ts = 2 if (_burst_type & 0x80) else 1
                    self.reset_keep_alive(_peerid)
                    self.group_voice(_src_sub, _dst_sub, _ts, _end, _peerid, data)
                else:
                    _ts = 2 if (_call_info & TS_CALL_MSK) else 1
                    if _packettype == PVT_VOICE:
                        self.reset_keep_alive(_peerid)
                        self.private_voice(_src_sub, _dst_sub, _ts, _end, _peerid, data)
                    elif _packettype == GROUP_DATA:
                        self.reset_keep_alive(_peerid)
                        self.group_data(_src_sub, _dst_sub, _ts, _end, _peerid, data)
                    elif _packettype == PVT_DATA:
                        self.reset_keep_alive(_peerid)
                        self.private_data(_src_sub, _dst_sub, _ts, _end, _peerid, data)
                return

            elif _packettype == XCMP_XNL:
                logger.debug('(%s) XCMP/XNL from %s:%s — ignored', self._system, host, port)
            elif _packettype == CALL_MON_STATUS:
                self.call_mon_status(data)
            elif _packettype == CALL_MON_RPT:
                self.call_mon_rpt(data)
            elif _packettype == REPEATER_BLOCKED:
                self.repeater_blocked(data)
            elif _packettype == CALL_INTERRUPT_REQ:
                logger.debug('(%s) CALL_INTERRUPT_REQ from %s:%s', self._system, host, port)
            elif _packettype == DE_REG_REQ:
                self.send_packet(self.DE_REG_REPLY_PKT, (host, port))
                self.de_register_peer(_peerid)
                if self._local['MASTER_PEER'] and self._peers:
                    self.send_to_ipsc(self.PEER_LIST_REPLY_PKT + build_peer_list(self._peers))
                logger.info('(%s) Peer De-Registration From: %s, %s:%s',
                            self._system, int_id(_peerid), host, port)
            elif _packettype == DE_REG_REPLY:
                logger.info('(%s) De-Registration Reply From: %s, %s:%s',
                            self._system, int_id(_peerid), host, port)
            elif _packettype == RPT_WAKE_UP:
                self.repeater_wake_up(data)
            return

        if _packettype in PEER_REQUIRED:
            if not self.valid_peer(_peerid):
                logger.warning('(%s) PeerError: Peer not in peer-list: %s, %s:%s',
                               self._system, int_id(_peerid), host, port)
                return
            if _packettype == PEER_ALIVE_REQ:
                self.peer_alive_req(data, _peerid, host, port)
            elif _packettype == PEER_REG_REQ:
                self.peer_reg_req(_peerid, host, port)
            elif _packettype == PEER_ALIVE_REPLY:
                self.peer_alive_reply(_peerid)
            elif _packettype == PEER_REG_REPLY:
                self.peer_reg_reply(_peerid)
            return

        if _packettype in MASTER_REQUIRED:
            if not self.valid_master(_peerid):
                logger.warning('(%s) MasterError: %s, %s:%s is not the master peer',
                               self._system, int_id(_peerid), host, port)
                return
            if _packettype == MASTER_ALIVE_REPLY:
                self.master_alive_reply(_peerid)
            elif _packettype == PEER_LIST_REPLY:
                self.peer_list_reply(data, _peerid)
            return

        if _packettype == MASTER_REG_REPLY:
            self.master_reg_reply(data, _peerid)
        elif _packettype == MASTER_REG_REQ:
            self.master_reg_req(data, _peerid, host, port)
        elif _packettype == MASTER_ALIVE_REQ:
            self.master_alive_req(_peerid, host, port)
        elif _packettype == PEER_LIST_REQ:
            self.peer_list_req(_peerid, host, port)
        elif _packettype == OPCODE_0xF0:
            logger.debug('(%s) 0xF0 from %s:%s — benign, no response', self._system, host, port)
        elif _packettype in (SYSTEM_MAP_REQ, SYSTEM_MAP_REPLY, UNKNOWN_9E,
                             WIRELINE, REMOTE_PROG_REQ, REMOTE_PROG_REPLY):
            logger.debug('(%s) Opcode %s from %s:%s — no handler',
                         self._system, ahex(_packettype), host, port)
        else:
            self.unknown_message(_packettype, _peerid, data)

    def error_received(self, exc):
        logger.error('(%s) UDP error received: %s', self._system, exc)

    def connection_lost(self, exc):
        logger.warning('(%s) UDP connection lost: %s', self._system, exc)

    # -----------------------------------------------------------------------
    # Callbacks (overridden by subclasses)
    # -----------------------------------------------------------------------

    def call_mon_status(self, _data):
        logger.debug('(%s) Repeater Call Monitor Origin Packet Received', self._system)
        if self._rcm:
            self._report.send_rcm(self._system + ',' + _data.decode('latin-1'))

    def call_mon_rpt(self, _data):
        logger.debug('(%s) Repeater Call Monitor Repeating Packet Received', self._system)
        if self._rcm:
            self._report.send_rcm(self._system + ',' + _data.decode('latin-1'))

    def repeater_blocked(self, _data):
        logger.debug('(%s) Repeater Blocked Packet Received', self._system)

    def repeater_wake_up(self, _data):
        logger.debug('(%s) Repeater Wake-Up Packet Received', self._system)

    def group_voice(self, _src_sub, _dst_sub, _ts, _end, _peerid, _data):
        logger.debug('(%s) Group Voice Packet: From %s, Peer %s, Dst %s',
                     self._system, int_id(_src_sub), int_id(_peerid), int_id(_dst_sub))
        _burst_type = _data[GV_BURST_TYPE_OFF]
        _now = time()
        if _burst_type == VOICE_HEAD:
            if self.rx_start[_ts] == 0 or (_now - self.rx_start[_ts]) > TS_CLEAR_TIME:
                self.rx_start[_ts] = _now
                logger.info('(%s) GROUP VOICE START: Peer: %s, Src: %s, TS: %s, TGID: %s',
                            self._system, int_id(_peerid), int_id(_src_sub), _ts, int_id(_dst_sub))
                if self._report:
                    self._report.send_bridge_event({
                        'event':  'GROUP VOICE START',
                        'system': self._system,
                        'peer':   int_id(_peerid),
                        'src':    int_id(_src_sub),
                        'ts':     _ts,
                        'tgid':   int_id(_dst_sub),
                    })
        elif _burst_type == VOICE_TERM:
            if self.rx_start[_ts] > 0:
                _duration = _now - self.rx_start[_ts]
                self.rx_start[_ts] = 0
                logger.info('(%s) GROUP VOICE END: Peer: %s, Src: %s, TS: %s, TGID: %s Duration: %.2fs',
                            self._system, int_id(_peerid), int_id(_src_sub), _ts, int_id(_dst_sub), _duration)
                if self._report:
                    self._report.send_bridge_event({
                        'event':    'GROUP VOICE END',
                        'system':   self._system,
                        'peer':     int_id(_peerid),
                        'src':      int_id(_src_sub),
                        'ts':       _ts,
                        'tgid':     int_id(_dst_sub),
                        'duration': round(_duration, 2),
                    })

    def private_voice(self, _src_sub, _dst_sub, _ts, _end, _peerid, _data):
        logger.debug('(%s) Private Voice Packet: From %s, Peer %s, Dst %s',
                     self._system, int_id(_src_sub), int_id(_peerid), int_id(_dst_sub))

    def group_data(self, _src_sub, _dst_sub, _ts, _end, _peerid, _data):
        logger.debug('(%s) Group Data Packet: From %s, Peer %s, Dst %s',
                     self._system, int_id(_src_sub), int_id(_peerid), int_id(_dst_sub))

    def private_data(self, _src_sub, _dst_sub, _ts, _end, _peerid, _data):
        logger.debug('(%s) Private Data Packet: From %s, Peer %s, Dst %s',
                     self._system, int_id(_src_sub), int_id(_peerid), int_id(_dst_sub))

    def unknown_message(self, _packettype, _peerid, _data):
        logger.error('(%s) Unknown Message Type: %s From: %s',
                     self._system, ahex(_packettype), int_id(_peerid))

    # -----------------------------------------------------------------------
    # IPSC maintenance
    # -----------------------------------------------------------------------

    def valid_peer(self, _peerid):
        return _peerid in self._peers

    def valid_master(self, _peerid):
        return self._master['RADIO_ID'] == _peerid

    def de_register_peer(self, _peerid):
        if _peerid in self._peers:
            del self._peers[_peerid]
            logger.info('(%s) Peer De-Registered: %s', self._system, int_id(_peerid))
        elif self.valid_master(_peerid):
            self._master_stat['CONNECTED']    = False
            self._master_stat['CONNECT_TIME'] = 0
            self._master_stat['PEER_LIST']    = False
            logger.info('(%s) Master De-Registered: %s', self._system, int_id(_peerid))
        else:
            logger.warning('(%s) De-Registration from unknown source: %s',
                           self._system, int_id(_peerid))

    def de_register_self(self):
        logger.info('(%s) De-Registering self from IPSC', self._system)
        pkt = self.hashed_packet(self._local['AUTH_KEY'], self.DE_REG_REQ_PKT)
        self.send_to_ipsc(pkt)

    def process_peer_list(self, _data):
        _temp_peers = []
        _peer_list_length = int.from_bytes(_data[5:7], 'big')
        self._local['NUM_PEERS'] = _peer_list_length // 11
        logger.info('(%s) Peer List Received: %s peers in this IPSC',
                    self._system, self._local['NUM_PEERS'])

        for i in range(7, _peer_list_length + 7, 11):
            _hex_radio_id = _data[i:i+4]
            _ip_address   = IPAddr(_data[i+4:i+8])
            _port         = int.from_bytes(_data[i+8:i+10], 'big')
            _hex_mode     = _data[i+10:i+11]

            _temp_peers.append(_hex_radio_id)
            _decoded_mode = process_mode_byte(_hex_mode)

            if _hex_radio_id in self._peers:
                self._peers[_hex_radio_id]['IP']          = _ip_address
                self._peers[_hex_radio_id]['PORT']        = _port
                self._peers[_hex_radio_id]['MODE']        = _hex_mode
                self._peers[_hex_radio_id]['MODE_DECODE'] = _decoded_mode
                self._peers[_hex_radio_id]['FLAGS']       = b''
                self._peers[_hex_radio_id]['FLAGS_DECODE']= ''
                logger.debug('(%s) Peer Updated: %s', self._system, int_id(_hex_radio_id))
            else:
                self._peers[_hex_radio_id] = {
                    'IP':          _ip_address,
                    'PORT':        _port,
                    'MODE':        _hex_mode,
                    'MODE_DECODE': _decoded_mode,
                    'FLAGS':       b'',
                    'FLAGS_DECODE':'',
                    'STATUS': {
                        'CONNECTED':               False,
                        'CONNECT_TIME':            0,
                        'KEEP_ALIVES_SENT':        0,
                        'KEEP_ALIVES_MISSED':      0,
                        'KEEP_ALIVES_OUTSTANDING': 0,
                        'KEEP_ALIVES_RECEIVED':    0,
                        'KEEP_ALIVE_RX_TIME':      0,
                    },
                }
                logger.debug('(%s) Peer Added: %s', self._system, int_id(_hex_radio_id))

        for peer in list(self._peers.keys()):
            if peer not in _temp_peers:
                self.de_register_peer(peer)
                logger.warning('(%s) Peer Deleted (not in new peer list): %s',
                               self._system, int_id(peer))

    def send_packet(self, _packet, addr):
        _host, _port = addr
        if self._local['AUTH_ENABLED']:
            _hash = bhex(hmac_new(self._local['AUTH_KEY'], _packet, sha1).hexdigest()[:20])
            _packet = _packet + _hash
        self.transport.sendto(_packet, (_host, _port))

    def send_to_ipsc(self, _packet):
        if self._local['AUTH_ENABLED']:
            _hash = bhex(hmac_new(self._local['AUTH_KEY'], _packet, sha1).hexdigest()[:20])
            _packet = _packet + _hash
        if self._master['STATUS']['CONNECTED']:
            self.transport.sendto(_packet, (self._master['IP'], self._master['PORT']))
        for peer in list(self._peers):
            if self._peers[peer]['STATUS']['CONNECTED']:
                self.transport.sendto(_packet, (self._peers[peer]['IP'], self._peers[peer]['PORT']))

    def transmit_group_voice(self, _src_sub, _dst_group, _ts, _burst_type, _data, _src_peer=None):
        """Broadcast a GROUP_VOICE packet to all IPSC peers and track TX call state."""
        _now = time()
        if _burst_type == VOICE_HEAD:
            if self.tx_start[_ts] == 0 or (_now - self.tx_start[_ts]) > TS_CLEAR_TIME:
                self.tx_start[_ts] = _now
                logger.info('(%s) GROUP VOICE TX START: TS: %s, TGID: %s, Src: %s, SrcPeer: %s',
                            self._system, _ts, int_id(_dst_group),
                            int_id(_src_sub), int_id(_src_peer) if _src_peer else '—')
                if self._report:
                    self._report.send_bridge_event({
                        'event':    'GROUP VOICE TX START',
                        'system':   self._system,
                        'ts':       _ts,
                        'tgid':     int_id(_dst_group),
                        'src':      int_id(_src_sub),
                        'src_peer': int_id(_src_peer) if _src_peer else None,
                    })
        elif _burst_type == VOICE_TERM:
            if self.tx_start[_ts] > 0:
                _duration = _now - self.tx_start[_ts]
                self.tx_start[_ts] = 0
                logger.info('(%s) GROUP VOICE TX END: TS: %s, TGID: %s Duration: %.2fs',
                            self._system, _ts, int_id(_dst_group), _duration)
                if self._report:
                    self._report.send_bridge_event({
                        'event':    'GROUP VOICE TX END',
                        'system':   self._system,
                        'ts':       _ts,
                        'tgid':     int_id(_dst_group),
                        'duration': round(_duration, 2),
                    })
        self.send_to_ipsc(_data)

    def hashed_packet(self, _key, _data):
        _hash = bhex(hmac_new(_key, _data, sha1).hexdigest()[:20])
        return _data + _hash

    def strip_hash(self, _data):
        return _data[:-10]

    def validate_auth(self, _key, _data):
        _payload  = self.strip_hash(_data)
        _hash     = _data[-10:]
        _chk_hash = bhex(hmac_new(_key, _payload, sha1).hexdigest()[:20])
        return compare_digest(_chk_hash, _hash)

    def reset_keep_alive(self, _peerid):
        if _peerid in self._peers:
            self._peers[_peerid]['STATUS']['KEEP_ALIVES_OUTSTANDING'] = 0
            self._peers[_peerid]['STATUS']['KEEP_ALIVE_RX_TIME']      = int(time())
        if _peerid == self._master['RADIO_ID']:
            self._master_stat['KEEP_ALIVES_OUTSTANDING'] = 0

    # -----------------------------------------------------------------------
    # Maintenance loop handlers
    # -----------------------------------------------------------------------

    def peer_alive_req(self, _data, _peerid, _host, _port):
        _hex_mode     = _data[5:6]
        _hex_flags    = _data[6:10]
        self._peers[_peerid]['MODE']         = _hex_mode
        self._peers[_peerid]['MODE_DECODE']  = process_mode_byte(_hex_mode)
        self._peers[_peerid]['FLAGS']        = _hex_flags
        self._peers[_peerid]['FLAGS_DECODE'] = process_flags_bytes(_hex_flags)
        self.send_packet(self.PEER_ALIVE_REPLY_PKT, (_host, _port))
        self.reset_keep_alive(_peerid)
        logger.debug('(%s) Keep-Alive reply sent to Peer %s, %s:%s',
                     self._system, int_id(_peerid), _host, _port)

    def peer_reg_req(self, _peerid, _host, _port):
        self.send_packet(self.PEER_REG_REPLY_PKT, (_host, _port))
        logger.info('(%s) Peer Registration Request From: %s, %s:%s',
                    self._system, int_id(_peerid), _host, _port)

    def peer_alive_reply(self, _peerid):
        self.reset_keep_alive(_peerid)
        self._peers[_peerid]['STATUS']['KEEP_ALIVES_RECEIVED'] += 1
        self._peers[_peerid]['STATUS']['KEEP_ALIVE_RX_TIME']   = int(time())
        logger.debug('(%s) Keep-Alive Reply received from Peer %s',
                     self._system, int_id(_peerid))

    def peer_reg_reply(self, _peerid):
        if _peerid in self._peers:
            self._peers[_peerid]['STATUS']['CONNECTED']    = True
            self._peers[_peerid]['STATUS']['CONNECT_TIME'] = int(time())
            logger.info('(%s) Registration Reply From: %s, %s:%s',
                        self._system, int_id(_peerid),
                        self._peers[_peerid]['IP'], self._peers[_peerid]['PORT'])

    def master_alive_reply(self, _peerid):
        self.reset_keep_alive(_peerid)
        self._master['STATUS']['KEEP_ALIVES_RECEIVED'] += 1
        self._master['STATUS']['KEEP_ALIVE_RX_TIME']   = int(time())
        logger.debug('(%s) Keep-Alive Reply received from Master %s',
                     self._system, int_id(_peerid))

    def peer_list_reply(self, _data, _peerid):
        self._master['STATUS']['PEER_LIST'] = True
        if len(_data) > 18:
            self.process_peer_list(_data)
        logger.debug('(%s) Peer List Reply Received From Master %s',
                     self._system, int_id(_peerid))

    def master_reg_reply(self, _data, _peerid):
        _hex_mode     = _data[5:6]
        _hex_flags    = _data[6:10]
        _num_peers    = _data[10:12]
        self._local['NUM_PEERS']            = int.from_bytes(_num_peers, 'big')
        self._master['RADIO_ID']            = _peerid
        self._master['MODE']                = _hex_mode
        self._master['MODE_DECODE']         = process_mode_byte(_hex_mode)
        self._master['FLAGS']               = _hex_flags
        self._master['FLAGS_DECODE']        = process_flags_bytes(_hex_flags)
        self._master_stat['CONNECTED']               = True
        self._master_stat['CONNECT_TIME']            = int(time())
        self._master_stat['KEEP_ALIVES_OUTSTANDING'] = 0
        logger.info('(%s) Registration response from Master: %s, %s:%s (%s peers)',
                    self._system, int_id(_peerid),
                    self._master['IP'], self._master['PORT'], self._local['NUM_PEERS'])
        # Request peer list immediately instead of waiting for the next maintenance loop tick
        if self._local['NUM_PEERS']:
            self.send_packet(self.PEER_LIST_REQ_PKT, self._master_sock)
            logger.info('(%s) Requesting peer list from master', self._system)

    def master_reg_req(self, _data, _peerid, _host, _port):
        if not acl_check(_peerid, self._local['REG_ACL']):
            logger.warning('(%s) Peer Registration ***REJECTED BY ACL***: %s %s:%s',
                           self._system, int_id(_peerid), _host, _port)
            return

        _hex_mode  = _data[5:6]
        _hex_flags = _data[6:10]

        is_new = _peerid not in self._peers
        if is_new:
            self._peers[_peerid] = {
                'IP':          _host,
                'PORT':        _port,
                'MODE':        _hex_mode,
                'MODE_DECODE': process_mode_byte(_hex_mode),
                'FLAGS':       _hex_flags,
                'FLAGS_DECODE':process_flags_bytes(_hex_flags),
                'STATUS': {
                    'CONNECTED':               True,
                    'CONNECT_TIME':            int(time()),
                    'KEEP_ALIVES_SENT':        0,
                    'KEEP_ALIVES_MISSED':      0,
                    'KEEP_ALIVES_OUTSTANDING': 0,
                    'KEEP_ALIVES_RECEIVED':    0,
                    'KEEP_ALIVE_RX_TIME':      int(time()),
                },
            }
        else:
            self._peers[_peerid].update({
                'IP': _host, 'PORT': _port,
                'MODE': _hex_mode, 'MODE_DECODE': process_mode_byte(_hex_mode),
                'FLAGS': _hex_flags, 'FLAGS_DECODE': process_flags_bytes(_hex_flags),
            })
            self._peers[_peerid]['STATUS']['KEEP_ALIVE_RX_TIME'] = int(time())

        self._local['NUM_PEERS'] = len(self._peers)

        reg_reply = MASTER_REG_REPLY + self._local_id + self.TS_FLAGS + bytes_2(self._local['NUM_PEERS']) + IPSC_VER
        self.send_packet(reg_reply, (_host, _port))
        # Push current peer list immediately; no need for peer to request it first
        self.send_packet(self.PEER_LIST_REPLY_PKT + build_peer_list(self._peers), (_host, _port))

        if is_new:
            logger.info('(%s) Peer Registered: %s, %s:%s (IPSC now has %s peers)',
                        self._system, int_id(_peerid), _host, _port, self._local['NUM_PEERS'])
            # Push updated peer list to all existing registered peers
            for existing_id, ep in list(self._peers.items()):
                if existing_id != _peerid:
                    self.send_packet(self.PEER_LIST_REPLY_PKT + build_peer_list(self._peers),
                                     (ep['IP'], ep['PORT']))
        else:
            logger.info('(%s) Peer Re-Registered: %s, %s:%s',
                        self._system, int_id(_peerid), _host, _port)

    def master_alive_req(self, _peerid, _host, _port):
        if _peerid in self._peers:
            self._peers[_peerid]['STATUS']['KEEP_ALIVES_RECEIVED'] += 1
            self._peers[_peerid]['STATUS']['KEEP_ALIVE_RX_TIME']   = int(time())
            self.send_packet(self.MASTER_ALIVE_REPLY_PKT, (_host, _port))
            logger.debug('(%s) Master Keep-Alive Request from peer %s, %s:%s',
                         self._system, int_id(_peerid), _host, _port)
        else:
            logger.warning('(%s) Master Keep-Alive from *UNREGISTERED* peer %s, %s:%s',
                           self._system, int_id(_peerid), _host, _port)

    def peer_list_req(self, _peerid, _host, _port):
        if _peerid in self._peers:
            logger.debug('(%s) Peer List Request from peer %s', self._system, int_id(_peerid))
            self.send_packet(self.PEER_LIST_REPLY_PKT + build_peer_list(self._peers), (_host, _port))
        else:
            logger.warning('(%s) Peer List Request from *UNREGISTERED* peer %s',
                           self._system, int_id(_peerid))

    # -----------------------------------------------------------------------
    # Connection maintenance loops (run_periodic callbacks)
    # -----------------------------------------------------------------------

    def master_maintenance_loop(self):
        logger.debug('(%s) MASTER Maintenance Loop', self._system)
        update_time = int(time())
        for peer in list(self._peers.keys()):
            delta = update_time - self._peers[peer]['STATUS']['KEEP_ALIVE_RX_TIME']
            logger.debug('(%s) Time since last KA from Peer %s: %ss',
                         self._system, int_id(peer), delta)
            if delta > 120:
                self.de_register_peer(peer)
                self.send_to_ipsc(self.PEER_LIST_REPLY_PKT + build_peer_list(self._peers))
                logger.warning('(%s) Timeout exceeded for Peer %s — de-registering',
                               self._system, int_id(peer))

    def peer_maintenance_loop(self):
        logger.debug('(%s) PEER Maintenance Loop', self._system)

        if not self._master_stat['CONNECTED']:
            self.send_packet(self.MASTER_REG_REQ_PKT, self._master_sock)
            logger.info('(%s) Registering with Master: %s:%s',
                        self._system, self._master['IP'], self._master['PORT'])

        elif self._master_stat['CONNECTED']:
            self.send_packet(self.MASTER_ALIVE_PKT, self._master_sock)
            logger.debug('(%s) Keep-Alive sent to Master: %s, %s:%s',
                         self._system, int_id(self._master['RADIO_ID']),
                         self._master['IP'], self._master['PORT'])

            if self._master_stat['KEEP_ALIVES_OUTSTANDING'] > 0:
                self._master_stat['KEEP_ALIVES_MISSED'] += 1
                logger.info('(%s) Master Keep-Alive Missed: %s:%s',
                            self._system, self._master['IP'], self._master['PORT'])

            if self._master_stat['KEEP_ALIVES_OUTSTANDING'] >= self._local['MAX_MISSED']:
                self._master_stat['CONNECTED']                = False
                self._master_stat['CONNECT_TIME']             = 0
                self._master_stat['KEEP_ALIVES_OUTSTANDING']  = 0
                logger.error('(%s) Max Master Keep-Alives Missed — de-registering Master: %s:%s',
                             self._system, self._master['IP'], self._master['PORT'])

            self._master_stat['KEEP_ALIVES_SENT']        += 1
            self._master_stat['KEEP_ALIVES_OUTSTANDING'] += 1

        else:
            logger.error('(%s) Master in UNKNOWN STATE: %s:%s',
                         self._system, self._master['IP'], self._master['PORT'])
            self._master_stat['CONNECTED']    = False
            self._master_stat['CONNECT_TIME'] = 0

        if self._master_stat['CONNECTED'] and not self._master_stat['PEER_LIST']:
            if self._local['NUM_PEERS']:
                self.send_packet(self.PEER_LIST_REQ_PKT, self._master_sock)
                logger.info('(%s) No Peer List — Requesting One From the Master', self._system)
            else:
                self._master_stat['PEER_LIST'] = True
                logger.debug('(%s) Skip peer list request — we are the only Peer', self._system)

        if self._master_stat['PEER_LIST']:
            for peer in list(self._peers.keys()):
                if peer == self._local_id:
                    continue

                if not self._peers[peer]['STATUS']['CONNECTED']:
                    self.send_packet(self.PEER_REG_REQ_PKT,
                                     (self._peers[peer]['IP'], self._peers[peer]['PORT']))
                    logger.info('(%s) Registering with Peer %s, %s:%s',
                                self._system, int_id(peer),
                                self._peers[peer]['IP'], self._peers[peer]['PORT'])

                elif self._peers[peer]['STATUS']['CONNECTED']:
                    self.send_packet(self.PEER_ALIVE_REQ_PKT,
                                     (self._peers[peer]['IP'], self._peers[peer]['PORT']))
                    logger.debug('(%s) Keep-Alive sent to Peer %s, %s:%s',
                                 self._system, int_id(peer),
                                 self._peers[peer]['IP'], self._peers[peer]['PORT'])

                    if self._peers[peer]['STATUS']['KEEP_ALIVES_OUTSTANDING'] > 0:
                        self._peers[peer]['STATUS']['KEEP_ALIVES_MISSED'] += 1
                        logger.info('(%s) Peer Keep-Alive Missed: %s, %s:%s',
                                    self._system, int_id(peer),
                                    self._peers[peer]['IP'], self._peers[peer]['PORT'])

                    if self._peers[peer]['STATUS']['KEEP_ALIVES_OUTSTANDING'] >= self._local['MAX_MISSED']:
                        self._peers[peer]['STATUS']['CONNECTED']               = False
                        logger.warning('(%s) Max Peer Keep-Alives Missed — de-registering Peer: %s, %s:%s',
                                       self._system, int_id(peer),
                                       self._peers[peer]['IP'], self._peers[peer]['PORT'])

                    self._peers[peer]['STATUS']['KEEP_ALIVES_SENT']        += 1
                    self._peers[peer]['STATUS']['KEEP_ALIVES_OUTSTANDING'] += 1


# ---------------------------------------------------------------------------
# __main__
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    import os
    import sys

    from config import build_config
    from log import config_logging

    os.chdir(os.path.dirname(os.path.realpath(sys.argv[0])))

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config',     action='store', dest='CFG_FILE',
                        help='/full/path/to/dmrlink.cfg')
    parser.add_argument('-ll', '--log_level', action='store', dest='LOG_LEVEL',
                        help='Override config file log level')
    parser.add_argument('-lh', '--log_handle',action='store', dest='LOG_HANDLERS',
                        help='Override config file log handlers')
    cli_args = parser.parse_args()

    if not cli_args.CFG_FILE:
        cli_args.CFG_FILE = os.path.dirname(os.path.abspath(__file__)) + '/dmrlink.cfg'

    CONFIG = build_config(cli_args.CFG_FILE)

    if cli_args.LOG_LEVEL:
        CONFIG['LOGGER']['LOG_LEVEL'] = cli_args.LOG_LEVEL
    if cli_args.LOG_HANDLERS:
        CONFIG['LOGGER']['LOG_HANDLERS'] = cli_args.LOG_HANDLERS

    config_logging(CONFIG['LOGGER'])
    logger.info("DMRlink3 'dmrlink.py' (c) 2013-2026 N0MJS & the K0USY Group — SYSTEM STARTING...")

    async def async_main():
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def sig_handler(sig):
            logger.info('*** DMRlink3 TERMINATING WITH SIGNAL %s ***', signal.Signals(sig).name)
            for system in systems:
                systems[system].de_register_self()
            stop_event.set()

        for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGQUIT]:
            loop.add_signal_handler(sig, sig_handler, sig)

        report_server = config_reports(CONFIG, ReportServer)
        build_aliases(CONFIG)
        await mk_ipsc_systems(CONFIG, systems, IPSC, report_server)
        await stop_event.wait()

    asyncio.run(async_main())
