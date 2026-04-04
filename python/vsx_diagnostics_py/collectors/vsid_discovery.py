"""
collectors/vsid_discovery.py
Collects VSX overview and VSID list from the active cluster member.

collect_vsid_discovery(session) -> Tuple[VSXOverview, List[VSIDInfo]]

This is the first collector that runs after preflight.  It establishes
the full list of VSIDs that every subsequent collector will iterate over.
Equivalent to the "VSX Overview" + "Virtual Device Discovery" sections of v18.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

from models.data import VSIDInfo, VSXOverview
from parsers.vsx_stat import parse_vsx_stat_v, parse_vsx_stat_l
from transport.ssh import ExpertSession

log = logging.getLogger(__name__)


def collect_vsid_discovery(
    session: ExpertSession,
) -> Tuple[VSXOverview, List[VSIDInfo]]:
    """
    Run vsx stat -v and vsx stat -l on the active member.
    Parse and return structured data.

    Raises RuntimeError if no VSIDs are discovered (indicates a
    fundamental problem with the gateway state or our connection).

    Both commands are run via ExpertSession.run() (the interactive shell)
    because they run in VS0 context and do not involve vsenv.
    """
    log.info("Collecting VSX overview (vsx stat -v) ...")
    raw_v = session.run("vsx stat -v 2>/dev/null")
    overview = parse_vsx_stat_v(raw_v)
    log.info(
        "VSX overview: total_conn=%d/%d  license_vs=%d",
        overview.total_conn_current,
        overview.total_conn_limit,
        overview.vs_license_count,
    )

    log.info("Discovering VSIDs (vsx stat -l) ...")
    raw_l = session.run("vsx stat -l 2>/dev/null")
    vsids = parse_vsx_stat_l(raw_l)

    if not vsids:
        raise RuntimeError(
            "No VSIDs discovered from 'vsx stat -l'. "
            "Check that the gateway is in VS0 expert context and VSX is configured."
        )

    gw_count  = sum(1 for v in vsids if v.is_firewall)
    sw_count  = sum(1 for v in vsids if v.is_switch)
    rtr_count = sum(1 for v in vsids if v.is_router)
    log.info(
        "Discovered %d VSIDs: %d firewall(s), %d switch(es), %d router(s)",
        len(vsids), gw_count, sw_count, rtr_count,
    )
    for v in vsids:
        log.debug(
            "  VSID %-3d  %-18s  %-22s  conn=%d/%d",
            v.vsid, v.vtype, v.name, v.conn_current, v.conn_limit,
        )

    return overview, vsids
