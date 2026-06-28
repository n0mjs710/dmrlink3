"""Unit tests for bridge.bridgeIPSC routing and call-boundary logic."""

import unittest
from time import time, sleep
from unittest.mock import MagicMock

import dmrlink
import bridge
from const import VOICE_HEAD, VOICE_TERM, SLOT1_VOICE, SLOT2_VOICE
from dmr_utils3.utils import bytes_3, bytes_4

from helpers import (
    MockDatagramTransport, make_config, make_system, make_peer_entry,
    make_gv_packet, make_bridge_rules,
)

# Suppress 'allow_sub undefined' NameError — normally set by bridge.build_acl()
bridge.allow_sub = lambda _sub: True

MASTER_ID = 3112000
PEER_ID   = 3112001
EXT_ID    = 3112010

MASTER_ID_B = bytes_4(MASTER_ID)
PEER_ID_B   = bytes_4(PEER_ID)
EXT_ID_B    = bytes_4(EXT_ID)

SRC_SUB   = b'\x00\x00\x01'
TGID_9    = bytes_3(9)
TGID_10   = bytes_3(10)


def _make_two_system_config():
    return make_config({
        'TEST-MASTER': make_system(MASTER_ID, '127.0.0.1', 50100, master_peer=True),
        'TEST-PEER':   make_system(PEER_ID,   '127.0.0.1', 50101, master_peer=False,
                                   master_ip='127.0.0.1', master_port=50100),
    })


class BridgeBase(unittest.TestCase):
    """
    Base class that:
    - Creates two bridgeIPSC instances (TEST-MASTER, TEST-PEER)
    - Mocks send_to_ipsc on both so we can assert forwarding
    - Populates dmrlink.systems so bridge.group_voice can look up the target
    - Restores everything in tearDown
    """

    def setUp(self):
        self.config = _make_two_system_config()
        self.master = bridge.bridgeIPSC('TEST-MASTER', self.config, None)
        self.peer   = bridge.bridgeIPSC('TEST-PEER',   self.config, None)

        self.master.send_to_ipsc = MagicMock()
        self.peer.send_to_ipsc   = MagicMock()

        self._orig_systems   = dict(dmrlink.systems)
        self._orig_bridges   = bridge.BRIDGES
        self._orig_trunks    = bridge.TRUNKS
        self._orig_src_index = bridge.BRIDGE_SRC_INDEX
        self._orig_by_system = bridge.BRIDGE_BY_SYSTEM

        dmrlink.systems.clear()
        dmrlink.systems.update({'TEST-MASTER': self.master, 'TEST-PEER': self.peer})

        bridge.TRUNKS = []
        self._set_bridges(make_bridge_rules('TEST-MASTER', 'TEST-PEER', TGID_9))

    def tearDown(self):
        dmrlink.systems.clear()
        dmrlink.systems.update(self._orig_systems)
        bridge.BRIDGES          = self._orig_bridges
        bridge.TRUNKS           = self._orig_trunks
        bridge.BRIDGE_SRC_INDEX = self._orig_src_index
        bridge.BRIDGE_BY_SYSTEM = self._orig_by_system

    def _set_bridges(self, bridges):
        """Replace bridge.BRIDGES and rebuild the routing indexes in one step."""
        bridge.BRIDGES = bridges
        bridge.BRIDGE_SRC_INDEX, bridge.BRIDGE_BY_SYSTEM = bridge.index_bridges(bridges)

    def _call_gv(self, system, src, dst, burst_type, ts):
        pkt = make_gv_packet(EXT_ID_B, src, dst, burst_type, ts)
        system.group_voice(src, dst, ts, False, EXT_ID_B, pkt)
        return pkt


# ---------------------------------------------------------------------------
# Basic forwarding
# ---------------------------------------------------------------------------

class TestForwarding(BridgeBase):

    def test_voice_head_forwarded(self):
        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_HEAD, 1)
        self.peer.send_to_ipsc.assert_called_once()

    def test_full_call_forwarded(self):
        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_HEAD, 1)
        for _ in range(3):
            self._call_gv(self.master, SRC_SUB, TGID_9, SLOT1_VOICE, 1)
        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_TERM, 1)
        self.assertEqual(self.peer.send_to_ipsc.call_count, 5)

    def test_bridge_inactive_not_forwarded(self):
        self._set_bridges(make_bridge_rules('TEST-MASTER', 'TEST-PEER', TGID_9, active=False))
        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_HEAD, 1)
        self.peer.send_to_ipsc.assert_not_called()

    def test_wrong_tgid_not_forwarded(self):
        wrong_tgid = bytes_3(8)
        self._call_gv(self.master, SRC_SUB, wrong_tgid, VOICE_HEAD, 1)
        self.peer.send_to_ipsc.assert_not_called()

    def test_wrong_ts_not_forwarded(self):
        # Bridge is on TS1; voice arrives on TS2 → should not forward
        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_HEAD, 2)
        self.peer.send_to_ipsc.assert_not_called()

    def test_source_system_not_forwarded_to_self(self):
        # The TEST-MASTER entry in BRIDGES should not forward back to itself
        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_HEAD, 1)
        self.master.send_to_ipsc.assert_not_called()


