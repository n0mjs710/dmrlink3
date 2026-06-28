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

"""
Trunk v2 — DMRlink3 server-to-server voice bridge
==================================================

Historical context
------------------
The original DMRlink "trunk" concept (circa 2013) was a bilateral IPSC
connection between two DMRlink instances that bypassed the two-timeslot
restriction of repeater endpoints and the traffic-cop contention logic.
That idea was the embryo from which the BrandMeister / IPSC2 teams grew
the OpenBridge Protocol (OBP); hblink3 implements OBP for the HBP world.

Trunk v2 applies the same lessons to the IPSC side: a purpose-built
server-to-server protocol layered on top of IPSC payloads, avoiding the
IPSC registration/keepalive machinery while preserving the existing
bridge routing core unchanged.

Design
------
A TRUNK system presents the same interface to the bridge routing core as
any IPSC MASTER or PEER system:

  - The routing core calls transmit_group_voice() to send a packet out.
  - Received packets are decoded and injected back via group_voice().
  - The STATUS dict has the same TS-keyed structure used by bridgeIPSC so
    the routing core can read/write TX state after forwarding.
  - Contention checking is automatically bypassed for TRUNK systems (bridge.py
    adds them to the TRUNKS list at startup), so the TS-keyed STATUS dict is
    harmless even though a trunk can carry multiple simultaneous streams on
    one timeslot.

The trunk layer adds a 6-byte header and strips it on receipt.  Everything
else is the routing core's concern.

Wire format
-----------
Every UDP datagram between two trunk peers is a 6-byte header followed by
the unchanged GROUP_VOICE IPSC packet produced by the routing core (already
rewritten for TS/TGID/peer-ID by bridge.py before transmission):

    Offset  Len  Field
    ------  ---  -----
     0       4   Stream ID — 4 random bytes, generated fresh at each call
                 start (VOICE_HEAD burst) and held constant through VOICE_TERM
                 or hangtime expiry.  Carried on every packet so the receiver
                 can associate bursts with a call without relying on the IPSC
                 sequence number (which is NOT monotonic when Talker Alias
                 alternates between LC and TA fragments in voice bursts).
     4       2   Flags — reserved; transmitted as 0x0000.  Future protocol
                 versions may define bits here (e.g. version indicators).
     6       N   IPSC payload — stock GROUP_VOICE packet, unchanged.

Stream ID details
-----------------
On VOICE_HEAD: generate os.urandom(4), store in _tx_streams[(ts, tgid)],
  append to _local_stream_ids deque for loop prevention.
On interior bursts: look up existing stream ID from _tx_streams.
On VOICE_TERM: forward with stream ID, then remove from _tx_streams.
Late-open (interior burst arrives with no prior VOICE_HEAD): generate a
  stream ID on the first burst and proceed normally.  This handles the
  common radio case where the VOICE_HEAD did not decode at the repeater
  or was lost in the IPSC→trunk hand-off.

Loop prevention
---------------
Any inbound packet whose stream ID appears in _local_stream_ids was
originated by this instance and has looped back.  Drop it silently.

Authentication
--------------
Authentication is by configured peer IP + port (source sockaddr checked
on every inbound packet).  No per-packet cryptographic MAC is used:

  - Ham radio content carries no private information; encryption is
    illegal in the US (47 CFR §97.113(a)(4)).
  - Both peers require stable public IPs to serve their own repeaters.
  - Source IP + port validation is the same model IPSC uses when
    AUTH_ENABLED is False, and is standard for amateur radio protocols
    (IRLP, Echolink, Wires-X, etc.).

Fast TGID ingress filter
------------------------
The _tgid_filter frozenset contains every TGID that appears in any
active bridge rule for this system (built by bridge.py at startup via
set_tgid_filter()).  Inbound packets whose TGID is not in this set are
dropped inside datagram_received() before group_voice() is ever called,
avoiding a full routing engine traversal for traffic this server has no
use for.

If set_tgid_filter() has not been called (e.g. standalone dmrlink.py
without bridge rules), _tgid_filter is None and all TGIDs pass through.
"""

import asyncio
import logging
import os
from collections import deque
from time import time

