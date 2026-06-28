"""Shared test utilities for dmrlink3 unit tests."""

import struct
from time import time as _time

from const import (
    GROUP_VOICE, VOICE_HEAD, VOICE_TERM, SLOT1_VOICE, SLOT2_VOICE,
    GV_CALL_INFO_OFF, GV_BURST_TYPE_OFF,
    IPSC_VER, TS_CALL_MSK,
    MASTER_REG_REQ, MASTER_ALIVE_REQ, DE_REG_REQ, PEER_LIST_REQ,
)
from dmr_utils3.utils import bytes_2, bytes_3, bytes_4


# ---------------------------------------------------------------------------
# Mode / flags constants
# ---------------------------------------------------------------------------

# Operational, digital, TS1+TS2 linked — matches config.py with PEER_OPER=True,
# IPSC_MODE=DIGITAL, TS1_LINK=True, TS2_LINK=True.
# Bit layout: 01 (oper) | 10 (digital) | 10 (TS1 on) | 10 (TS2 on) = 0x6A
MODE_DIGITAL_TS12 = bytes([0b01101010])

# FLAGS bytes 3 and 4 for CON_APP=True, DATA_CALL=True, VOICE_CALL=True
# byte3 = 0x20 (CON_APP bit), byte4 = 0x0D (DATA|VOICE|MASTER) or 0x0C (DATA|VOICE)
FLAGS_MASTER = b'\x00\x00\x20\x0D'   # …MASTER_PEER=True
FLAGS_PEER   = b'\x00\x00\x20\x0C'   # …MASTER_PEER=False

TS_FLAGS_MASTER = MODE_DIGITAL_TS12 + FLAGS_MASTER   # 5 bytes
TS_FLAGS_PEER   = MODE_DIGITAL_TS12 + FLAGS_PEER     # 5 bytes


# ---------------------------------------------------------------------------
# Config factories
# ---------------------------------------------------------------------------

def make_system(radio_id_int, ip, port, master_peer=True,
                master_ip='127.0.0.1', master_port=50100):
    """Return a SYSTEMS entry identical in structure to what config.py produces."""
    radio_id = bytes_4(radio_id_int)
    flags    = FLAGS_MASTER if master_peer else FLAGS_PEER
    return {
        'LOCAL': {
            'ENABLED':      True,
            'PEER_OPER':    True,
            'IPSC_MODE':    'DIGITAL',
            'TS1_LINK':     True,
            'TS2_LINK':     True,
            'MODE':         MODE_DIGITAL_TS12,
            'FLAGS':        flags,
            'AUTH_ENABLED': False,
            'CSBK_CALL':    False,
            'RCM':          False,
            'CON_APP':      True,
            'XNL_CALL':     False,
            'XNL_MASTER':   False,
            'DATA_CALL':    True,
            'VOICE_CALL':   True,
            'MASTER_PEER':  master_peer,
            'RADIO_ID':     radio_id,
            'IP':           ip,
            'PORT':         port,
            'ALIVE_TIMER':  5,
            'MAX_MISSED':   5,
            'AUTH_KEY':     b'\x00' * 20,
            'GROUP_HANGTIME': 5,
            'NUM_PEERS':    0,
            # PERMIT:ALL — mirrors what process_acls() produces at runtime.
            # acl_build('PERMIT:ALL', PEER_MAX) → (True, frozenset(), (1,), (4294967295,))
            'REG_ACL':      (True, frozenset(), (1,), (4294967295,)),
        },
        'MASTER': {
            'RADIO_ID':     b'\x00\x00\x00\x00',
            'MODE':         b'\x00',
            'MODE_DECODE':  '',
            'FLAGS':        b'\x00\x00\x00\x00',
            'FLAGS_DECODE': '',
            'IP':           master_ip if not master_peer else '',
            'PORT':         master_port if not master_peer else '',
            'STATUS': {
                'CONNECTED':               False,
                'PEER_LIST':               False,
                'KEEP_ALIVES_SENT':        0,
                'KEEP_ALIVES_MISSED':      0,
                'KEEP_ALIVES_OUTSTANDING': 0,
                'KEEP_ALIVES_RECEIVED':    0,
                'KEEP_ALIVE_RX_TIME':      0,
            },
        },
        'PEERS': {},
    }