# ---------------------------------------------------------------------------
# Packet rewriting
# ---------------------------------------------------------------------------

class TestPacketRewrite(BridgeBase):

    def _forwarded(self, src, dst, burst_type, ts):
        self._call_gv(self.master, src, dst, burst_type, ts)
        return self.peer.send_to_ipsc.call_args[0][0]

    def test_peer_id_rewritten(self):
        fwd = self._forwarded(SRC_SUB, TGID_9, VOICE_HEAD, 1)
        target_radio_id = self.config['SYSTEMS']['TEST-PEER']['LOCAL']['RADIO_ID']
        self.assertEqual(fwd[1:5], target_radio_id,
                         'Forwarded packet peer_id should be the target system radio ID')

    def test_ipsc_dst_tgid_rewritten(self):
        """When src_tgid ≠ tgt_tgid, the IPSC dst_group (bytes 9-11) gets rewritten."""
        self._set_bridges(make_bridge_rules('TEST-MASTER', 'TEST-PEER', TGID_9, TGID_10))
        fwd = self._forwarded(SRC_SUB, TGID_9, VOICE_HEAD, 1)
        self.assertEqual(fwd[9:12], TGID_10,
                         'Forwarded IPSC dst_group should be target TGID (10)')
        self.assertNotEqual(fwd[9:12], TGID_9)

    def test_dmr_lc_dst_tgid_rewritten(self):
        """The DMR LC dst field (bytes 31-33) should also be rewritten to target TGID."""
        self._set_bridges(make_bridge_rules('TEST-MASTER', 'TEST-PEER', TGID_9, TGID_10))
        fwd = self._forwarded(SRC_SUB, TGID_9, VOICE_HEAD, 1)
        self.assertEqual(fwd[31:34], TGID_10,
                         'Forwarded DMR LC dst should be target TGID (10)')

    def test_ts_call_info_bit_rewritten(self):
        """When target TS=1, call_info bit 5 should be cleared (TS1)."""
        fwd = self._forwarded(SRC_SUB, TGID_9, VOICE_HEAD, 1)
        call_info = fwd[17]
        self.assertFalse(call_info & 0x20, 'call_info TS bit should be clear for target TS1')

    def test_ts_call_info_bit_set_for_ts2_target(self):
        """When target TS=2, call_info bit 5 should be set (TS2)."""
        self._set_bridges(make_bridge_rules('TEST-MASTER', 'TEST-PEER',
                                            src_tgid=TGID_9, tgt_tgid=TGID_9, ts=1, active=True))
        # Override to make target TS=2 (mutates the member in-place so the index reflects it)
        bridge.BRIDGES['TESTBRIDGE'][1]['TS'] = 2
        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_HEAD, 1)
        fwd = self.peer.send_to_ipsc.call_args[0][0]
        call_info = fwd[17]
        self.assertTrue(call_info & 0x20, 'call_info TS bit should be set for target TS2')

    def test_slot2_burst_rewritten_to_slot1_for_ts1_target(self):
        """Source TS2 SLOT2_VOICE bridged to a TS1 target: burst type rewritten to SLOT1_VOICE."""
        self._set_bridges({
            'TESTBRIDGE': [
                {'SYSTEM': 'TEST-MASTER', 'TS': 2, 'TGID': TGID_9, 'ACTIVE': True,
                 'TO_TYPE': 'NONE', 'TIMEOUT': 0, 'TIMER': 0, 'ON': [], 'OFF': [], 'RESET': []},
                {'SYSTEM': 'TEST-PEER',   'TS': 1, 'TGID': TGID_9, 'ACTIVE': True,
                 'TO_TYPE': 'NONE', 'TIMEOUT': 0, 'TIMER': 0, 'ON': [], 'OFF': [], 'RESET': []},
            ]
        })
        pkt = make_gv_packet(EXT_ID_B, SRC_SUB, TGID_9, SLOT2_VOICE, 2)
        self.master.group_voice(SRC_SUB, TGID_9, 2, False, EXT_ID_B, pkt)
        fwd = self.peer.send_to_ipsc.call_args[0][0]
        self.assertEqual(fwd[30], SLOT1_VOICE,
                         'SLOT2_VOICE should be rewritten to SLOT1_VOICE when target is TS1')

    def test_slot1_burst_rewritten_to_slot2_for_ts2_target(self):
        """Source TS1 SLOT1_VOICE bridged to a TS2 target: burst type rewritten to SLOT2_VOICE."""
        self._set_bridges({
            'TESTBRIDGE': [
                {'SYSTEM': 'TEST-MASTER', 'TS': 1, 'TGID': TGID_9, 'ACTIVE': True,
                 'TO_TYPE': 'NONE', 'TIMEOUT': 0, 'TIMER': 0, 'ON': [], 'OFF': [], 'RESET': []},
                {'SYSTEM': 'TEST-PEER',   'TS': 2, 'TGID': TGID_9, 'ACTIVE': True,
                 'TO_TYPE': 'NONE', 'TIMEOUT': 0, 'TIMER': 0, 'ON': [], 'OFF': [], 'RESET': []},
            ]
        })
        pkt = make_gv_packet(EXT_ID_B, SRC_SUB, TGID_9, SLOT1_VOICE, 1)
        self.master.group_voice(SRC_SUB, TGID_9, 1, False, EXT_ID_B, pkt)
        fwd = self.peer.send_to_ipsc.call_args[0][0]
        self.assertEqual(fwd[30], SLOT2_VOICE,
                         'SLOT1_VOICE should be rewritten to SLOT2_VOICE when target is TS2')

    def test_voice_head_burst_type_not_rewritten(self):
        """VOICE_HEAD burst type should not be changed by the TS rewrite (only SLOT1/2 are)."""
        fwd = self._forwarded(SRC_SUB, TGID_9, VOICE_HEAD, 1)
        self.assertEqual(fwd[30], VOICE_HEAD)


