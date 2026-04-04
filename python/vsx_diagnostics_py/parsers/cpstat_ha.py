"""
parsers/cpstat_ha.py
Pure function that parses raw output from: cpstat ha -f all

parse_cpstat_ha(raw) -> List[PNOTEEntry]

No SSH calls, no side effects.

cpstat ha -f all output structure
-----------------------------------
The output contains three pipe-delimited tables separated by headers:

    HA State table
    | Name           | State   | ... |
    |----------------|---------|-----|
    | A-VSX-01       | Active  | ... |

    Problem Notification table
    | Name           | Status  |
    |----------------|---------|
    | Firewall       | OK      |
    | ClusterXL      | OK      |

    Cluster IPs table
    | IP             | ...     |

Critical lesson from v18 (lines 794-804):
    PNOTE parsing MUST be scoped strictly to the "Problem Notification table"
    section only.  The other tables have similar pipe-delimited format and
    will produce false positives if included.

    The awk in v18:
        /Problem Notification table/ { in_pnote=1; next }
        /Cluster IPs table/          { in_pnote=0 }

    We replicate this exactly with a state-machine in Python.
"""

from __future__ import annotations

import re
import logging
from typing import List

from models.data import PNOTEEntry

log = logging.getLogger(__name__)


def parse_cpstat_ha(raw: str) -> List[PNOTEEntry]:
    """
    Parse cpstat ha -f all output and return all PNOTE entries.

    Scoped strictly to the "Problem Notification table" section.
    Entries with status "OK" are included so the caller can distinguish
    "all OK" from "not collected".

    The assessor filters to non-OK entries via ClusterHealth.pnote_issues.
    """
    entries: List[PNOTEEntry] = []

    if not raw.strip():
        log.warning("cpstat ha: empty output")
        return entries

    in_pnote = False

    for line in raw.splitlines():
        # State transitions — mirror v18 awk exactly
        if 'Problem Notification table' in line:
            in_pnote = True
            continue
        if 'Cluster IPs table' in line:
            in_pnote = False
            continue

        if not in_pnote:
            continue

        # Must contain a pipe to be a data row
        if '|' not in line:
            continue

        # Split on pipe, strip whitespace from each field
        parts = [p.strip() for p in line.split('|')]
        # parts[0] is empty (before first pipe), parts[1]=name, parts[2]=status
        if len(parts) < 3:
            continue

        name   = parts[1].strip()
        status = parts[2].strip()

        # Skip header row, separator rows, and empty fields
        if not name or not status:
            continue
        if name in ('Name', '----', '====') or set(name) <= set('-= '):
            continue
        if status in ('Status', '----', '====') or set(status) <= set('-= '):
            continue

        entries.append(PNOTEEntry(name=name, status=status))
        log.debug("cpstat ha PNOTE: %s -> %s", name, status)

    issues = [e for e in entries if e.status != 'OK']
    log.info(
        "cpstat ha: %d PNOTE entries (%d issues)",
        len(entries), len(issues),
    )
    if issues:
        for e in issues:
            log.warning("cpstat ha PNOTE issue: %s -> %s", e.name, e.status)

    return entries
