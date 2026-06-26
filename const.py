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

# IPSC Protocol Constants and Bitmasks -- merged from ipsc_const.py and ipsc_mask.py

# Known IPSC Message Types
CALL_CONFIRMATION     = b'\x05'
TXT_MESSAGE_ACK       = b'\x54'
CALL_MON_STATUS       = b'\x61'
CALL_MON_RPT          = b'\x62'
CALL_MON_NACK         = b'\x63'
XCMP_XNL              = b'\x70'
GROUP_VOICE           = b'\x80'
PVT_VOICE             = b'\x81'
GROUP_DATA            = b'\x83'
PVT_DATA              = b'\x84'
RPT_WAKE_UP           = b'\x85'
UNKNOWN_COLLISION     = b'\x86'
MASTER_REG_REQ        = b'\x90'
MASTER_REG_REPLY      = b'\x91'
PEER_LIST_REQ         = b'\x92'
PEER_LIST_REPLY       = b'\x93'
PEER_REG_REQ          = b'\x94'
PEER_REG_REPLY        = b'\x95'
MASTER_ALIVE_REQ      = b'\x96'
MASTER_ALIVE_REPLY    = b'\x97'
PEER_ALIVE_REQ        = b'\x98'
PEER_ALIVE_REPLY      = b'\x99'
DE_REG_REQ            = b'\x9A'
DE_REG_REPLY          = b'\x9B'

# IPSC Version Information
IPSC_VER_14           = b'\x00'
IPSC_VER_15           = b'\x00'
IPSC_VER_15A          = b'\x00'
IPSC_VER_16           = b'\x01'
IPSC_VER_17           = b'\x02'
IPSC_VER_18           = b'\x02'
IPSC_VER_19           = b'\x03'
IPSC_VER_22           = b'\x04'

LINK_TYPE_IPSC        = b'\x04'

BURST_DATA_TYPE = {
    'VOICE_HEAD':  b'\x01',
    'VOICE_TERM':  b'\x02',
    'SLOT1_VOICE': b'\x0A',
    'SLOT2_VOICE': b'\x8A',
}

# IPSC Version field used in registration packets
IPSC_VER = LINK_TYPE_IPSC + IPSC_VER_17 + LINK_TYPE_IPSC + IPSC_VER_16

# Packet type membership lists
ANY_PEER_REQUIRED = [GROUP_VOICE, PVT_VOICE, GROUP_DATA, PVT_DATA, CALL_MON_STATUS,
                     CALL_MON_RPT, CALL_MON_NACK, XCMP_XNL, RPT_WAKE_UP, DE_REG_REQ]
PEER_REQUIRED     = [PEER_ALIVE_REQ, PEER_ALIVE_REPLY, PEER_REG_REQ, PEER_REG_REPLY]
MASTER_REQUIRED   = [PEER_LIST_REPLY, MASTER_ALIVE_REPLY]
USER_PACKETS      = [GROUP_VOICE, PVT_VOICE, GROUP_DATA, PVT_DATA]

# RCM Timeslot constants
TS = {b'\x00': '1', b'\x01': '2'}


# ---------------------------------------------------------------------------
# IPSC Bitmasks (from ipsc_mask.py)
# ---------------------------------------------------------------------------

# LINKING STATUS byte flags
#   xx.. ....  Peer Operational (01 = operational)
#   ..xx ....  Peer MODE: 00=No Radio, 01=Analog, 10=Digital
#   .... xx..  IPSC Slot 1: 10=on, 01=off
#   .... ..xx  IPSC Slot 2: 10=on, 01=off
PEER_OP_MSK       = 0b01000000
PEER_MODE_MSK     = 0b00110000
PEER_MODE_ANALOG  = 0b00010000
PEER_MODE_DIGITAL = 0b00100000
IPSC_TS1_MSK      = 0b00001100
IPSC_TS2_MSK      = 0b00000011

# SERVICE FLAGS byte 3
CSBK_MSK          = 0b10000000
RPT_MON_MSK       = 0b01000000
CON_APP_MSK       = 0b00100000

# SERVICE FLAGS byte 4
XNL_STAT_MSK      = 0b10000000
XNL_MSTR_MSK      = 0b01000000
XNL_SLAVE_MSK     = 0b00100000
PKT_AUTH_MSK      = 0b00010000
DATA_CALL_MSK     = 0b00001000
VOICE_CALL_MSK    = 0b00000100
MSTR_PEER_MSK     = 0b00000001

# TIMESLOT CALL & STATUS byte (byte 17 of voice/data packets)
#   .x.. ....  End flag (0=in-progress, 1=end)
#   ..x. ....  Timeslot (0=TS1, 1=TS2)
END_MSK           = 0b01000000
TS_CALL_MSK       = 0b00100000

# RTP header bitmasks
RTP_VER_MSK       = 0b11000000
RTP_PAD_MSK       = 0b00100000
RTP_EXT_MSK       = 0b00010000
RTP_CSIC_MSK      = 0b00001111
RTP_MRKR_MSK      = 0b10000000
RTP_PAY_TYPE_MSK  = 0b01111111
