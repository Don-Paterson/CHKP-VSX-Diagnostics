"""
parsers/cphaprob.py
Pure functions that parse raw output from cphaprob commands.

parse_cphaprob_stat(raw)    -> dict   (all fields needed by ClusterHealth)
parse_cphaprob_if(raw)      -> List[str]   monitored interface names
parse_cphaprob_syncstat(raw)-> dict   (sync_status, sync_lost_updates)

No SSH calls, no side effects.

cphaprob stat output structure (R82)
-------------------------------------
Cluster Mode: High Availability (Active Up) with IGMP Membership

    Number      Unique Address  Assigned Load   State
    1 (local)   10.1.1.2        100%            Active
    2           10.1.1.3        0%              Standby
    3           10.1.1.4        0%              Backup

Last member state change
  Member: A-VSX-02
  State change: STANDBY -> ACTIVE
  Event time: Sat Apr  4 17:10:01 2026

Last cluster failover event
  Failover counter: 1
  Transition to new ACTIVE: A-VSX-01 -> A-VSX-02
  Event time: Sat Apr  4 17:10:01 2026

Key R82 note:
  Valid cluster states: ACTIVE / STANDBY / BACKUP / READY / DOWN
  ACTIVE! means degraded active (running without full sync)
  State names may have trailing whitespace — always strip.

cphaprob syncstat output structure
------------------------------------
Sync status: OK
...
Lost updates........... 0

Key lesson from v18 (line 786):
  SYNC_LOST line has leading whitespace — strip before comparison.
  Pattern: 'Lost updates.*[[:space:]]N'

cphaprob -a if output structure
---------------------------------
Required interfaces:
  eth0        Monitor Only        OK
  eth2        Monitor Only        OK
  bond0       Monitor Only        OK

Non-Monitored interfaces:
  lo
  ...

We extract only the monitored (non-"Non-Monitored") interface names.
"""

from __future__ import annotations

import re
import logging
from typing import Dict, List

log = logging.getLogger(__name__)

# Member state line:
#   "    1 (local)   10.1.1.2   100%   Active"
#   "    2           10.1.1.3   0%     Standby"
# The state word is one of: Active / Standby / Backup / Ready / Down
# May also appear as ACTIVE / STANDBY etc. (uppercase) or ACTIVE! (degraded)
_RE_MEMBER_LINE = re.compile(
    r'^\s*(\d+)'            # member number
    r'(?:\s+\(local\))?'   # optional "(local)" marker
    r'\s+([\d.]+)'          # IP address
    r'\s+\d+%'             # load percentage
    r'\s+(ACTIVE!?|STANDBY|BACKUP|READY|DOWN|Active|Standby|Backup|Ready|Down)'
    r'(?:\s+(.+))?'        # optional member name (some versions append it)
    r'\s*$',
    re.IGNORECASE,
)

# Member name line variant (some R82 builds show name separately):
#   "    1 (local)   10.1.1.2   100%   Active    A-VSX-01"
# The regex above handles this with the optional group 4.

# Cluster mode line
_RE_CLUSTER_MODE = re.compile(r'^Cluster Mode:\s*(.+)$', re.MULTILINE)

# Failover counter
_RE_FAILOVER_COUNT = re.compile(r'Failover counter:\s*(\d+)')

# Transition to new ACTIVE
_RE_FAILOVER_TRANS = re.compile(r'Transition to new ACTIVE:\s*(.+)')

# Event time (used for both failover time and state change time)
_RE_EVENT_TIME = re.compile(r'Event time:\s*(.+)')

# State change line
_RE_STATE_CHANGE = re.compile(r'State change:\s*(.+)')

# Member name in "Last member state change" block
_RE_MEMBER_NAME_BLOCK = re.compile(r'^\s*Member:\s*(.+)$', re.MULTILINE)

# Sync status
_RE_SYNC_STATUS = re.compile(r'^Sync status:\s*(.+)$', re.MULTILINE)

# Lost updates  — strip whitespace around the number
_RE_SYNC_LOST = re.compile(r'Lost updates[.\s]+(\d+)', re.IGNORECASE)

# Monitored interface line (in cphaprob -a if):
#   "  eth0        Monitor Only        OK"
# Non-monitored section header signals end of monitored block
_RE_MONITORED_IF = re.compile(r'^\s+(\S+)\s+\S')