from const import (
    GROUP_VOICE,
    VOICE_HEAD, VOICE_TERM,
    GV_BURST_TYPE_OFF, GV_CALL_INFO_OFF, GV_MIN_LEN,
    TS_CALL_MSK, END_MSK,
)
from dmr_utils3.utils import int_id
from dmrlink import run_periodic

__author__     = 'Cortney T. Buffington, N0MJS'
__copyright__  = 'Copyright (c) 2016-2026 Cortney T. Buffington, N0MJS and the K0USY Group'
__license__    = 'GNU GPLv3'
__maintainer__ = 'Cort Buffington, N0MJS'
__email__      = 'n0mjs@me.com'

logger = logging.getLogger(__name__)

# Wire format constants
TRUNK_HEADER_LEN  = 6           # 4-byte stream ID + 2-byte flags
TRUNK_FLAGS       = b'\x00\x00' # reserved flags field; always zero

# Loop-prevention deque depth.  100 entries covers roughly 50 concurrent calls
# with two boundaries each (head + term), which is well beyond any realistic load.
TRUNK_LOOP_DEQUE_LEN = 100

# Seconds of silence before an open inbound stream is considered dead and
# removed from _rx_streams.  Matches typical GROUP_HANGTIME values.
TRUNK_STREAM_HANGTIME = 8.0


