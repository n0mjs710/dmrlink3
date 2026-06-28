#!/usr/bin/env python
#
###############################################################################
#   Copyright (C) 2016-2026  Cortney T. Buffington, N0MJS <n0mjs@me.com>
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

# This application bridges traffic between IPSC systems using rule files.
#
# bridge_rules.py defines IPSC network, timeslot, and TGID matching rules that
# determine which voice calls are bridged between systems.

import asyncio
import json
import logging
import signal
import sys

from binascii import b2a_hex as ahex
from importlib.util import spec_from_file_location, module_from_spec
from time import time

from dmr_utils3.utils import bytes_3, bytes_4, int_id

from config import acl_check, process_acls
from dmrlink import (IPSC, ReportServer, TS_CLEAR_TIME, build_aliases, config_reports,
                     mk_ipsc_systems, run_periodic, systems)
from const import (GV_BURST_TYPE_OFF, VOICE_HEAD, VOICE_TERM, SLOT1_VOICE, SLOT2_VOICE)
from trunk import TRUNK


__author__      = 'Cortney T. Buffington, N0MJS'
__copyright__   = 'Copyright (c) 2016-2026 Cortney T. Buffington, N0MJS and the K0USY Group'
__credits__     = 'Adam Fast, KC0YLK; Dave Kierzkowski, KD8EYF; Steve Zingman, N4IRS; Mike Zingman, N4IRR'
__license__     = 'GNU GPLv3'
__maintainer__  = 'Cort Buffington, N0MJS'
__email__       = 'n0mjs@me.com'

logger = logging.getLogger(__name__)

# Bridge state; populated in async_main after rules file is loaded
BRIDGES          = {}
TRUNKS           = []
BRIDGE_CONF      = {}
BRIDGE_SRC_INDEX = {}   # (system, ts, tgid) -> [(bridge, member, all_members)]
BRIDGE_BY_SYSTEM = {}   # system -> [(bridge, member)]

# Alias dicts; populated on startup in async_main (dashboard owns downloads/refresh)
peer_ids       = {}
subscriber_ids = {}
talkgroup_ids  = {}
local_ids      = {}


# ---------------------------------------------------------------------------
# Bridge rules loader
# ---------------------------------------------------------------------------

def _load_file_module(path, label):
    """Load a Python file by path and return it as a module object."""
    spec = spec_from_file_location(label, path)
    if spec is None:
        return None
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_bridge_config(_bridge_rules):
    try:
        bridge_file = _load_file_module(_bridge_rules, 'bridge_rules')
        if bridge_file is None:
            raise FileNotFoundError(_bridge_rules)
        logger.info('Bridge configuration file found and imported')
    except FileNotFoundError:
        logger.critical('Bridge rules file "%s" not found', _bridge_rules)
        sys.exit(1)
    except Exception as e:
        logger.critical('Error loading bridge rules file "%s": %s', _bridge_rules, e, exc_info=True)
        sys.exit(1)

    for _bridge in bridge_file.BRIDGES:
        for _system in bridge_file.BRIDGES[_bridge]:
            if _system['SYSTEM'] not in CONFIG['SYSTEMS']:
                sys.exit('ERROR: Conference bridge configured for system not found in main config')

            _system['TGID']   = bytes_3(_system['TGID'])
            _system['ON']     = frozenset(bytes_3(x) for x in _system['ON'])
            _system['OFF']    = frozenset(bytes_3(x) for x in _system['OFF'])
            _system['RESET']  = frozenset(bytes_3(x) for x in _system['RESET'])
            _system['TIMEOUT'] = _system['TIMEOUT'] * 60
            _system['TIMER']   = time() + _system['TIMEOUT']

    return {
        'BRIDGE_CONF': bridge_file.BRIDGE_CONF,
        'BRIDGES':     bridge_file.BRIDGES,
        'TRUNKS':      bridge_file.TRUNKS,
    }


def index_bridges(_bridges):
    src_index = {}
    by_system = {}
    for _bridge in _bridges:
        _members = _bridges[_bridge]
        for _member in _members:
            src_index.setdefault((_member['SYSTEM'], _member['TS'], _member['TGID']), []).append(
                (_bridge, _member, _members))
            by_system.setdefault(_member['SYSTEM'], []).append((_bridge, _member))
    return src_index, by_system


