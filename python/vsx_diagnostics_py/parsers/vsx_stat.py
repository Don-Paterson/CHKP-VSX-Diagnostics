"""
parsers/vsx_stat.py
Pure functions that parse raw output from vsx stat.

parse_vsx_stat_v(raw)   -> VSXOverview
parse_vsx_stat_l(raw)   -> List[VSIDInfo]

No SSH calls, no side effects.  Takes raw strings, returns data objects.
All field names map directly to the bash variable names in v18.
"""

from __future__ import annotations

import re
import logging
from typing import List

from models.data import VSIDInfo, VSXOverview

log = logging.getLogger(__name__)


def parse_vsx_stat_v(raw: str) -> VSXOverview:
    """
    Parse output of: vsx stat -v

    Extracts:
      - Total connections [current / limit]
      - Number of Virtual Systems allowed by license

    Example lines we're targeting:
      Total connections [current / limit]:  1234 / 999999
      Number of Virtual Systems allowed by license: 10
    """
    overview = VSXOverview(raw_output=raw)

    # Total connections [current / limit]:  1234 / 999999
    m = re.search(
        r'Total connections\s*\[current\s*/\s*limit\]\s*:\s*(\d+)\s*/\s*(\d+)',
        raw, re.IGNORECASE
    )
    if m:
        overview.total_conn_current = int(m.group(1))
        overview.total_conn_limit   = int(m.group(2))
        log.debug("vsx stat -v: total_conn=%d limit=%d",
                  overview.total_conn_current, overview.total_conn_limit)

    # Number of Virtual Systems allowed by license: 10
    m = re.search(
        r'Number of Virtual Systems allowed by license\s*:\s*(\d+)',
        raw, re.IGNORECASE
    )
    if m:
        overview.vs_license_count = int(m.group(1))
        log.debug("vsx stat -v: license allows %d VS", overview.vs_license_count)

    return overview


def parse_vsx_stat_l(raw: str) -> List[VSIDInfo]:
    """
    Parse output of: vsx stat -l

    The output is a series of records, each delimited by a blank line,
    in the form:

        VSID: 0
        Type:  VSX Gateway
        Name:  A-VSX-GW
        Security Policy:  Standard
        Connections number: 42
        Connections peak: 100
        Connections limit: 999999

    Returns a list of VSIDInfo objects, one per record.
    Order matches the order in vsx stat -l output (typically ascending VSID).

    Maps to v18 awk block:
        /^VSID:/  { vsid=$2 ... }
        /^Connections limit:/ { ...; print vsid "|" vtype "|" ... }
    """
    vsids: List[VSIDInfo] = []

    # State for current record being built
    cur_vsid: int | None = None
    cur_type  = ""
    cur_name  = ""
    cur_policy = ""
    cur_conn   = 0
    cur_peak   = 0
    cur_limit  = 0

    def _flush():
        nonlocal cur_vsid
        if cur_vsid is not None:
            vsids.append(VSIDInfo(
                vsid        = cur_vsid,
                vtype       = cur_type.strip(),
                name        = cur_name.strip(),
                policy      = cur_policy.strip(),
                conn_current= cur_conn,
                conn_peak   = cur_peak,
                conn_limit  = cur_limit,
            ))
            log.debug("Discovered VSID %d  type=%r  name=%r", cur_vsid, cur_type, cur_name)
        cur_vsid = None

    for line in raw.splitlines():
        stripped = line.strip()

        if stripped.startswith("VSID:"):
            # New record starts - flush the previous one first
            _flush()
            try:
                cur_vsid  = int(stripped.split(":", 1)[1].strip())
                cur_type  = ""
                cur_name  = ""
                cur_policy = ""
                cur_conn  = 0
                cur_peak  = 0
                cur_limit = 0
            except (ValueError, IndexError):
                log.warning("Could not parse VSID line: %r", line)
                cur_vsid = None

        elif stripped.startswith("Type:") and cur_vsid is not None:
            cur_type = stripped.split(":", 1)[1].strip()

        elif stripped.startswith("Name:") and cur_vsid is not None:
            cur_name = stripped.split(":", 1)[1].strip()

        elif stripped.startswith("Security Policy:") and cur_vsid is not None:
            cur_policy = stripped.split(":", 1)[1].strip()

        elif re.match(r"Connections number\s*:", stripped, re.IGNORECASE) and cur_vsid is not None:
            try:
                cur_conn = int(stripped.rsplit(":", 1)[1].strip())
            except (ValueError, IndexError):
                cur_conn = 0

        elif re.match(r"Connections peak\s*:", stripped, re.IGNORECASE) and cur_vsid is not None:
            try:
                cur_peak = int(stripped.rsplit(":", 1)[1].strip())
            except (ValueError, IndexError):
                cur_peak = 0

        elif re.match(r"Connections limit\s*:", stripped, re.IGNORECASE) and cur_vsid is not None:
            try:
                cur_limit = int(stripped.rsplit(":", 1)[1].strip())
            except (ValueError, IndexError):
                cur_limit = 0
            # In v18 the limit line triggers the output - but we flush on
            # the next VSID: line (or at end), so no special action needed here.

    # Flush the final record
    _flush()

    if not vsids:
        log.error("parse_vsx_stat_l: no VSIDs found in output (%d chars)", len(raw))

    return vsids
