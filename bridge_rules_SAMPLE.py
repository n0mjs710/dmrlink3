'''
DMRlink3 bridge rules — SAMPLE FILE
====================================
THIS FILE WILL NOT WORK AS-IS. You must substitute your own system names,
talkgroup IDs, and timeslot assignments.

Rename this file to bridge_rules.py (or whatever you pass to -b/--bridge-rules
on the bridge.py command line).

BRIDGE STRUCTURE
----------------
Bridges are organized by name (e.g. 'WORLDWIDE', 'STATEWIDE'). Think of each
bridge as a conference room: any system that is ACTIVE on a bridge will receive
all voice traffic arriving on that bridge from any other active system.

Each system entry under a bridge has the following keys:

    SYSTEM  - Name matching a section in dmrlink.cfg (case-sensitive)
    TS      - Timeslot to match (1 or 2)
    TGID    - Integer talkgroup ID to match on that timeslot
    ACTIVE  - Initial state: True = bridging active, False = bridging off
    TIMEOUT - Timer duration in MINUTES (used with TO_TYPE ON or OFF)
    TO_TYPE - Timer behavior:
                'ON'   - When activated, deactivate after TIMEOUT minutes
                'OFF'  - When deactivated, reactivate after TIMEOUT minutes
                'NONE' - No timer; state only changes via ON/OFF triggers
    ON      - List of TGIDs that activate this entry (PTT on one of these
              talkgroups keys the bridge on)
    OFF     - List of TGIDs that deactivate this entry
    RESET   - List of TGIDs that reset a running ON-type timer without
              changing active state. Useful when voice traffic arrives on a
              different TGID than the trigger.

TRUNKS
------
TRUNKS is a list of system names that bypass contention handling. All traffic
is always forwarded to a trunk system without checking group-hangtime or
timeslot-clear-time rules. Leave as [] if not needed.

BRIDGE_CONF
-----------
Global options for bridge.py. Currently unused beyond the REPORT key (which
is vestigial from the dmrlink pickle-reporting era and can be left as-is).
'''

# Global bridge.py options
BRIDGE_CONF = {
    'REPORT': True,
}

# Trunk systems — contention handling bypassed for these
TRUNKS = []

# Bridge definitions
BRIDGES = {
    'WORLDWIDE': [
        {'SYSTEM': 'MASTER-1', 'TS': 1, 'TGID': 91,   'ACTIVE': True,  'TIMEOUT': 2, 'TO_TYPE': 'ON',   'ON': [91],  'OFF': [9, 10], 'RESET': []},
        {'SYSTEM': 'CLIENT-1', 'TS': 1, 'TGID': 91,   'ACTIVE': True,  'TIMEOUT': 2, 'TO_TYPE': 'ON',   'ON': [91],  'OFF': [9, 10], 'RESET': []},
    ],
    'NATIONWIDE': [
        {'SYSTEM': 'MASTER-1', 'TS': 1, 'TGID': 3100, 'ACTIVE': True,  'TIMEOUT': 2, 'TO_TYPE': 'NONE', 'ON': [3,],  'OFF': [8, 10], 'RESET': []},
        {'SYSTEM': 'CLIENT-2', 'TS': 1, 'TGID': 3100, 'ACTIVE': True,  'TIMEOUT': 2, 'TO_TYPE': 'NONE', 'ON': [3,],  'OFF': [8, 10], 'RESET': []},
    ],
    'STATEWIDE': [
        {'SYSTEM': 'MASTER-1', 'TS': 2, 'TGID': 3129, 'ACTIVE': False, 'TIMEOUT': 5, 'TO_TYPE': 'NONE', 'ON': [4,],  'OFF': [7, 10], 'RESET': []},
        {'SYSTEM': 'CLIENT-2', 'TS': 2, 'TGID': 3129, 'ACTIVE': False, 'TIMEOUT': 5, 'TO_TYPE': 'NONE', 'ON': [4,],  'OFF': [7, 10], 'RESET': []},
    ],
}


if __name__ == '__main__':
    from pprint import pprint
    pprint(BRIDGES)