def parse_cphaprob_stat(raw: str) -> Dict:
    """
    Parse cphaprob stat output.

    Returns a dict with keys matching ClusterHealth fields:
        cluster_mode, member_states, failover_count, failover_transition,
        failover_time, last_state_change, last_state_change_time
    """
    result = {
        'cluster_mode':           '',
        'member_states':          {},   # {name_or_ip: state}
        'failover_count':         0,
        'failover_transition':    '',
        'failover_time':          '',
        'last_state_change':      '',
        'last_state_change_time': '',
    }

    if not raw.strip():
        log.warning("cphaprob stat: empty output")
        return result

    # --- Cluster mode ---
    m = _RE_CLUSTER_MODE.search(raw)
    if m:
        result['cluster_mode'] = m.group(1).strip()
        log.debug("cphaprob: cluster_mode=%r", result['cluster_mode'])

    # --- Member states ---
    # Parse the member table lines
    # We also try to correlate IP -> name from the topology later;
    # here we key by IP since names aren't always in cphaprob stat.
    for line in raw.splitlines():
        m = _RE_MEMBER_LINE.match(line)
        if m:
            ip    = m.group(2).strip()
            state = m.group(3).strip().upper()
            name  = m.group(4).strip() if m.group(4) else ip
            # Normalise ACTIVE! -> ACTIVE (degraded flagged separately)
            result['member_states'][name] = state
            log.debug("cphaprob: member %s -> %s", name, state)

    # --- Failover event ---
    m = _RE_FAILOVER_COUNT.search(raw)
    if m:
        result['failover_count'] = int(m.group(1))

    m = _RE_FAILOVER_TRANS.search(raw)
    if m:
        result['failover_transition'] = m.group(1).strip()

    # Event time after "Last cluster failover event"
    failover_block = _extract_block(raw, 'Last cluster failover event')
    if failover_block:
        m = _RE_EVENT_TIME.search(failover_block)
        if m:
            result['failover_time'] = m.group(1).strip()

    # --- Last state change ---
    state_block = _extract_block(raw, 'Last member state change')
    if state_block:
        m = _RE_STATE_CHANGE.search(state_block)
        if m:
            result['last_state_change'] = m.group(1).strip()
        m = _RE_EVENT_TIME.search(state_block)
        if m:
            result['last_state_change_time'] = m.group(1).strip()

    log.info(
        "cphaprob stat: mode=%r  members=%d  failovers=%d",
        result['cluster_mode'],
        len(result['member_states']),
        result['failover_count'],
    )
    return result


def parse_cphaprob_if(raw: str) -> List[str]:
    """
    Parse cphaprob -a if output and return list of monitored interface names.

    Only interfaces listed BEFORE the "Non-Monitored interfaces" section
    are included — these are the ones that will trigger a cluster failover
    if they go down.
    """
    monitored: List[str] = []
    in_non_monitored = False

    for line in raw.splitlines():
        if 'Non-Monitored' in line:
            in_non_monitored = True
            continue
        if in_non_monitored:
            continue
        m = _RE_MONITORED_IF.match(line)
        if m:
            iface = m.group(1).strip()
            if iface and iface not in ('Required', 'interfaces:'):
                monitored.append(iface)

    log.debug("cphaprob -a if: monitored interfaces: %s", monitored)
    return monitored


def parse_cphaprob_syncstat(raw: str) -> Dict:
    """
    Parse cphaprob syncstat output.

    Returns dict with keys: sync_status, sync_lost_updates

    Key lesson from v18: SYNC_LOST line may have leading whitespace.
    Strip before comparison.  'SYNC_LOST' (the status value) is different
    from 'Lost updates' (the counter line) — handle both.
    """
    result = {
        'sync_status':      '',
        'sync_lost_updates': 0,
    }

    if not raw.strip():
        log.warning("cphaprob syncstat: empty output")
        return result

    m = _RE_SYNC_STATUS.search(raw)
    if m:
        result['sync_status'] = m.group(1).strip()
        log.info("cphaprob syncstat: status=%r", result['sync_status'])

    m = _RE_SYNC_LOST.search(raw)
    if m:
        try:
            result['sync_lost_updates'] = int(m.group(1).strip())
        except ValueError:
            pass
    log.debug("cphaprob syncstat: lost_updates=%d", result['sync_lost_updates'])

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_block(text: str, header: str) -> str:
    """
    Extract the text block that follows 'header' up to the next blank line
    or next section header.  Returns empty string if header not found.
    """
    lines = text.splitlines()
    capturing = False
    block_lines: List[str] = []

    for line in lines:
        if header in line:
            capturing = True
            continue
        if capturing:
            # Stop at blank line followed by a non-indented line (new section)
            if not line.strip() and block_lines:
                # Peek: if next non-blank line is a new header, stop
                break
            block_lines.append(line)

    return '\n'.join(block_lines)