# ---------------------------------------------------------------------------
# Call boundary tracking (per-TS call_start dict)
# ---------------------------------------------------------------------------

class TestCallBoundary(unittest.TestCase):
    """
    IPSC (base class) tracks call start/end via VOICE_HEAD and VOICE_TERM burst types,
    NOT via seq_id (which Talker Alias firmware churns every superframe).
    rx_start[ts] is set on VOICE_HEAD and cleared on VOICE_TERM.
    bridgeIPSC calls super().group_voice() so the base-class tracking fires.
    """

    def setUp(self):
        config = _make_two_system_config()
        self._orig_systems   = dict(dmrlink.systems)
        self._orig_bridges   = bridge.BRIDGES
        self._orig_trunks    = bridge.TRUNKS
        self._orig_src_index = bridge.BRIDGE_SRC_INDEX
        self._orig_by_system = bridge.BRIDGE_BY_SYSTEM

        # No BRIDGES needed — we are only checking rx_start tracking
        bridge.BRIDGES          = {}
        bridge.TRUNKS           = []
        bridge.BRIDGE_SRC_INDEX = {}
        bridge.BRIDGE_BY_SYSTEM = {}

        self.proto = bridge.bridgeIPSC('TEST-MASTER', config, None)
        self.proto.transport = MockDatagramTransport()

        dmrlink.systems.clear()

    def tearDown(self):
        dmrlink.systems.clear()
        dmrlink.systems.update(self._orig_systems)
        bridge.BRIDGES          = self._orig_bridges
        bridge.TRUNKS           = self._orig_trunks
        bridge.BRIDGE_SRC_INDEX = self._orig_src_index
        bridge.BRIDGE_BY_SYSTEM = self._orig_by_system

    def _gv(self, burst_type, ts=1, src=SRC_SUB, dst=TGID_9):
        pkt = make_gv_packet(EXT_ID_B, src, dst, burst_type, ts)
        self.proto.group_voice(src, dst, ts, False, EXT_ID_B, pkt)

    def test_voice_head_sets_call_start(self):
        self.assertEqual(self.proto.rx_start[1], 0)
        self._gv(VOICE_HEAD, 1)
        self.assertGreater(self.proto.rx_start[1], 0)

    def test_voice_term_clears_call_start(self):
        self._gv(VOICE_HEAD, 1)
        self._gv(VOICE_TERM, 1)
        self.assertEqual(self.proto.rx_start[1], 0)

    def test_per_ts_independent(self):
        self._gv(VOICE_HEAD, ts=1)
        self.assertEqual(self.proto.rx_start[2], 0, 'TS2 rx_start should remain 0')

        self._gv(VOICE_TERM, ts=1)
        self.assertEqual(self.proto.rx_start[1], 0)
        self.assertEqual(self.proto.rx_start[2], 0)

    def test_ta_seq_id_churn_no_duplicate_call_start(self):
        """
        Talker Alias firmware sends a new IPSC seq_id every superframe but does NOT
        send duplicate VOICE_HEAD bursts. However, if two consecutive VOICE_HEAD arrive
        (e.g., due to network quirks) within TS_CLEAR_TIME, rx_start must NOT be reset.
        """
        self._gv(VOICE_HEAD, 1)
        first_start = self.proto.rx_start[1]

        sleep(0.01)   # 10 ms — well under TS_CLEAR_TIME (0.2 s)
        self._gv(VOICE_HEAD, 1)   # second HEAD within clear-time
        second_start = self.proto.rx_start[1]

        self.assertEqual(first_start, second_start,
                         'rx_start must not reset for VOICE_HEAD within TS_CLEAR_TIME')

    def test_voice_head_restarts_after_clear_time(self):
        """A VOICE_HEAD arriving more than TS_CLEAR_TIME after rx_start replaces it."""
        orig_clear = dmrlink.TS_CLEAR_TIME
        dmrlink.TS_CLEAR_TIME = 0.0   # zero clear-time for this test
        try:
            self._gv(VOICE_HEAD, 1)
            first_start = self.proto.rx_start[1]
            sleep(0.01)
            self._gv(VOICE_HEAD, 1)
            second_start = self.proto.rx_start[1]
            self.assertGreater(second_start, first_start,
                               'rx_start should update when TS_CLEAR_TIME has elapsed')
        finally:
            dmrlink.TS_CLEAR_TIME = orig_clear

    def test_unmatched_voice_term_no_crash(self):
        """A VOICE_TERM with no preceding VOICE_HEAD should not crash."""
        self._gv(VOICE_TERM, 1)   # must not raise
        self.assertEqual(self.proto.rx_start[1], 0)

    def test_ts2_call_tracking_independent_of_ts1(self):
        self._gv(VOICE_HEAD, ts=2)
        self.assertGreater(self.proto.rx_start[2], 0)
        self.assertEqual(self.proto.rx_start[1], 0)


