"""
parsers/securexl.py
Pure function for parsing fwaccel stat output.

parse_securexl_status(raw) -> str   "enabled" / "disabled" / "n/a"

R82 lesson from v18 (lines 696-701):
    Two fwaccel stat output formats exist:

    Format 1 (older / R81.10):
        Accelerator Status : enabled

    Format 2 (R82 KPPAK table):
        |Id|Name|Status|Interfaces|Features|
        | 0|KPPAK|enabled|...|...|

    v18 tries Format 1 first, falls back to Format 2:
        saccel=$(fwaccel stat | awk '/^Accelerator Status/ {print $NF}')
        if [ -z "$saccel" ]; then
            saccel=$(fwaccel stat | awk -F'|' '/KPPAK/ {gsub(/[ \\t]/, "", $4); print $4}')
        fi

    Field index 3 (0-based) after pipe split = Status column.
    Strip all whitespace from the value before returning.
"""

from __future__ import annotations

import re
import logging

log = logging.getLogger(__name__)


def parse_securexl_status(raw: str) -> str:
    """
    Parse fwaccel stat output and return the acceleration status string.

    Returns one of:
        "enabled"   — SecureXL is active
        "disabled"  — SecureXL is present but disabled
        "n/a"       — fwaccel not available or output unrecognised

    Tries Format 1 (Accelerator Status line) first, then Format 2 (KPPAK table).
    """
    if not raw.strip():
        return "n/a"

    # Format 1: "Accelerator Status : enabled"
    for line in raw.splitlines():
        if line.strip().startswith('Accelerator Status'):
            parts = line.split(':', 1)
            if len(parts) == 2:
                status = parts[1].strip().lower()
                log.debug("SecureXL (fmt1): %r", status)
                return status

    # Format 2: pipe-delimited KPPAK table
    # |Id|Name|Status|Interfaces|Features|
    # | 0|KPPAK|enabled|...|...|
    for line in raw.splitlines():
        if 'KPPAK' in line and '|' in line:
            parts = [p.strip() for p in line.split('|')]
            # parts: ['', id, name, status, interfaces, features, '']
            # Status is at index 3 after pipe-split
            if len(parts) >= 4:
                status = parts[3].strip().lower()
                if status in ('enabled', 'disabled'):
                    log.debug("SecureXL (fmt2/KPPAK): %r", status)
                    return status

    log.debug("SecureXL: status not found in output (%d chars)", len(raw))
    return "n/a"