# ---------------------------------------------------------------------------
# Rule timer loop (runs every 60 seconds)
# ---------------------------------------------------------------------------

def rule_timer_loop(_report=None):
    logger.debug('(ALL IPSC SYSTEMS) Rule timer loop')
    _now = time()
    _changed = False

    for _bridge in BRIDGES:
        for _system in BRIDGES[_bridge]:
            if _system['TO_TYPE'] == 'ON':
                if _system['ACTIVE']:
                    if _system['TIMER'] < _now:
                        _system['ACTIVE'] = False
                        _changed = True
                        logger.info('Bridge TIMEOUT: DEACTIVATE System: %s, Bridge: %s, TS: %s, TGID: %s',
                                    _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']))
                    else:
                        logger.info('Bridge ACTIVE (ON timer): System: %s Bridge: %s TS: %s TGID: %s Timeout in: %.0fs',
                                    _system['SYSTEM'], _bridge, _system['TS'],
                                    int_id(_system['TGID']), _system['TIMER'] - _now)
                else:
                    logger.debug('Bridge INACTIVE (no change): System: %s Bridge: %s TS: %s TGID: %s',
                                 _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']))
            elif _system['TO_TYPE'] == 'OFF':
                if not _system['ACTIVE']:
                    if _system['TIMER'] < _now:
                        _system['ACTIVE'] = True
                        _changed = True
                        logger.info('Bridge TIMEOUT: ACTIVATE System: %s, Bridge: %s, TS: %s, TGID: %s',
                                    _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']))
                    else:
                        logger.info('Bridge INACTIVE (OFF timer): System: %s Bridge: %s TS: %s TGID: %s Timeout in: %.0fs',
                                    _system['SYSTEM'], _bridge, _system['TS'],
                                    int_id(_system['TGID']), _system['TIMER'] - _now)
                else:
                    logger.debug('Bridge ACTIVE (no change): System: %s Bridge: %s TS: %s TGID: %s',
                                 _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']))
            else:
                logger.debug('Bridge NO ACTION: System: %s, Bridge: %s, TS: %s, TGID: %s',
                             _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']))

    if _changed and _report:
        _report.send_bridge()


# ---------------------------------------------------------------------------
# Report server with bridge support
# ---------------------------------------------------------------------------

class BridgeReportServer(ReportServer):

    def send_bridge(self):
        bridge_data = {}
        for name, bridge in BRIDGES.items():
            bridge_data[name] = []
            for entry in bridge:
                bridge_data[name].append({
                    'SYSTEM':  entry['SYSTEM'],
                    'TS':      entry['TS'],
                    'TGID':    int_id(entry['TGID']),
                    'ACTIVE':  entry['ACTIVE'],
                    'TO_TYPE': entry['TO_TYPE'],
                    'TIMEOUT': entry['TIMEOUT'],
                    'TIMER':   entry['TIMER'],
                    'ON':      [int_id(t) for t in entry['ON']],
                    'OFF':     [int_id(t) for t in entry['OFF']],
                    'RESET':   [int_id(t) for t in entry['RESET']],
                })
        self._send_json({'type': 'bridge', 'bridges': bridge_data})

    def send_bridge_event(self, _event):
        self._send_json({'type': 'bridge_event', 'data': _event})


# ---------------------------------------------------------------------------
# Bridge IPSC subclass
# ---------------------------------------------------------------------------

class bridgeIPSC(IPSC):
    def __init__(self, _name, _config, _report):
        IPSC.__init__(self, _name, _config, _report)

        self.STATUS = {
            1: {'RX_TGID': b'\x00\x00\x00', 'TX_TGID': b'\x00\x00\x00',
                'RX_TIME': 0, 'TX_TIME': 0,
                'RX_SRC_SUB': b'\x00\x00\x00', 'TX_SRC_SUB': b'\x00\x00\x00'},
            2: {'RX_TGID': b'\x00\x00\x00', 'TX_TGID': b'\x00\x00\x00',
                'RX_TIME': 0, 'TX_TIME': 0,
                'RX_SRC_SUB': b'\x00\x00\x00', 'TX_SRC_SUB': b'\x00\x00\x00'},
        }


    def group_voice(self, _src_sub, _dst_group, _ts, _end, _peerid, _data):
        if not acl_check(_src_sub, self._CONFIG['GLOBAL']['SUB_ACL']):
            logger.warning('(%s) Group Voice ***REJECTED BY ACL*** From: %s, Peer %s, Dst %s',
                           self._system, int_id(_src_sub), int_id(_peerid), int_id(_dst_group))
            return
        super().group_voice(_src_sub, _dst_group, _ts, _end, _peerid, _data)

        _burst_data_type = _data[GV_BURST_TYPE_OFF]   # int; use VOICE_HEAD / SLOT1_VOICE etc.
        now              = time()

        # Both ON and OFF triggers fire on key-down (VOICE_HEAD) so the bridge state is
        # current before the VOICE_HEAD itself is forwarded and before any unkey delay.
        if _burst_data_type == VOICE_HEAD:
            _bridge_changed = False
            for _bridge, _system in BRIDGE_BY_SYSTEM.get(self._system, ()):
                if _ts != _system['TS']:
                    continue
                if _dst_group in _system['ON'] or _dst_group in _system['RESET']:
                    if _dst_group in _system['ON'] and not _system['ACTIVE']:
                        _system['ACTIVE'] = True
                        _bridge_changed = True
                        logger.info('(%s) Bridge: %s activated', self._system, _bridge)
                        if _system['TO_TYPE'] == 'OFF':
                            _system['TIMER'] = now
                            logger.info('(%s) Bridge: %s OFF-timer cancelled (activated by ON trigger)', self._system, _bridge)
                    if _system['ACTIVE'] and _system['TO_TYPE'] == 'ON':
                        _system['TIMER'] = now + _system['TIMEOUT']
                        logger.info('(%s) Bridge: %s ON-timer reset to %.0fs', self._system, _bridge, _system['TIMEOUT'])
                if _dst_group in _system['OFF'] or _dst_group in _system['RESET']:
                    if _dst_group in _system['OFF'] and _system['ACTIVE']:
                        _system['ACTIVE'] = False
                        _bridge_changed = True
                        logger.info('(%s) Bridge: %s deactivated', self._system, _bridge)
                        if _system['TO_TYPE'] == 'ON':
                            _system['TIMER'] = now
                            logger.info('(%s) Bridge: %s ON-timer cancelled (deactivated by OFF trigger)', self._system, _bridge)
                    if not _system['ACTIVE'] and _system['TO_TYPE'] == 'OFF':
                        _system['TIMER'] = now + _system['TIMEOUT']
                        logger.info('(%s) Bridge: %s OFF-timer reset to %.0fs', self._system, _bridge, _system['TIMEOUT'])
            if _bridge_changed and self._report:
                self._report.send_bridge()

        for _bridge, _system, _members in BRIDGE_SRC_INDEX.get((self._system, _ts, _dst_group), ()):
            if not _system['ACTIVE']:
                continue

            for _target in _members:
                if _target['SYSTEM'] == self._system:
                    continue
                if not _target['ACTIVE']:
                    continue

                _target_status = systems[_target['SYSTEM']].STATUS
                _target_system = self._CONFIG['SYSTEMS'][_target['SYSTEM']]

                # BEGIN CONTENTION HANDLING
                if _target['SYSTEM'] not in TRUNKS:
                    if ((_target['TGID'] != _target_status[_target['TS']]['RX_TGID']) and
                            ((now - _target_status[_target['TS']]['RX_TIME']) < _target_system['LOCAL']['GROUP_HANGTIME'])):
                        if _burst_data_type == VOICE_HEAD:
                            logger.info('(%s) Call not bridged to TGID %s, target in RX group hangtime: %s TS: %s TGID: %s',
                                        self._system, int_id(_target['TGID']),
                                        _target['SYSTEM'], _target['TS'],
                                        int_id(_target_status[_target['TS']]['RX_TGID']))
                        continue
                    if ((_target['TGID'] != _target_status[_target['TS']]['TX_TGID']) and
                            ((now - _target_status[_target['TS']]['TX_TIME']) < _target_system['LOCAL']['GROUP_HANGTIME'])):
                        if _burst_data_type == VOICE_HEAD:
                            logger.info('(%s) Call not bridged to TGID %s, target in TX group hangtime: %s TS: %s TGID: %s',
                                        self._system, int_id(_target['TGID']),
                                        _target['SYSTEM'], _target['TS'],
                                        int_id(_target_status[_target['TS']]['TX_TGID']))
                        continue
                    if ((_target['TGID'] == _target_status[_target['TS']]['RX_TGID']) and
                            ((now - _target_status[_target['TS']]['RX_TIME']) < TS_CLEAR_TIME)):
                        if _burst_data_type == VOICE_HEAD:
                            logger.info('(%s) Call not bridged to TGID %s, matching call active on target: %s TS: %s TGID: %s',
                                        self._system, int_id(_target['TGID']),
                                        _target['SYSTEM'], _target['TS'],
                                        int_id(_target_status[_target['TS']]['RX_TGID']))
                        continue
                    if ((_target['TGID'] == _target_status[_target['TS']]['TX_TGID']) and
                            (_src_sub != _target_status[_target['TS']]['TX_SRC_SUB']) and
                            ((now - _target_status[_target['TS']]['TX_TIME']) < TS_CLEAR_TIME)):
                        if _burst_data_type == VOICE_HEAD:
                            logger.info('(%s) Call not bridged for sub %s, bridge in progress on target: %s TS: %s TGID: %s SUB: %s',
                                        self._system, int_id(_src_sub),
                                        _target['SYSTEM'], _target['TS'],
                                        int_id(_target_status[_target['TS']]['TX_TGID']),
                                        int_id(_target_status[_target['TS']]['TX_SRC_SUB']))
                        continue
                # END CONTENTION HANDLING

                # BEGIN FRAME FORWARDING
                _tmp_data = _data

                # Rewrite Peer ID
                _tmp_data = _tmp_data.replace(_peerid, _target_system['LOCAL']['RADIO_ID'], 1)

                # Rewrite IPSC SRC + DST GROUP
                _tmp_data = _tmp_data.replace(_src_sub + _dst_group, _src_sub + _target['TGID'], 1)

                # Rewrite DST GROUP + IPSC SRC in DMR LC
                _tmp_data = _tmp_data.replace(_dst_group + _src_sub, _target['TGID'] + _src_sub, 1)

                # Rewrite IPSC timeslot byte
                _call_info = int_id(_data[17:18])
                if _target['TS'] == 1:
                    _call_info &= ~(1 << 5)
                elif _target['TS'] == 2:
                    _call_info |= 1 << 5
                _tmp_data = _tmp_data[:17] + bytes([_call_info]) + _tmp_data[18:]

                # Rewrite DMR timeslot in burst data
                if _burst_data_type in (SLOT1_VOICE, SLOT2_VOICE):
                    _new_burst = SLOT1_VOICE if _target['TS'] == 1 else SLOT2_VOICE
                    _tmp_data = _tmp_data[:30] + bytes([_new_burst]) + _tmp_data[31:]

                systems[_target['SYSTEM']].transmit_group_voice(
                    _src_sub, _target['TGID'], _target['TS'], _burst_data_type, _tmp_data, _peerid)
                # END FRAME FORWARDING

                if _target['SYSTEM'] not in TRUNKS:
                    _target_status[_target['TS']]['TX_TGID']   = _target['TGID']
                    _target_status[_target['TS']]['TX_TIME']    = now
                    _target_status[_target['TS']]['TX_SRC_SUB'] = _src_sub

        # Record RX state for contention handler
        self.STATUS[_ts]['RX_TGID'] = _dst_group
        self.STATUS[_ts]['RX_TIME'] = now



# ---------------------------------------------------------------------------
# Bridge-aware TRUNK subclass
# ---------------------------------------------------------------------------

class bridgeTRUNK(TRUNK):
    """
    Bridge-aware Trunk v2 system.

    Extends TRUNK with the full bridge routing logic so that inbound trunk
    packets are forwarded to other systems according to BRIDGES rules,
    exactly as bridgeIPSC does for IPSC sources.

    NOTE: The routing logic in group_voice() below is intentionally parallel
    to bridgeIPSC.group_voice().  A future refactor should extract a shared
    _route_group_voice() helper to eliminate the duplication.  For now the
    logic is kept inline to avoid changing working bridgeIPSC code while
    trunk v2 is being established.
    """

    def group_voice(self, _src_sub, _dst_group, _ts, _end, _peerid, _data):
        if not acl_check(_src_sub, self._CONFIG['GLOBAL']['SUB_ACL']):
            logger.warning('(%s) TRUNK Group Voice ***REJECTED BY ACL*** From: %s, Peer %s, Dst %s',
                           self._system, int_id(_src_sub), int_id(_peerid), int_id(_dst_group))
            return

        _burst_data_type = _data[GV_BURST_TYPE_OFF]
        now = time()

        # Process ON/OFF/RESET bridge triggers on key-down so bridge state is
        # current before this VOICE_HEAD is forwarded (same as bridgeIPSC).
        if _burst_data_type == VOICE_HEAD:
            _bridge_changed = False
            for _bridge, _system in BRIDGE_BY_SYSTEM.get(self._system, ()):
                if _ts != _system['TS']:
                    continue
                if _dst_group in _system['ON'] or _dst_group in _system['RESET']:
                    if _dst_group in _system['ON'] and not _system['ACTIVE']:
                        _system['ACTIVE'] = True
                        _bridge_changed = True
                        logger.info('(%s) Bridge: %s activated', self._system, _bridge)
                        if _system['TO_TYPE'] == 'OFF':
                            _system['TIMER'] = now
                            logger.info('(%s) Bridge: %s OFF-timer cancelled (activated by ON trigger)',
                                        self._system, _bridge)
                    if _system['ACTIVE'] and _system['TO_TYPE'] == 'ON':
                        _system['TIMER'] = now + _system['TIMEOUT']
                        logger.info('(%s) Bridge: %s ON-timer reset to %.0fs',
                                    self._system, _bridge, _system['TIMEOUT'])
                if _dst_group in _system['OFF'] or _dst_group in _system['RESET']:
                    if _dst_group in _system['OFF'] and _system['ACTIVE']:
                        _system['ACTIVE'] = False
                        _bridge_changed = True
                        logger.info('(%s) Bridge: %s deactivated', self._system, _bridge)
                        if _system['TO_TYPE'] == 'ON':
                            _system['TIMER'] = now
                            logger.info('(%s) Bridge: %s ON-timer cancelled (deactivated by OFF trigger)',
                                        self._system, _bridge)
                    if not _system['ACTIVE'] and _system['TO_TYPE'] == 'OFF':
                        _system['TIMER'] = now + _system['TIMEOUT']
                        logger.info('(%s) Bridge: %s OFF-timer reset to %.0fs',
                                    self._system, _bridge, _system['TIMEOUT'])
            if _bridge_changed and self._report:
                self._report.send_bridge()

        for _bridge, _system, _members in BRIDGE_SRC_INDEX.get((self._system, _ts, _dst_group), ()):
            if not _system['ACTIVE']:
                continue
            for _target in _members:
                if _target['SYSTEM'] == self._system:
                    continue
                if not _target['ACTIVE']:
                    continue

                _target_status = systems[_target['SYSTEM']].STATUS
                _target_system = self._CONFIG['SYSTEMS'][_target['SYSTEM']]

                # Contention handling (bypassed for trunk targets via TRUNKS list)
                if _target['SYSTEM'] not in TRUNKS:
                    if ((_target['TGID'] != _target_status[_target['TS']]['RX_TGID']) and
                            ((now - _target_status[_target['TS']]['RX_TIME']) < _target_system['LOCAL']['GROUP_HANGTIME'])):
                        if _burst_data_type == VOICE_HEAD:
                            logger.info('(%s) Call not bridged to TGID %s, target in RX group hangtime: %s TS: %s TGID: %s',
                                        self._system, int_id(_target['TGID']),
                                        _target['SYSTEM'], _target['TS'],
                                        int_id(_target_status[_target['TS']]['RX_TGID']))
                        continue
                    if ((_target['TGID'] != _target_status[_target['TS']]['TX_TGID']) and
                            ((now - _target_status[_target['TS']]['TX_TIME']) < _target_system['LOCAL']['GROUP_HANGTIME'])):
                        if _burst_data_type == VOICE_HEAD:
                            logger.info('(%s) Call not bridged to TGID %s, target in TX group hangtime: %s TS: %s TGID: %s',
                                        self._system, int_id(_target['TGID']),
                                        _target['SYSTEM'], _target['TS'],
                                        int_id(_target_status[_target['TS']]['TX_TGID']))
                        continue
                    if ((_target['TGID'] == _target_status[_target['TS']]['RX_TGID']) and
                            ((now - _target_status[_target['TS']]['RX_TIME']) < TS_CLEAR_TIME)):
                        if _burst_data_type == VOICE_HEAD:
                            logger.info('(%s) Call not bridged to TGID %s, matching call active on target: %s TS: %s TGID: %s',
                                        self._system, int_id(_target['TGID']),
                                        _target['SYSTEM'], _target['TS'],
                                        int_id(_target_status[_target['TS']]['RX_TGID']))
                        continue
                    if ((_target['TGID'] == _target_status[_target['TS']]['TX_TGID']) and
                            (_src_sub != _target_status[_target['TS']]['TX_SRC_SUB']) and
                            ((now - _target_status[_target['TS']]['TX_TIME']) < TS_CLEAR_TIME)):
                        if _burst_data_type == VOICE_HEAD:
                            logger.info('(%s) Call not bridged for sub %s, bridge in progress on target: %s TS: %s TGID: %s SUB: %s',
                                        self._system, int_id(_src_sub),
                                        _target['SYSTEM'], _target['TS'],
                                        int_id(_target_status[_target['TS']]['TX_TGID']),
                                        int_id(_target_status[_target['TS']]['TX_SRC_SUB']))
                        continue

                # Frame forwarding (same rewrites as bridgeIPSC)
                _tmp_data = _data
                _tmp_data = _tmp_data.replace(_peerid, _target_system['LOCAL']['RADIO_ID'], 1)
                _tmp_data = _tmp_data.replace(_src_sub + _dst_group, _src_sub + _target['TGID'], 1)
                _tmp_data = _tmp_data.replace(_dst_group + _src_sub, _target['TGID'] + _src_sub, 1)
                _call_info = int_id(_data[17:18])
                if _target['TS'] == 1:
                    _call_info &= ~(1 << 5)
                elif _target['TS'] == 2:
                    _call_info |= 1 << 5
                _tmp_data = _tmp_data[:17] + bytes([_call_info]) + _tmp_data[18:]
                if _burst_data_type in (SLOT1_VOICE, SLOT2_VOICE):
                    _new_burst = SLOT1_VOICE if _target['TS'] == 1 else SLOT2_VOICE
                    _tmp_data = _tmp_data[:30] + bytes([_new_burst]) + _tmp_data[31:]

                systems[_target['SYSTEM']].transmit_group_voice(
                    _src_sub, _target['TGID'], _target['TS'], _burst_data_type, _tmp_data, _peerid)

                if _target['SYSTEM'] not in TRUNKS:
                    _target_status[_target['TS']]['TX_TGID']    = _target['TGID']
                    _target_status[_target['TS']]['TX_TIME']     = now
                    _target_status[_target['TS']]['TX_SRC_SUB']  = _src_sub

        # Trunk STATUS is {} — RX state writes are omitted; values are only
        # consulted by contention checks, which are bypassed for trunk targets.


# ---------------------------------------------------------------------------
# TRUNK system factory
# ---------------------------------------------------------------------------

async def mk_trunk_systems(_config, _systems, _report_server):
    """Instantiate and bind UDP endpoints for all TRUNK-type systems in config.

    Called from async_main() after mk_ipsc_systems() so that TRUNK and IPSC
    systems share the same global systems dict.  TRUNK systems are skipped by
    mk_ipsc_systems() (which only handles IPSC MASTER/PEER systems).
    """
    loop = asyncio.get_running_loop()
    for system in _config['SYSTEMS']:
        if _config['SYSTEMS'][system].get('SYSTEM_TYPE') != 'TRUNK':
            continue
        _systems[system] = bridgeTRUNK(system, _config, _report_server)
        proto = _systems[system]
        ip    = _config['SYSTEMS'][system]['LOCAL']['IP'] or '0.0.0.0'
        port  = _config['SYSTEMS'][system]['LOCAL']['PORT']
        await loop.create_datagram_endpoint(
            lambda p=proto: p,
            local_addr=(ip, port),
        )
        logger.info('(%s) TRUNK UDP endpoint bound on %s:%s', system, ip, port)
    return _systems


# ---------------------------------------------------------------------------
# __main__
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    import os

    from config import build_config
    from log import config_logging

    os.chdir(os.path.dirname(os.path.realpath(sys.argv[0])))

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config',      action='store', dest='CFG_FILE',
                        help='/full/path/to/dmrlink.cfg')
    parser.add_argument('-b', '--bridge-rules', action='store', dest='BRIDGE_RULES',
                        default='bridge_rules.py',
                        help='path to bridge rules file (default: bridge_rules.py)')
    parser.add_argument('-ll', '--log_level',   action='store', dest='LOG_LEVEL',
                        help='Override config file log level')
    parser.add_argument('-lh', '--log_handle',  action='store', dest='LOG_HANDLERS',
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
    logger.info("DMRlink3 'bridge.py' (c) 2016-2026 N0MJS & the K0USY Group — SYSTEM STARTING...")

    async def async_main():
        global BRIDGES, TRUNKS, BRIDGE_CONF

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def sig_handler(sig):
            logger.info('*** DMRlink3 bridge.py TERMINATING WITH SIGNAL %s ***', signal.Signals(sig).name)
            for system in systems:
                systems[system].de_register_self()
            stop_event.set()

        for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGQUIT]:
            loop.add_signal_handler(sig, sig_handler, sig)

        report_server = config_reports(CONFIG, BridgeReportServer)

        global peer_ids, subscriber_ids, talkgroup_ids, local_ids
        peer_ids, subscriber_ids, talkgroup_ids, local_ids = build_aliases(CONFIG)

        await mk_ipsc_systems(CONFIG, systems, bridgeIPSC, report_server)
        await mk_trunk_systems(CONFIG, systems, report_server)

        process_acls(CONFIG)

        CONFIG_DICT = make_bridge_config(cli_args.BRIDGE_RULES)
        BRIDGE_CONF = CONFIG_DICT['BRIDGE_CONF']
        TRUNKS      = CONFIG_DICT['TRUNKS']
        BRIDGES     = CONFIG_DICT['BRIDGES']

        # TRUNK-type systems always bypass contention — add them to TRUNKS
        # automatically so operators don't have to list them in bridge_rules.py.
        for system in CONFIG['SYSTEMS']:
            if CONFIG['SYSTEMS'][system].get('SYSTEM_TYPE') == 'TRUNK':
                if system not in TRUNKS:
                    TRUNKS.append(system)
                    logger.info('(%s) TRUNK auto-added to contention bypass list', system)

        global BRIDGE_SRC_INDEX, BRIDGE_BY_SYSTEM
        BRIDGE_SRC_INDEX, BRIDGE_BY_SYSTEM = index_bridges(BRIDGES)

        # Build and install the fast TGID ingress filter for each trunk system.
        # The filter is derived from bridge rules so it stays in sync with routing
        # without requiring a separate configuration entry.
        for system in CONFIG['SYSTEMS']:
            if CONFIG['SYSTEMS'][system].get('SYSTEM_TYPE') == 'TRUNK':
                tgids = frozenset(
                    member['TGID']
                    for bridge_members in BRIDGES.values()
                    for member in bridge_members
                    if member['SYSTEM'] == system
                )
                systems[system].set_tgid_filter(tgids)

        loop.create_task(run_periodic(60, rule_timer_loop, 'rule_timer', report_server))

        await stop_event.wait()

    asyncio.run(async_main())
