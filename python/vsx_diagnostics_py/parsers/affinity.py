"""
parsers/affinity.py
Pure functions for parsing fw ctl affinity -l output.

parse_affinity(raw)         -> str   deduplicated affinity lines
parse_corexl_instances(raw) -> int   count of active CoreXL instances

R82 lesson from v18:
    fw ctl affinity -l repeats each entry once per CoreXL instance.
    On a 4-instance system, every line appears 4 times.
    v18 fixes this with:  fw ctl affinity -l 2>&1 | sort -u
    We replicate sort -u by deduplicating while preserving first-seen order.

fw ctl multik stat output (for CoreXL instance count):
    ID | Active | CPU IDs
    ---+--------+--------
     0 | Yes    | 0
     1 | Yes    | 1
     2 | Yes    | 2
     3 | Yes    | 3

COREXL_INSTANCES = count of lines containing '| Yes'
"""

from __future__ import annotations

import logging
from typing import List

log = logging.getLogger(__name__)


def parse_affinity(raw: str) -> str:
    """
    Deduplicate fw ctl affinity -l output.
    Returns the unique lines joined by newline, order preserved.
    Empty/whitespace lines are dropped.
    """
    seen: set[str] = set()
    unique: List[str] = []

    for line in raw.splitlines():
        stripped = line.rstrip()
        if not stripped:
            continue
        if stripped not in seen:
            seen.add(stripped)
            unique.append(stripped)

    deduped = len(raw.splitlines()) - len(unique)
    if deduped > 0:
        log.debug("affinity: removed %d duplicate lines (R82 per-instance repeat)", deduped)

    return '\n'.join(unique)


def parse_corexl_instances(raw: str) -> int:
    """
    Count active CoreXL instances from fw ctl multik stat output.
    Counts lines containing '| Yes' — mirrors v18 grep -c '| Yes'.
    Returns 0 if output is empty or command unavailable.
    """
    count = sum(1 for line in raw.splitlines() if '| Yes' in line)
    log.debug("CoreXL instances: %d", count)
    return count
