#!/usr/bin/env python3
"""
Fake IPSC repeater peer for integration-testing dmrlink3.

Registers with an IPSC master, exchanges keepalives, and optionally
injects GROUP_VOICE calls so the bridge's forwarding path can be
exercised against a real running dmrlink3 instance.

Usage examples
--------------
# Manual: register and stay connected until Ctrl-C
  python fake_ipsc_peer.py --master 127.0.0.1:50100 --id 3112002 --port 50200

# Automated: send one call on TS1 and TS2, then exit
  python fake_ipsc_peer.py --master 127.0.0.1:50100 --id 3112002 --port 50200 \\
      --auto-call --src-sub 3112002 --tgid 9

# Point at the LOCAL-PEER port (client-mode entry point) instead of the master
  python fake_ipsc_peer.py --master 127.0.0.1:50100 --id 3112003 --port 50201
"""

import argparse
import asyncio
import logging
import signal
import struct
import sys
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).parent.parent))

from const import (
    MASTER_REG_REQ, MASTER_REG_REPLY,
    MASTER_ALIVE_REQ, MASTER_ALIVE_REPLY,
    PEER_LIST_REPLY, PEER_LIST_REQ,
    DE_REG_REQ,
    GROUP_VOICE,
    VOICE_HEAD, VOICE_TERM, SLOT1_VOICE,
    IPSC_VER, TS_CALL_MSK,
)
from dmr_utils3.utils import bytes_2, bytes_3, bytes_4

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger('fake_peer')

# Fixed capabilities: operational, digital, TS1+TS2, voice+data, not master
MODE  = bytes([0b01101010])
FLAGS = b'\x00\x00\x20\x0C'   # CON_APP | DATA_CALL | VOICE_CALL

SLOT_BURSTS = 6   # SLOT1_VOICE bursts between VOICE_HEAD and VOICE_TERM


def _make_reg_req(local_id):
    return MASTER_REG_REQ + local_id + MODE + FLAGS + IPSC_VER


def _make_alive_req(local_id):
    return MASTER_ALIVE_REQ + local_id + MODE + FLAGS + IPSC_VER


def _make_dereg(local_id):
    return DE_REG_REQ + local_id


def _make_gv(local_id, src_sub, dst_group, burst_type, timeslot, seq=0):
    """Build a GROUP_VOICE packet."""
    call_info = TS_CALL_MSK if timeslot == 2 else 0
    return (
        GROUP_VOICE
        + local_id
        + bytes([seq % 256])
        + src_sub
        + dst_group
        + b'\x00'                   # call_type
        + b'\x00\x00\x00\x00'     # call_ctrl
        + bytes([call_info])        # call_info
        + b'\x00' * 12            # RTP header
        + bytes([burst_type])       # burst_type
        + dst_group + src_sub      # DMR LC dst/src
        + b'\x00' * 28            # rest of DMR payload
    )


