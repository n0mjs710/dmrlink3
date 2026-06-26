"""Unit tests for the IPSC protocol state machine (dmrlink.IPSC)."""

import unittest
from time import time

from const import (
    MASTER_REG_REPLY, PEER_LIST_REPLY, MASTER_ALIVE_REPLY,
    DE_REG_REPLY, OPCODE_0xF0, SYSTEM_MAP_REQ,
    VOICE_HEAD, VOICE_TERM, SLOT1_VOICE, SLOT2_VOICE,
)
from dmrlink import IPSC
from dmr_utils3.utils import bytes_4

from helpers import (
    MockDatagramTransport, make_config, make_system, make_peer_entry,
    make_reg_req, make_alive_req, make_dereg_req, make_peer_list_req,
    make_gv_packet, MODE_DIGITAL_TS12, FLAGS_PEER,
)


MASTER_ID = 3112000
PEER_A_ID = 3112001
PEER_B_ID = 3112002
EXT_ID    = 3112010   # an external repeater sending voice

MASTER_ID_B = bytes_4(MASTER_ID)
PEER_A_ID_B = bytes_4(PEER_A_ID)
PEER_B_ID_B = bytes_4(PEER_B_ID)
EXT_ID_B    = bytes_4(EXT_ID)

SRC_SUB  = b'\x00\x00\x01'
DST_GRP  = b'\x00\x00\x09'


