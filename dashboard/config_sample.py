###############################################################################
#   Copyright (C) 2016-2026  Cortney T. Buffington, N0MJS <n0mjs@me.com>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 3 of the License, or
#   (at your option) any later version.
###############################################################################

# Copy this file to config.py and edit for your install.

# Display / branding
REPORT_NAME     = 'My DMRlink3 System'  # Shown in the dashboard header

# Connection to dmrlink3's reporting feed ([REPORTS] in dmrlink.cfg)
DMRLINK_IP      = '127.0.0.1'           # dmrlink3 reporting host
DMRLINK_PORT    = 4321                  # dmrlink3 REPORT_PORT

# Web server
WEB_HOST        = '0.0.0.0'             # Interface to bind the dashboard
WEB_PORT        = 8080                  # Port (must be > 1024 if not root)

# Call log
LOG_LINES       = 300                   # Number of recent call-log entries to retain

# Alias files (map DMR IDs to callsigns / talkgroup names).
# Point PATH at dmrlink3's directory to share the downloaded files, or
# keep your own copies here.
PATH            = '../'
PEER_FILE       = 'peer_ids.json'
SUBSCRIBER_FILE = 'subscriber_ids.json'
TGID_FILE       = 'talkgroup_ids.json'  # optional {id: name} dict; ok if missing
LOCAL_SUB_FILE  = ''                    # optional local override, '' to disable
LOCAL_PEER_FILE = ''                    # optional local override, '' to disable