def make_config(systems_dict=None):
    """Return a minimal CONFIG dict suitable for IPSC / bridgeIPSC construction."""
    return {
        'GLOBAL': {
            'PATH':    '/tmp',
            # PERMIT:ALL — mirrors what process_acls() produces at runtime.
            # acl_build('PERMIT:ALL', ID_MAX) → (True, frozenset(), (1,), (16776415,))
            'SUB_ACL': (True, frozenset(), (1,), (16776415,)),
        },
        'REPORTS': {
            'REPORT_NETWORKS':       'NONE',
            'REPORT_RCM':            False,
            'REPORT_INTERVAL':       60,
            'REPORT_PORT':           19999,
            'REPORT_CLIENTS':        ['127.0.0.1'],
            'PRINT_PEERS_INC_MODE':  False,
            'PRINT_PEERS_INC_FLAGS': False,
        },
        'LOGGER': {
            'LOG_FILE':     '/dev/null',
            'LOG_HANDLERS': 'null',
            'LOG_LEVEL':    'ERROR',
            'LOG_NAME':     'test',
        },
        'ALIASES': {
            'TRY_DOWNLOAD':     False,
            'LOCAL_FILE':       False,
            'PATH':             '/tmp',
            'PEER_FILE':        'peer_ids.json',
            'SUBSCRIBER_FILE':  'subscriber_ids.json',
            'TGID_FILE':        'talkgroup_ids.json',
            'PEER_URL':         '',
            'SUBSCRIBER_URL':   '',
            'STALE_TIME':       0,
        },
        'SYSTEMS': systems_dict or {},
    }


# ---------------------------------------------------------------------------
# Mock transport
# ---------------------------------------------------------------------------

class MockDatagramTransport:
    """Captures sendto() calls for assertion in tests."""
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr=None):
        self.sent.append((bytes(data), addr))

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Peer state helper
# ---------------------------------------------------------------------------

def make_peer_entry(host='127.0.0.1', port=50200):
    """Return a pre-built peer STATUS dict matching what master_reg_req creates."""
    return {
        'IP':           host,
        'PORT':         port,
        'MODE':         MODE_DIGITAL_TS12,
        'MODE_DECODE':  {},
        'FLAGS':        FLAGS_PEER,
        'FLAGS_DECODE': {},
        'STATUS': {
            'CONNECTED':               True,
            'KEEP_ALIVES_SENT':        0,
            'KEEP_ALIVES_MISSED':      0,
            'KEEP_ALIVES_OUTSTANDING': 0,
            'KEEP_ALIVES_RECEIVED':    0,
            'KEEP_ALIVE_RX_TIME':      int(_time()),
        },
    }


# ---------------------------------------------------------------------------
# Packet builders
# ---------------------------------------------------------------------------

def make_reg_req(peer_id_bytes, ts_flags=None):
    """MASTER_REG_REQ packet sent by a joining peer."""
    return MASTER_REG_REQ + peer_id_bytes + (ts_flags or TS_FLAGS_PEER) + IPSC_VER


def make_alive_req(peer_id_bytes, ts_flags=None):
    """MASTER_ALIVE_REQ (keepalive) sent by a peer."""
    return MASTER_ALIVE_REQ + peer_id_bytes + (ts_flags or TS_FLAGS_PEER) + IPSC_VER


def make_dereg_req(peer_id_bytes):
    """DE_REG_REQ sent by a leaving peer."""
    return DE_REG_REQ + peer_id_bytes


def make_peer_list_req(peer_id_bytes):
    """PEER_LIST_REQ sent by a peer."""
    return PEER_LIST_REQ + peer_id_bytes


