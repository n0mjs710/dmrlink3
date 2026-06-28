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

import bisect
import configparser
import sys
from socket import getaddrinfo, IPPROTO_UDP

import const
from dmr_utils3.utils import int_id

__author__     = 'Cortney T. Buffington, N0MJS'
__copyright__  = 'Copyright (c) 2016-2026 Cortney T. Buffington, N0MJS and the K0USY Group'
__license__    = 'GNU GPLv3'
__maintainer__ = 'Cort Buffington, N0MJS'
__email__      = 'n0mjs@me.com'


def merge_ranges(_ranges):
    merged = []
    for start, end in sorted(_ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def acl_build(_acl, _max):
    if not _acl:
        return (True, frozenset(), (const.ID_MIN,), (_max,))

    singles = set()
    ranges = []
    sections = _acl.split(':')

    action = (sections[0] == 'PERMIT')

    for entry in sections[1].split(','):
        if entry == 'ALL':
            ranges = [(const.ID_MIN, _max)]
            singles = set()
            break
        elif '-' in entry:
            start, end = entry.split('-')
            start, end = int(start), int(end)
            if (const.ID_MIN <= start <= _max) or (const.ID_MIN <= end <= _max):
                ranges.append((start, end))
            else:
                sys.exit('ACL CREATION ERROR, VALUE OUT OF RANGE ({} - {}) IN RANGE-BASED ENTRY: {}'.format(
                    const.ID_MIN, _max, entry))
        else:
            id = int(entry)
            if const.ID_MIN <= id <= _max:
                singles.add(id)
            else:
                sys.exit('ACL CREATION ERROR, VALUE OUT OF RANGE ({} - {}) IN SINGLE ID ENTRY: {}'.format(
                    const.ID_MIN, _max, entry))

    merged = merge_ranges(ranges)
    starts = tuple(r[0] for r in merged)
    ends   = tuple(r[1] for r in merged)
    return (action, frozenset(singles), starts, ends)


def acl_check(_id, _acl):
    id = int_id(_id)
    action, singles, starts, ends = _acl
    if id in singles:
        return action
    i = bisect.bisect_right(starts, id) - 1
    if i >= 0 and id <= ends[i]:
        return action
    return not action


def process_acls(_config):
    # Global subscriber ACL — gates propagation through the bridge
    _config['GLOBAL']['SUB_ACL'] = acl_build(_config['GLOBAL']['SUB_ACL'], const.ID_MAX)
    # Per-system registration ACL — only meaningful on master systems (MASTER_PEER: True)
    for system in _config['SYSTEMS']:
        _config['SYSTEMS'][system]['LOCAL']['REG_ACL'] = acl_build(
            _config['SYSTEMS'][system]['LOCAL']['REG_ACL'], const.PEER_MAX)


def get_address(_host):
    ipv4 = ipv6 = ''
    for item in getaddrinfo(_host, None, 0, 0, IPPROTO_UDP):
        if item[0] == 2:
            ipv4 = item[4][0]
        elif item[0] == 30:
            ipv6 = item[4][0]
    if ipv4:
        return ipv4
    if ipv6:
        return ipv6
    return 'invalid address'


def build_config(_config_file):
    config = configparser.ConfigParser()
    if not config.read(_config_file):
        sys.exit('Configuration file \'' + _config_file + '\' is not a valid configuration file! Exiting...')

    CONFIG = {
        'GLOBAL':  {},
        'REPORTS': {},
        'LOGGER':  {},
        'ALIASES': {},
        'SYSTEMS': {},
    }

    try:
        for section in config.sections():
            if section == 'GLOBAL':
                CONFIG['GLOBAL'].update({
                    'PATH':    config.get(section, 'PATH'),
                    'USE_ACL': config.get(section, 'USE_ACL'),
                    'SUB_ACL': config.get(section, 'SUB_ACL'),
                })

            elif section == 'REPORTS':
                CONFIG['REPORTS'].update({
                    'REPORT_NETWORKS':     config.get(section, 'REPORT_NETWORKS').strip(),
                    'REPORT_RCM':          config.get(section, 'REPORT_RCM').strip().lower() in ('true', 'yes', '1', 'on'),
                    'REPORT_INTERVAL':     config.getint(section, 'REPORT_INTERVAL'),
                    'REPORT_PORT':         config.getint(section, 'REPORT_PORT'),
                    'REPORT_CLIENTS':      [c.strip() for c in config.get(section, 'REPORT_CLIENTS').split(',')],
                    'PRINT_PEERS_INC_MODE':  config.getboolean(section, 'PRINT_PEERS_INC_MODE'),
                    'PRINT_PEERS_INC_FLAGS': config.getboolean(section, 'PRINT_PEERS_INC_FLAGS'),
                })

            elif section == 'LOGGER':
                CONFIG['LOGGER'].update({
                    'LOG_FILE':     config.get(section, 'LOG_FILE'),
                    'LOG_HANDLERS': config.get(section, 'LOG_HANDLERS'),
                    'LOG_LEVEL':    config.get(section, 'LOG_LEVEL'),
                    'LOG_NAME':     config.get(section, 'LOG_NAME'),
                })

            elif section == 'ALIASES':
                CONFIG['ALIASES'].update({
                    'USE_ALIASES':     config.getboolean(section, 'USE_ALIASES', fallback=False),
                    'TRY_DOWNLOAD':    config.getboolean(section, 'TRY_DOWNLOAD'),
                    'PATH':            config.get(section, 'PATH'),
                    'PEER_FILE':       config.get(section, 'PEER_FILE'),
                    'SUBSCRIBER_FILE': config.get(section, 'SUBSCRIBER_FILE'),
                    'TGID_FILE':       config.get(section, 'TGID_FILE'),
                    'LOCAL_FILE':      config.get(section, 'LOCAL_FILE'),
                    'PEER_URL':        config.get(section, 'PEER_URL'),
                    'SUBSCRIBER_URL':  config.get(section, 'SUBSCRIBER_URL'),
                    'STALE_TIME':      config.getint(section, 'STALE_DAYS') * 86400,
                })

            elif config.getboolean(section, 'ENABLED'):
                # TRUNK systems have a minimal config distinct from full IPSC systems.
                if config.get(section, 'SYSTEM_TYPE', fallback='IPSC') == 'TRUNK':
                    peer_ip   = get_address(config.get(section, 'PEER_IP'))
                    peer_port = config.getint(section, 'PEER_PORT')
                    CONFIG['SYSTEMS'][section] = {
                        'SYSTEM_TYPE': 'TRUNK',
                        'LOCAL': {
                            'ENABLED':        True,
                            'SYSTEM_TYPE':    'TRUNK',
                            'RADIO_ID':       bytes.fromhex(format(int(config.get(section, 'RADIO_ID')), '08x')),
                            'IP':             config.get(section, 'IP').strip(),
                            'PORT':           config.getint(section, 'PORT'),
                            'GROUP_HANGTIME': config.getint(section, 'GROUP_HANGTIME'),
                        },
                        'TRUNK': {
                            'PEER_IP':   peer_ip,
                            'PEER_PORT': peer_port,
                            'PEER_SOCK': (peer_ip, peer_port),
                        },
                        'MASTER': {},
                        'PEERS':  {},
                    }
                    continue

                CONFIG['SYSTEMS'][section] = {'LOCAL': {}, 'MASTER': {}, 'PEERS': {}}

                CONFIG['SYSTEMS'][section]['LOCAL'].update({
                    'ENABLED':      True,
                    'PEER_OPER':    config.getboolean(section, 'PEER_OPER'),
                    'IPSC_MODE':    config.get(section, 'IPSC_MODE'),
                    'TS1_LINK':     config.getboolean(section, 'TS1_LINK'),
                    'TS2_LINK':     config.getboolean(section, 'TS2_LINK'),
                    'MODE':         b'',
                    'AUTH_ENABLED': config.getboolean(section, 'AUTH_ENABLED'),
                    'CSBK_CALL':    config.getboolean(section, 'CSBK_CALL'),
                    'RCM':          config.getboolean(section, 'RCM'),
                    'CON_APP':      config.getboolean(section, 'CON_APP'),
                    'XNL_CALL':     config.getboolean(section, 'XNL_CALL'),
                    'XNL_MASTER':   config.getboolean(section, 'XNL_MASTER'),
                    'DATA_CALL':    config.getboolean(section, 'DATA_CALL'),
                    'VOICE_CALL':   config.getboolean(section, 'VOICE_CALL'),
                    'MASTER_PEER':  config.getboolean(section, 'MASTER_PEER'),
                    'FLAGS':        b'',
                    'RADIO_ID':     bytes.fromhex(format(int(config.get(section, 'RADIO_ID')), '08x')),
                    'IP':           config.get(section, 'IP').strip(),
                    'PORT':         config.getint(section, 'PORT'),
                    'ALIVE_TIMER':  config.getint(section, 'ALIVE_TIMER'),
                    'MAX_MISSED':   config.getint(section, 'MAX_MISSED'),
                    'AUTH_KEY':     bytes.fromhex(config.get(section, 'AUTH_KEY').rjust(40, '0')),
                    'GROUP_HANGTIME': config.getint(section, 'GROUP_HANGTIME'),
                    'NUM_PEERS':    0,
                    'REG_ACL':      config.get(section, 'REG_ACL', fallback='PERMIT:ALL'),
                })

                CONFIG['SYSTEMS'][section]['MASTER'].update({
                    'RADIO_ID':     b'\x00\x00\x00\x00',
                    'MODE':         b'\x00',
                    'MODE_DECODE':  '',
                    'FLAGS':        b'\x00\x00\x00\x00',
                    'FLAGS_DECODE': '',
                    'STATUS': {
                        'CONNECTED':               False,
                        'CONNECT_TIME':            0,
                        'PEER_LIST':               False,
                        'KEEP_ALIVES_SENT':        0,
                        'KEEP_ALIVES_MISSED':      0,
                        'KEEP_ALIVES_OUTSTANDING': 0,
                        'KEEP_ALIVES_RECEIVED':    0,
                        'KEEP_ALIVE_RX_TIME':      0,
                    },
                    'IP':   '',
                    'PORT': '',
                })

                if not CONFIG['SYSTEMS'][section]['LOCAL']['MASTER_PEER']:
                    CONFIG['SYSTEMS'][section]['MASTER'].update({
                        'IP':   get_address(config.get(section, 'MASTER_IP')),
                        'PORT': config.getint(section, 'MASTER_PORT'),
                    })

                # Build the MODE byte
                MODE_BYTE = 0
                if CONFIG['SYSTEMS'][section]['LOCAL']['PEER_OPER']:
                    MODE_BYTE |= 1 << 6
                if CONFIG['SYSTEMS'][section]['LOCAL']['IPSC_MODE'] == 'ANALOG':
                    MODE_BYTE |= 1 << 4
                elif CONFIG['SYSTEMS'][section]['LOCAL']['IPSC_MODE'] == 'DIGITAL':
                    MODE_BYTE |= 1 << 5
                if CONFIG['SYSTEMS'][section]['LOCAL']['TS1_LINK']:
                    MODE_BYTE |= 1 << 3
                else:
                    MODE_BYTE |= 1 << 2
                if CONFIG['SYSTEMS'][section]['LOCAL']['TS2_LINK']:
                    MODE_BYTE |= 1 << 1
                else:
                    MODE_BYTE |= 1 << 0
                CONFIG['SYSTEMS'][section]['LOCAL']['MODE'] = bytes([MODE_BYTE])

                # Build the FLAGS field (4 bytes: 0x00, 0x00, FLAG_1, FLAG_2)
                FLAG_1 = FLAG_2 = 0
                if CONFIG['SYSTEMS'][section]['LOCAL']['CSBK_CALL']:
                    FLAG_1 |= 1 << 7
                if CONFIG['SYSTEMS'][section]['LOCAL']['RCM']:
                    FLAG_1 |= 1 << 6
                if CONFIG['SYSTEMS'][section]['LOCAL']['CON_APP']:
                    FLAG_1 |= 1 << 5
                if CONFIG['SYSTEMS'][section]['LOCAL']['XNL_CALL']:
                    FLAG_2 |= 1 << 7
                if CONFIG['SYSTEMS'][section]['LOCAL']['XNL_CALL'] and CONFIG['SYSTEMS'][section]['LOCAL']['XNL_MASTER']:
                    FLAG_2 |= 1 << 6
                elif CONFIG['SYSTEMS'][section]['LOCAL']['XNL_CALL'] and not CONFIG['SYSTEMS'][section]['LOCAL']['XNL_MASTER']:
                    FLAG_2 |= 1 << 5
                if CONFIG['SYSTEMS'][section]['LOCAL']['AUTH_ENABLED']:
                    FLAG_2 |= 1 << 4
                if CONFIG['SYSTEMS'][section]['LOCAL']['DATA_CALL']:
                    FLAG_2 |= 1 << 3
                if CONFIG['SYSTEMS'][section]['LOCAL']['VOICE_CALL']:
                    FLAG_2 |= 1 << 2
                if CONFIG['SYSTEMS'][section]['LOCAL']['MASTER_PEER']:
                    FLAG_2 |= 1 << 0
                CONFIG['SYSTEMS'][section]['LOCAL']['FLAGS'] = b'\x00\x00' + bytes([FLAG_1, FLAG_2])

    except configparser.Error as err:
        print(err)
        sys.exit('Could not parse configuration file, exiting...')

    return CONFIG


if __name__ == '__main__':
    import os
    import argparse
    from pprint import pprint

    os.chdir(os.path.dirname(os.path.realpath(sys.argv[0])))

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', action='store', dest='CONFIG_FILE',
                        help='/full/path/to/dmrlink.cfg')
    cli_args = parser.parse_args()

    if not cli_args.CONFIG_FILE:
        cli_args.CONFIG_FILE = os.path.dirname(os.path.abspath(__file__)) + '/../dmrlink.cfg'

    pprint(build_config(cli_args.CONFIG_FILE))
