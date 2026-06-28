"""
Tests for Trunk v2 STATUS write semantics.

A TRUNK system can carry an arbitrary number of simultaneous streams, so its
STATUS dict cannot be TS-keyed without lying about capacity.  More importantly,
the TS-keyed values are never consulted for any decision: contention checks are
bypassed for trunk targets (TRUNKS list), and no other code reads trunk STATUS.

Expected behaviour after the fix:
  - TRUNK.STATUS is {} (empty dict, no TS keys)
  - Routing FROM an IPSC source TO a trunk target does not write TX state
  - Routing FROM a trunk source does not write RX state to the trunk's STATUS
  - Routing FROM a trunk source TO an IPSC target still writes TX state (IPSC
    targets do use STATUS for contention), and the packet is still forwarded
"""

import unittest
from unittest.mock import MagicMock

import dmrlink
import bridge
from bridge import bridgeIPSC, bridgeTRUNK
from const import VOICE_HEAD, VOICE_TERM, SLOT1_VOICE
from dmr_utils3.utils import bytes_3, bytes_4

from helpers import (
    MockDatagramTransport, make_config, make_system, make_trunk_system,
    make_gv_packet, make_bridge_rules,
)

MASTER_ID  = 3112000
TRUNK_RID  = 3129100
EXT_ID     = 3112010
EXT_ID_B   = bytes_4(EXT_ID)
SRC_SUB    = b'\x00\x00\x01'
TGID_9     = bytes_3(9)


def _make_config():
    """Two-system config: one IPSC master and one trunk endpoint."""
    return make_config({
        'TEST-MASTER': make_system(MASTER_ID, '127.0.0.1', 50100, master_peer=True),
        'TEST-TRUNK':  make_trunk_system(TRUNK_RID),
    })


class BridgeTrunkBase(unittest.TestCase):
    """
    Base: IPSC master + TRUNK endpoint, bridge MASTER→TRUNK on TGID 9.
    Trunk is in TRUNKS so contention is bypassed.
    BRIDGE_SRC_INDEX / BRIDGE_BY_SYSTEM are built from BRIDGES so the
    forwarding loop actually executes.
    """

    def setUp(self):
        self.config = _make_config()
        self.master = bridgeIPSC('TEST-MASTER', self.config, None)
        self.trunk  = bridgeTRUNK('TEST-TRUNK',  self.config, None)

        self.master.send_to_ipsc = MagicMock()
        self.trunk.transport     = MockDatagramTransport()

        self._orig_systems   = dict(dmrlink.systems)
        self._orig_bridges   = bridge.BRIDGES
        self._orig_trunks    = bridge.TRUNKS
        self._orig_src_index = bridge.BRIDGE_SRC_INDEX
        self._orig_by_system = bridge.BRIDGE_BY_SYSTEM

        dmrlink.systems.clear()
        dmrlink.systems.update({'TEST-MASTER': self.master, 'TEST-TRUNK': self.trunk})

        bridge.BRIDGES = make_bridge_rules('TEST-MASTER', 'TEST-TRUNK', TGID_9)
        bridge.TRUNKS  = ['TEST-TRUNK']
        bridge.BRIDGE_SRC_INDEX, bridge.BRIDGE_BY_SYSTEM = bridge.index_bridges(bridge.BRIDGES)

    def tearDown(self):
        dmrlink.systems.clear()
        dmrlink.systems.update(self._orig_systems)
        bridge.BRIDGES         = self._orig_bridges
        bridge.TRUNKS          = self._orig_trunks
        bridge.BRIDGE_SRC_INDEX = self._orig_src_index
        bridge.BRIDGE_BY_SYSTEM = self._orig_by_system

    def _gv(self, system, burst_type, ts=1, src=SRC_SUB, dst=TGID_9):
        pkt = make_gv_packet(EXT_ID_B, src, dst, burst_type, ts)
        system.group_voice(src, dst, ts, False, EXT_ID_B, pkt)

    def _reverse_bridge(self):
        """Switch the bridge so trunk is the source and master is the target."""
        bridge.BRIDGES = make_bridge_rules('TEST-TRUNK', 'TEST-MASTER', TGID_9)
        bridge.BRIDGE_SRC_INDEX, bridge.BRIDGE_BY_SYSTEM = bridge.index_bridges(bridge.BRIDGES)


# ---------------------------------------------------------------------------
# STATUS structure
# ---------------------------------------------------------------------------