class TRUNK(asyncio.DatagramProtocol):
    """
    Trunk v2 system endpoint — wire protocol layer.

    Handles the UDP send/receive path, stream ID generation, loop prevention,
    fast TGID filtering, and inbound stream tracking.  The routing logic lives
    in a subclass (bridgeTRUNK in bridge.py), which overrides group_voice().

    This split mirrors hblink3's OPENBRIDGE / bridgeOBP pattern: the protocol
    class handles framing; the bridge subclass handles routing.
    """

    def __init__(self, _name, _config, _report):
        self._system = _name
        self._CONFIG = _config
        self._report = _report
        self._config = self._CONFIG['SYSTEMS'][self._system]

        trunk_cfg        = self._config['TRUNK']
        self._peer_ip    = trunk_cfg['PEER_IP']
        self._peer_port  = trunk_cfg['PEER_PORT']
        self._peer_sock  = trunk_cfg['PEER_SOCK']   # pre-built tuple for sendto()

        # STATUS dict — TS-keyed to match bridgeIPSC.STATUS so the bridge
        # routing core can write TX state after forwarding to this system.
        # Contention checks (which read this dict) are bypassed for TRUNK
        # systems via the TRUNKS list, so only the TX state writes matter.
        self.STATUS = {
            1: {'RX_TGID': b'\x00\x00\x00', 'TX_TGID': b'\x00\x00\x00',
                'RX_TIME': 0,                'TX_TIME': 0,
                'RX_SRC_SUB': b'\x00\x00\x00', 'TX_SRC_SUB': b'\x00\x00\x00'},
            2: {'RX_TGID': b'\x00\x00\x00', 'TX_TGID': b'\x00\x00\x00',
                'RX_TIME': 0,                'TX_TIME': 0,
                'RX_SRC_SUB': b'\x00\x00\x00', 'TX_SRC_SUB': b'\x00\x00\x00'},
        }

        # Outbound stream tracking: (ts, tgid_bytes) -> stream_id_bytes.
        # Created on VOICE_HEAD (or first burst for late-open); removed on VOICE_TERM.
        # Stale entries are harmless — they are overwritten by the next VOICE_HEAD.
        self._tx_streams = {}

        # Stream IDs generated by this instance (loop prevention).
        # Bounded deque: oldest entries are evicted automatically.
        self._local_stream_ids = deque(maxlen=TRUNK_LOOP_DEQUE_LEN)

        # Inbound stream tracking: stream_id_bytes -> {ts, tgid, last_seen}.
        # Reaped periodically by _reap_stale_streams().
        self._rx_streams = {}

        # Fast-drop TGID filter (frozenset of 3-byte tgid bytes objects).
        # None = accept all TGIDs (before set_tgid_filter() is called).
        self._tgid_filter = None

        logger.info('(%s) TRUNK v2 instance created — peer %s:%s',
                    self._system, self._peer_ip, self._peer_port)

    # -----------------------------------------------------------------------
    # asyncio.DatagramProtocol interface
    # -----------------------------------------------------------------------

    def connection_made(self, transport):
        self.transport = transport
        asyncio.get_running_loop().create_task(
            run_periodic(TRUNK_STREAM_HANGTIME, self._reap_stale_streams, self._system))
        logger.info('(%s) TRUNK UDP endpoint ready', self._system)

    def error_received(self, exc):
        logger.error('(%s) TRUNK UDP error: %s', self._system, exc)

    def connection_lost(self, exc):
        logger.warning('(%s) TRUNK UDP connection lost: %s', self._system, exc)

    def datagram_received(self, data, addr):
        # Peer authentication: source sockaddr must match configured peer.
        if addr != self._peer_sock:
            logger.warning('(%s) TRUNK packet from unexpected source %s:%s — discarded',
                           self._system, addr[0], addr[1])
            return

        # Minimum: 6-byte trunk header + GV_MIN_LEN bytes of IPSC payload.
        if len(data) < TRUNK_HEADER_LEN + GV_MIN_LEN:
            logger.warning('(%s) TRUNK packet too short (%d bytes) — discarded',
                           self._system, len(data))
            return

        # Unpack trunk header.
        stream_id = data[:4]
        # data[4:6] — flags; reserved and currently ignored on receipt
        ipsc_data = data[TRUNK_HEADER_LEN:]

        # Loop prevention: stream IDs we generated that arrive back here are loops.
        if stream_id in self._local_stream_ids:
            logger.debug('(%s) TRUNK loop detected — stream %s discarded',
                         self._system, stream_id.hex())
            return

        # Only GROUP_VOICE is handled; other packet types are not bridged.
        if ipsc_data[0:1] != GROUP_VOICE:
            logger.debug('(%s) TRUNK non-GROUP_VOICE opcode 0x%s — discarded',
                         self._system, ipsc_data[0:1].hex())
            return

        # Decode the IPSC fields needed for filtering and routing.
        _peerid     = ipsc_data[1:5]
        _src_sub    = ipsc_data[6:9]
        _dst_group  = ipsc_data[9:12]
        _call_info  = ipsc_data[GV_CALL_INFO_OFF]
        _burst_type = ipsc_data[GV_BURST_TYPE_OFF]
        _end        = bool(_call_info & END_MSK)

        # Timeslot: VOICE_HEAD/VOICE_TERM encode TS in the call_info byte;
        # interior voice bursts encode it in the high bit of burst_type.
        if _burst_type in (VOICE_HEAD, VOICE_TERM):
            _ts = 2 if (_call_info & TS_CALL_MSK) else 1
        else:
            _ts = 2 if (_burst_type & 0x80) else 1

        # Fast TGID ingress filter — drop before touching the routing engine.
        if self._tgid_filter is not None and _dst_group not in self._tgid_filter:
            logger.debug('(%s) TRUNK fast-drop TGID %s (not in bridge rules)',
                         self._system, int_id(_dst_group))
            return

        # Update inbound stream tracking.
        if _burst_type == VOICE_TERM:
            s = self._rx_streams.pop(stream_id, None)
            if s:
                logger.info('(%s) TRUNK RX stream %s TS%s TGID %s ended',
                            self._system, stream_id.hex(), _ts, int_id(_dst_group))
        else:
            if stream_id not in self._rx_streams:
                orphan = _burst_type not in (VOICE_HEAD,)
                logger.info('(%s) TRUNK RX stream %s TS%s TGID %s %s',
                            self._system, stream_id.hex(), _ts, int_id(_dst_group),
                            'started (late-entry / orphan)' if orphan else 'started')
            self._rx_streams[stream_id] = {
                'ts': _ts, 'tgid': _dst_group, 'last_seen': time()
            }

        # Hand the plain IPSC payload to the routing core.
        self.group_voice(_src_sub, _dst_group, _ts, _end, _peerid, ipsc_data)

    # -----------------------------------------------------------------------
    # Routing core egress interface
    # -----------------------------------------------------------------------

    def transmit_group_voice(self, _src_sub, _dst_group, _ts, _burst_type, _data, _src_peer=None):
        """Send a GROUP_VOICE packet to the trunk peer.

        Called by the bridge routing core with a fully-rewritten IPSC packet
        (peer ID, TS, and TGID already substituted by bridge.py).  Wraps it
        in the 6-byte trunk header and sends it to the configured peer UDP
        address.

        Stream ID lifecycle:
          VOICE_HEAD  → generate fresh stream ID, store, add to loop-prevention deque
          Interior    → look up stored stream ID (generate if missing: late-open TX)
          VOICE_TERM  → forward with stored stream ID, then retire it
        """
        stream_key = (_ts, _dst_group)

        if _burst_type == VOICE_HEAD or stream_key not in self._tx_streams:
            stream_id = os.urandom(4)
            self._tx_streams[stream_key] = stream_id
            self._local_stream_ids.append(stream_id)
            if _burst_type != VOICE_HEAD:
                logger.debug('(%s) TRUNK TX late-open stream %s TS%s TGID %s',
                             self._system, stream_id.hex(), _ts, int_id(_dst_group))
            else:
                logger.debug('(%s) TRUNK TX stream %s TS%s TGID %s started',
                             self._system, stream_id.hex(), _ts, int_id(_dst_group))

        stream_id = self._tx_streams[stream_key]
        self.transport.sendto(stream_id + TRUNK_FLAGS + _data, self._peer_sock)

        if _burst_type == VOICE_TERM:
            self._tx_streams.pop(stream_key, None)
            logger.debug('(%s) TRUNK TX stream %s TS%s TGID %s ended',
                         self._system, stream_id.hex(), _ts, int_id(_dst_group))

    # -----------------------------------------------------------------------
    # Routing core ingress interface (override in subclass)
    # -----------------------------------------------------------------------

    def group_voice(self, _src_sub, _dst_sub, _ts, _end, _peerid, _data):
        """Inbound GROUP_VOICE from trunk peer — override in bridgeTRUNK.

        The plain IPSC payload (trunk header already stripped) is passed here
        after source validation, loop detection, and TGID filtering.  A
        subclass overrides this to inject the frame into the bridge routing
        engine exactly as bridgeIPSC.group_voice() does for IPSC sources.
        """

    def de_register_self(self):
        """No-op — TRUNK has no IPSC registration to withdraw on shutdown."""
        logger.info('(%s) TRUNK — no de-registration required, continuing shutdown',
                    self._system)

    # -----------------------------------------------------------------------
    # TGID fast-drop filter installation
    # -----------------------------------------------------------------------

    def set_tgid_filter(self, tgid_set):
        """Install the ingress TGID allow-set.

        Called by bridge.py after loading bridge rules.  tgid_set must be a
        frozenset of 3-byte bytes objects matching the TGID values used in
        BRIDGES (the same type as member['TGID'] after make_bridge_config()
        converts them with bytes_3()).

        Inbound packets for any TGID not in this set are dropped in
        datagram_received() before the routing engine is invoked.
        """
        self._tgid_filter = tgid_set
        logger.info('(%s) TRUNK TGID ingress filter installed: %d TGIDs',
                    self._system, len(tgid_set))

    # -----------------------------------------------------------------------
    # Stale stream reaper (run_periodic callback)
    # -----------------------------------------------------------------------

    def _reap_stale_streams(self):
        """Expire inbound streams that have gone quiet past TRUNK_STREAM_HANGTIME.

        Called every TRUNK_STREAM_HANGTIME seconds via run_periodic().
        Outbound streams (_tx_streams) are keyed by (ts, tgid) and
        self-correct on the next VOICE_HEAD, so they are not reaped here.
        """
        cutoff = time() - TRUNK_STREAM_HANGTIME
        stale  = [sid for sid, s in self._rx_streams.items()
                  if s['last_seen'] < cutoff]
        for sid in stale:
            s = self._rx_streams.pop(sid)
            logger.info('(%s) TRUNK RX stream %s TS%s TGID %s reaped (hangtime)',
                        self._system, sid.hex(), s['ts'], int_id(s['tgid']))