# ---------------------------------------------------------------------------
# Contention handling
# ---------------------------------------------------------------------------

class TestContention(BridgeBase):
    """
    Calls on a target TS that is already busy (within GROUP_HANGTIME)
    must be blocked even when the bridge is active.
    """

    def test_rx_hangtime_blocks_different_tgid(self):
        """If the target just received a *different* TGID, a new call should be blocked."""
        different_tgid = bytes_3(8)
        self.peer.STATUS[1]['RX_TGID'] = different_tgid
        self.peer.STATUS[1]['RX_TIME'] = time()   # just now

        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_HEAD, 1)
        self.peer.send_to_ipsc.assert_not_called()

    def test_rx_hangtime_expired_allows_call(self):
        """After GROUP_HANGTIME expires, the same scenario should forward."""
        different_tgid = bytes_3(8)
        self.peer.STATUS[1]['RX_TGID'] = different_tgid
        self.peer.STATUS[1]['RX_TIME'] = time() - 10   # 10 s ago (hangtime=5 s)

        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_HEAD, 1)
        self.peer.send_to_ipsc.assert_called_once()

    def test_tx_hangtime_blocks_different_tgid(self):
        """If the target recently transmitted on a *different* TGID, new call is blocked."""
        different_tgid = bytes_3(8)
        self.peer.STATUS[1]['TX_TGID'] = different_tgid
        self.peer.STATUS[1]['TX_TIME'] = time()

        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_HEAD, 1)
        self.peer.send_to_ipsc.assert_not_called()

    def test_trunk_bypasses_contention(self):
        """Systems in TRUNKS skip all contention checks."""
        different_tgid = bytes_3(8)
        self.peer.STATUS[1]['RX_TGID'] = different_tgid
        self.peer.STATUS[1]['RX_TIME'] = time()

        bridge.TRUNKS = ['TEST-PEER']
        self._call_gv(self.master, SRC_SUB, TGID_9, VOICE_HEAD, 1)
        self.peer.send_to_ipsc.assert_called_once()


if __name__ == '__main__':
    unittest.main()