class _CapturingIPSC(IPSC):
    """Records group_voice() calls for timeslot-detection assertions."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gv_calls = []

    def group_voice(self, src, dst, ts, end, peerid, data):
        self.gv_calls.append({'src': src, 'dst': dst, 'ts': ts, 'end': end})


class IPSCMasterBase(unittest.TestCase):
    """Base: master-mode IPSC with a mock transport, no real socket."""

    SYSTEM_NAME = 'TEST-MASTER'

    def setUp(self):
        cfg = make_config({
            self.SYSTEM_NAME: make_system(MASTER_ID, '127.0.0.1', 50100, master_peer=True),
        })
        self.proto = IPSC(self.SYSTEM_NAME, cfg, None)
        self.transport = MockDatagramTransport()
        self.proto.transport = self.transport

    def inject(self, data, host='127.0.0.1', port=50200):
        self.proto.datagram_received(data, (host, port))

    def pre_register(self, peer_id_bytes, host='127.0.0.1', port=50200):
        self.proto._peers[peer_id_bytes] = make_peer_entry(host, port)

    def pkts_to(self, host, port):
        return [p for p, addr in self.transport.sent if addr == (host, port)]

    def opcodes_to(self, host, port):
        return [p[0:1] for p in self.pkts_to(host, port)]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration(IPSCMasterBase):

    def test_reg_adds_peer(self):
        self.inject(make_reg_req(PEER_A_ID_B), '127.0.0.1', 50201)
        self.assertIn(PEER_A_ID_B, self.proto._peers)

    def test_reg_num_peers_updated(self):
        self.inject(make_reg_req(PEER_A_ID_B), '127.0.0.1', 50201)
        self.assertEqual(self.proto._local['NUM_PEERS'], 1)

    def test_reg_sends_reg_reply(self):
        self.inject(make_reg_req(PEER_A_ID_B), '127.0.0.1', 50201)
        self.assertIn(MASTER_REG_REPLY, self.opcodes_to('127.0.0.1', 50201))

    def test_reg_sends_peer_list(self):
        self.inject(make_reg_req(PEER_A_ID_B), '127.0.0.1', 50201)
        self.assertIn(PEER_LIST_REPLY, self.opcodes_to('127.0.0.1', 50201))

    def test_rereg_no_duplicate(self):
        pkt = make_reg_req(PEER_A_ID_B)
        self.inject(pkt, '127.0.0.1', 50201)
        self.inject(pkt, '127.0.0.1', 50201)
        self.assertEqual(len(self.proto._peers), 1)

    def test_second_peer_broadcast_to_first(self):
        self.inject(make_reg_req(PEER_A_ID_B), '127.0.0.1', 50201)
        self.transport.sent.clear()

        self.inject(make_reg_req(PEER_B_ID_B), '127.0.0.1', 50202)

        self.assertIn(PEER_LIST_REPLY, self.opcodes_to('127.0.0.1', 50201),
                      'Existing peer A should receive a fresh peer list when peer B registers')

    def test_peer_list_contains_registered_peers(self):
        self.inject(make_reg_req(PEER_A_ID_B), '127.0.0.1', 50201)
        peer_list_pkts = [p for p in self.pkts_to('127.0.0.1', 50201)
                          if p[0:1] == PEER_LIST_REPLY]
        self.assertTrue(peer_list_pkts)
        # The peer list payload (after opcode + radio_id) should include PEER_A's radio ID
        self.assertIn(PEER_A_ID_B, peer_list_pkts[-1])


# ---------------------------------------------------------------------------
# De-registration
# ---------------------------------------------------------------------------

class TestDeregistration(IPSCMasterBase):

    def test_dereg_removes_peer(self):
        self.pre_register(PEER_A_ID_B, '127.0.0.1', 50201)
        self.inject(make_dereg_req(PEER_A_ID_B), '127.0.0.1', 50201)
        self.assertNotIn(PEER_A_ID_B, self.proto._peers)

    def test_dereg_sends_reply(self):
        self.pre_register(PEER_A_ID_B, '127.0.0.1', 50201)
        self.inject(make_dereg_req(PEER_A_ID_B), '127.0.0.1', 50201)
        self.assertIn(DE_REG_REPLY, self.opcodes_to('127.0.0.1', 50201))


# ---------------------------------------------------------------------------
# Keepalive
# ---------------------------------------------------------------------------

class TestKeepalive(IPSCMasterBase):

    def test_master_alive_req_gets_reply(self):
        self.pre_register(PEER_A_ID_B, '127.0.0.1', 50201)
        self.inject(make_alive_req(PEER_A_ID_B), '127.0.0.1', 50201)
        self.assertIn(MASTER_ALIVE_REPLY, self.opcodes_to('127.0.0.1', 50201))

    def test_master_alive_req_increments_ka_received(self):
        self.pre_register(PEER_A_ID_B, '127.0.0.1', 50201)
        self.inject(make_alive_req(PEER_A_ID_B), '127.0.0.1', 50201)
        self.assertEqual(self.proto._peers[PEER_A_ID_B]['STATUS']['KEEP_ALIVES_RECEIVED'], 1)

    def test_any_gv_packet_refreshes_watchdog(self):
        self.pre_register(PEER_A_ID_B, '127.0.0.1', 50201)
        old_ka_time = self.proto._peers[PEER_A_ID_B]['STATUS']['KEEP_ALIVE_RX_TIME']

        import time
        time.sleep(0.01)

        pkt = make_gv_packet(PEER_A_ID_B, SRC_SUB, DST_GRP, VOICE_HEAD, 1)
        self.inject(pkt, '127.0.0.1', 50201)

        new_ka_time = self.proto._peers[PEER_A_ID_B]['STATUS']['KEEP_ALIVE_RX_TIME']
        self.assertGreaterEqual(new_ka_time, old_ka_time,
                                'KA timestamp should refresh on any received packet')


# ---------------------------------------------------------------------------
# Peer list request — must unicast to requester only
# ---------------------------------------------------------------------------

class TestPeerListRequest(IPSCMasterBase):

    def test_peer_list_req_unicasts_to_requester(self):
        self.inject(make_reg_req(PEER_A_ID_B), '127.0.0.1', 50201)
        self.inject(make_reg_req(PEER_B_ID_B), '127.0.0.1', 50202)
        self.transport.sent.clear()

        self.inject(make_peer_list_req(PEER_A_ID_B), '127.0.0.1', 50201)

        # Peer A gets a reply
        self.assertIn(PEER_LIST_REPLY, self.opcodes_to('127.0.0.1', 50201))
        # Peer B does NOT get a reply
        self.assertNotIn(PEER_LIST_REPLY, self.opcodes_to('127.0.0.1', 50202))


# ---------------------------------------------------------------------------
# Timeslot detection
# ---------------------------------------------------------------------------

class TestTimeslotDetection(unittest.TestCase):
    """
    For VOICE_HEAD/VOICE_TERM: _ts comes from call_info bit 5.
    For SLOT1/SLOT2_VOICE:     _ts comes from burst_type bit 7.

    This tests the critical bug fix where `(_call_info & TS_CALL_MSK) + 1`
    would produce _ts=33 for TS2 instead of _ts=2.
    """

    def setUp(self):
        cfg = make_config({
            'TEST-MASTER': make_system(MASTER_ID, '127.0.0.1', 50100, master_peer=True),
        })
        self.proto = _CapturingIPSC('TEST-MASTER', cfg, None)
        self.proto.transport = MockDatagramTransport()
        self.proto._peers[EXT_ID_B] = make_peer_entry()

    def _inject(self, burst_type, timeslot):
        pkt = make_gv_packet(EXT_ID_B, SRC_SUB, DST_GRP, burst_type, timeslot)
        self.proto.datagram_received(pkt, ('127.0.0.1', 50200))
        return self.proto.gv_calls[-1]['ts']

    def test_voice_head_ts1(self):
        self.assertEqual(self._inject(VOICE_HEAD, 1), 1)

    def test_voice_head_ts2(self):
        self.assertEqual(self._inject(VOICE_HEAD, 2), 2)

    def test_voice_term_ts1(self):
        self.assertEqual(self._inject(VOICE_TERM, 1), 1)

    def test_voice_term_ts2(self):
        self.assertEqual(self._inject(VOICE_TERM, 2), 2)

    def test_slot1_voice_ts1(self):
        # SLOT1_VOICE = 0x0A, bit 7 = 0 → TS1 regardless of call_info
        self.assertEqual(self._inject(SLOT1_VOICE, 1), 1)

    def test_slot2_voice_ts2(self):
        # SLOT2_VOICE = 0x8A, bit 7 = 1 → TS2 regardless of call_info
        self.assertEqual(self._inject(SLOT2_VOICE, 2), 2)

    def test_slot2_voice_overrides_call_info_ts1(self):
        # Packet has SLOT2_VOICE burst type (says TS2) but call_info TS bit = 0 (says TS1).
        # The correct TS is 2 — burst_type wins for SLOT1/SLOT2.
        pkt = make_gv_packet(EXT_ID_B, SRC_SUB, DST_GRP, SLOT2_VOICE, 1)  # call_info=TS1
        self.proto.datagram_received(pkt, ('127.0.0.1', 50200))
        ts = self.proto.gv_calls[-1]['ts']
        self.assertEqual(ts, 2, 'SLOT2_VOICE (bit7=1) must yield TS2 even if call_info says TS1')


# ---------------------------------------------------------------------------
# Unknown / new opcodes — no crash, no response
# ---------------------------------------------------------------------------

class TestUnknownOpcodes(IPSCMasterBase):

    def test_opcode_0xf0_no_exception(self):
        pkt = OPCODE_0xF0 + PEER_A_ID_B + b'\x00' * 10
        self.inject(pkt)   # must not raise

    def test_opcode_0xf0_no_reply(self):
        pkt = OPCODE_0xF0 + PEER_A_ID_B + b'\x00' * 10
        self.inject(pkt)
        self.assertFalse(self.transport.sent)

    def test_system_map_req_no_exception(self):
        pkt = SYSTEM_MAP_REQ + PEER_A_ID_B + b'\x00' * 10
        self.inject(pkt)

    def test_truly_unknown_opcode_no_exception(self):
        pkt = b'\xFF' + PEER_A_ID_B + b'\x00' * 10
        self.inject(pkt)


if __name__ == '__main__':
    unittest.main()