class TestTrunkStatusStructure(BridgeTrunkBase):
    """TRUNK.STATUS must be an empty dict with no TS keys."""

    def test_status_is_empty_dict(self):
        self.assertEqual(self.trunk.STATUS, {})

    def test_status_has_no_slot1_key(self):
        self.assertNotIn(1, self.trunk.STATUS)

    def test_status_has_no_slot2_key(self):
        self.assertNotIn(2, self.trunk.STATUS)


# ---------------------------------------------------------------------------
# IPSC source → trunk target: TX state must not be written
# ---------------------------------------------------------------------------

class TestIpscToTrunkTxState(BridgeTrunkBase):
    """
    When an IPSC source routes to a trunk target, TX state must not be
    written to the trunk's STATUS dict (it has no TS keys and the values
    would never be read for any contention decision anyway).
    """

    def test_tx_state_not_written_on_voice_head(self):
        self._gv(self.master, VOICE_HEAD)
        self.assertEqual(self.trunk.STATUS, {},
                         'TX state must not be written to trunk STATUS on VOICE_HEAD')

    def test_tx_state_not_written_on_interior_burst(self):
        self._gv(self.master, VOICE_HEAD)
        self._gv(self.master, SLOT1_VOICE)
        self.assertEqual(self.trunk.STATUS, {},
                         'TX state must not be written to trunk STATUS on interior burst')

    def test_tx_state_not_written_on_voice_term(self):
        self._gv(self.master, VOICE_HEAD)
        self._gv(self.master, VOICE_TERM)
        self.assertEqual(self.trunk.STATUS, {},
                         'TX state must not be written to trunk STATUS on VOICE_TERM')

    def test_full_call_to_trunk_no_crash(self):
        """Full call routed to trunk must not raise KeyError at any point."""
        try:
            self._gv(self.master, VOICE_HEAD)
            for _ in range(3):
                self._gv(self.master, SLOT1_VOICE)
            self._gv(self.master, VOICE_TERM)
        except KeyError as exc:
            self.fail(f'Routing to trunk raised KeyError: {exc}')

    def test_packet_is_still_forwarded_to_trunk(self):
        """TX state skip must not stop the packet being sent to the trunk peer."""
        self._gv(self.master, VOICE_HEAD)
        self.assertTrue(self.trunk.transport.sent,
                        'VOICE_HEAD must still be forwarded to trunk peer')


# ---------------------------------------------------------------------------
# Trunk source → IPSC target: TX state MUST still be written
# ---------------------------------------------------------------------------

class TestTrunkToIpscTxState(BridgeTrunkBase):
    """
    When a trunk source routes to an IPSC target, TX state MUST be written
    to the IPSC target's STATUS (IPSC targets use it for contention checks).
    """

    def setUp(self):
        super().setUp()
        self._reverse_bridge()

    def test_tx_tgid_written_to_ipsc_target(self):
        self._gv(self.trunk, VOICE_HEAD)
        self.assertEqual(self.master.STATUS[1]['TX_TGID'], TGID_9,
                         'TX_TGID must be written to IPSC target STATUS')

    def test_tx_time_written_to_ipsc_target(self):
        self._gv(self.trunk, VOICE_HEAD)
        self.assertGreater(self.master.STATUS[1]['TX_TIME'], 0,
                           'TX_TIME must be written to IPSC target STATUS')

    def test_packet_forwarded_to_ipsc_target(self):
        self._gv(self.trunk, VOICE_HEAD)
        self.master.send_to_ipsc.assert_called_once()


# ---------------------------------------------------------------------------
# Trunk source RX state: must not be written back to trunk STATUS
# ---------------------------------------------------------------------------

class TestTrunkSourceRxState(BridgeTrunkBase):
    """
    When trunk is the source, the routing core must not write RX state back
    to the trunk's own STATUS dict (it has no TS keys, and the values would
    never be read for contention since trunk targets bypass the check).
    """

    def setUp(self):
        super().setUp()
        self._reverse_bridge()

    def test_rx_state_not_written_to_trunk_source(self):
        self._gv(self.trunk, VOICE_HEAD)
        self.assertEqual(self.trunk.STATUS, {},
                         'RX state must not be written to trunk source STATUS')

    def test_trunk_source_full_call_no_crash(self):
        """Full call from trunk source must not raise KeyError."""
        try:
            self._gv(self.trunk, VOICE_HEAD)
            for _ in range(3):
                self._gv(self.trunk, SLOT1_VOICE)
            self._gv(self.trunk, VOICE_TERM)
        except KeyError as exc:
            self.fail(f'Trunk source group_voice raised KeyError: {exc}')


if __name__ == '__main__':
    unittest.main()