def make_gv_packet(peer_id, src_sub, dst_group, burst_type, timeslot, seq=0):
    """
    Build a GROUP_VOICE packet suitable for datagram_received().

    Packet layout (65 bytes total):
        [0]     GROUP_VOICE opcode
        [1:5]   peer_id
        [5]     seq_id (informational only — Talker Alias firmware churns this)
        [6:9]   src_sub
        [9:12]  dst_group
        [12]    call_type
        [13:17] call_ctrl
        [17]    call_info  — bit 5 = TS (for VOICE_HEAD/VOICE_TERM)
        [18:30] RTP header (12 bytes)
        [30]    burst_type — bit 7 = TS (for SLOT1/SLOT2_VOICE)
        [31:34] dst_group  — start of DMR payload (LC dst field for TGID rewrite)
        [34:37] src_sub    — DMR LC src field
        [37:65] zeros
    """
    call_info = TS_CALL_MSK if timeslot == 2 else 0

    return (
        GROUP_VOICE
        + peer_id                    # bytes 1-4
        + bytes([seq])               # byte  5  (seq_id)
        + src_sub                    # bytes 6-8
        + dst_group                  # bytes 9-11
        + b'\x00'                    # byte  12 (call_type)
        + b'\x00\x00\x00\x00'       # bytes 13-16 (call_ctrl)
        + bytes([call_info])         # byte  17 (call_info)
        + b'\x00' * 12              # bytes 18-29 (RTP header)
        + bytes([burst_type])        # byte  30 (burst_type)
        + dst_group                  # bytes 31-33 (DMR LC dst — for TGID rewrite)
        + src_sub                    # bytes 34-36 (DMR LC src)
        + b'\x00' * 28             # bytes 37-64 (rest of DMR payload)
    )


def make_trunk_system(radio_id_int=3129100, ip='127.0.0.1', port=50200,
                      peer_ip='10.0.0.1', peer_port=50200):
    """Return a SYSTEMS entry for a TRUNK system (parallel to make_system).

    Matches the structure produced by config.py when SYSTEM_TYPE = TRUNK.
    """
    return {
        'SYSTEM_TYPE': 'TRUNK',
        'LOCAL': {
            'ENABLED':        True,
            'SYSTEM_TYPE':    'TRUNK',
            'RADIO_ID':       bytes_4(radio_id_int),
            'IP':             ip,
            'PORT':           port,
            'GROUP_HANGTIME': 5,
        },
        'TRUNK': {
            'PEER_IP':   peer_ip,
            'PEER_PORT': peer_port,
            'PEER_SOCK': (peer_ip, peer_port),
        },
        'MASTER': {},
        'PEERS':  {},
    }


def make_bridge_rules(src_system, tgt_system, src_tgid, tgt_tgid=None, ts=1, active=True):
    """Return a BRIDGES dict with one bridge connecting src_system to tgt_system."""
    if tgt_tgid is None:
        tgt_tgid = src_tgid
    src_tgid_b = bytes_3(src_tgid) if isinstance(src_tgid, int) else src_tgid
    tgt_tgid_b = bytes_3(tgt_tgid) if isinstance(tgt_tgid, int) else tgt_tgid
    return {
        'TESTBRIDGE': [
            {
                'SYSTEM': src_system, 'TS': ts, 'TGID': src_tgid_b, 'ACTIVE': active,
                'TO_TYPE': 'NONE', 'TIMEOUT': 0, 'TIMER': 0,
                'ON': [], 'OFF': [], 'RESET': [],
            },
            {
                'SYSTEM': tgt_system, 'TS': ts, 'TGID': tgt_tgid_b, 'ACTIVE': active,
                'TO_TYPE': 'NONE', 'TIMEOUT': 0, 'TIMER': 0,
                'ON': [], 'OFF': [], 'RESET': [],
            },
        ]
    }