class FakeIPSCPeer(asyncio.DatagramProtocol):

    def __init__(self, local_id_int, master_host, master_port,
                 alive_timer=5, auto_call=False, src_sub_int=None, tgid_int=9):
        self.local_id    = bytes_4(local_id_int)
        self.master_addr = (master_host, master_port)
        self.alive_timer = alive_timer
        self.auto_call   = auto_call
        self.src_sub     = bytes_3(src_sub_int or local_id_int)
        self.tgid        = bytes_3(tgid_int)

        self.transport   = None
        self.connected   = False
        self._loop       = None
        self._tasks      = []
        self._seq        = 0
        self._stop       = None

    # -----------------------------------------------------------------------
    # asyncio.DatagramProtocol
    # -----------------------------------------------------------------------

    def connection_made(self, transport):
        self.transport = transport
        self._loop     = asyncio.get_running_loop()
        self._stop     = asyncio.Event()

        self._tasks.append(self._loop.create_task(self._maintenance()))
        logger.info('Bound, will register with master %s:%s', *self.master_addr)

    def datagram_received(self, data, addr):
        opcode  = data[0:1]
        peer_id = data[1:5]

        if opcode == MASTER_REG_REPLY:
            self.connected = True
            num_peers = struct.unpack('>H', data[10:12])[0]
            logger.info('Registered with master %s:%s (%s peers)', *addr, num_peers)
            if num_peers:
                self._send(PEER_LIST_REQ + self.local_id)
            if self.auto_call and not self._tasks[1:]:
                self._tasks.append(self._loop.create_task(self._run_auto_calls()))

        elif opcode == MASTER_ALIVE_REPLY:
            logger.debug('Keepalive reply from master')

        elif opcode == PEER_LIST_REPLY:
            logger.debug('Peer list received')

        else:
            logger.debug('Rx opcode %s from %s:%s', opcode.hex(), *addr)

    def error_received(self, exc):
        logger.error('UDP error: %s', exc)

    def connection_lost(self, exc):
        logger.warning('UDP connection lost: %s', exc)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _send(self, pkt):
        self.transport.sendto(pkt, self.master_addr)

    def _next_seq(self):
        self._seq = (self._seq + 1) % 256
        return self._seq

    async def _maintenance(self):
        while not self._stop.is_set():
            await asyncio.sleep(self.alive_timer)
            if not self.connected:
                self._send(_make_reg_req(self.local_id))
                logger.info('Sent MASTER_REG_REQ to %s:%s', *self.master_addr)
            else:
                self._send(_make_alive_req(self.local_id))
                logger.debug('Sent MASTER_ALIVE_REQ')

    def _send_voice_burst(self, burst_type, timeslot, seq):
        pkt = _make_gv(self.local_id, self.src_sub, self.tgid, burst_type, timeslot, seq)
        self._send(pkt)

    async def _send_call(self, timeslot, burst_gap=0.06):
        """Send VOICE_HEAD + SLOT_BURSTS + VOICE_TERM on the given timeslot."""
        burst_type = SLOT1_VOICE if timeslot == 1 else 0x8A  # SLOT2_VOICE
        seq = self._next_seq()

        self._send_voice_burst(VOICE_HEAD, timeslot, seq)
        logger.info('Call START TS%s TGID %s', timeslot, int.from_bytes(self.tgid, 'big'))
        await asyncio.sleep(burst_gap)

        for _ in range(SLOT_BURSTS):
            seq = self._next_seq()
            self._send_voice_burst(burst_type, timeslot, seq)
            await asyncio.sleep(burst_gap)

        seq = self._next_seq()
        self._send_voice_burst(VOICE_TERM, timeslot, seq)
        logger.info('Call END   TS%s TGID %s', timeslot, int.from_bytes(self.tgid, 'big'))

    async def _run_auto_calls(self):
        await asyncio.sleep(1)   # let registration settle
        await self._send_call(1)
        await asyncio.sleep(1)
        await self._send_call(2)
        await asyncio.sleep(0.5)
        self._deregister()
        self._stop.set()

    def _deregister(self):
        if self.connected:
            self._send(_make_dereg(self.local_id))
            logger.info('Sent DE_REG_REQ')
            self.connected = False

    async def run_until_stop(self):
        await self._stop.wait()


async def async_main(args):
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    master_host, master_port = args.master.rsplit(':', 1)
    peer = FakeIPSCPeer(
        local_id_int  = args.id,
        master_host   = master_host,
        master_port   = int(master_port),
        alive_timer   = args.alive_timer,
        auto_call     = args.auto_call,
        src_sub_int   = args.src_sub,
        tgid_int      = args.tgid,
    )

    def sig_handler(sig):
        logger.info('Signal %s — deregistering', signal.Signals(sig).name)
        peer._deregister()
        stop.set()

    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, sig_handler, sig)

    transport, _ = await loop.create_datagram_endpoint(
        lambda: peer,
        local_addr=('0.0.0.0', args.port),
    )
    logger.info('Fake IPSC peer bound on port %s, local radio ID %s', args.port, args.id)

    if args.auto_call:
        await peer.run_until_stop()
    else:
        await stop.wait()

    transport.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Fake IPSC peer for dmrlink3 testing')
    parser.add_argument('--master',     default='127.0.0.1:50100',
                        help='IPSC master host:port (default: 127.0.0.1:50100)')
    parser.add_argument('--id',         type=int, default=3112002,
                        help='Local radio ID (default: 3112002)')
    parser.add_argument('--port',       type=int, default=50200,
                        help='Local UDP port to bind (default: 50200)')
    parser.add_argument('--alive-timer', type=int, default=5, dest='alive_timer',
                        help='Keepalive interval in seconds (default: 5)')
    parser.add_argument('--auto-call',  action='store_true', dest='auto_call',
                        help='Send one call on TS1 and TS2 then exit')
    parser.add_argument('--src-sub',    type=int, default=None, dest='src_sub',
                        help='Source subscriber ID for auto-calls (default: same as --id)')
    parser.add_argument('--tgid',       type=int, default=9,
                        help='Talkgroup ID for auto-calls (default: 9)')
    cli = parser.parse_args()
    asyncio.run(async_main(cli))
