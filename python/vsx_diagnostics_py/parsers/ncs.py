"""
collectors/ncs.py
Collects NCS (Network Configuration Script) data for all VSIDs via
vsx showncs, using the file-redirect workaround for the stdout
suppression bug in subshell capture.

collect_ncs(session, vsids) -> Tuple[bool, Dict[int, NCSData]]

Returns:
    showncs_available : bool          False if showncs produced no output at all
    ncs_by_vsid       : Dict[int, NCSData]   Keyed by VSID; VSID 0 excluded

Critical lesson from v18 (line 308):
    vsx showncs suppresses stdout when captured via $() subshell or
    Paramiko exec_command pipe.  The workaround is to redirect stdout
    to a remote temp file, then cat it back.

    Bash equivalent:
        vsx showncs 1 > /tmp/ncs_probe.txt 2>/dev/null
        cat /tmp/ncs_probe.txt

    Python equivalent (via ExpertSession):
        session.run_to_remote_file("vsx showncs 1", "/tmp/ncs_probe.txt")
        raw = session.read_remote_file("/tmp/ncs_probe.txt")
        session.remove_remote_file("/tmp/ncs_probe.txt")

Additional R82 requirement (from v18 line 308 comment):
    vsx showncs on R82 requires 'vsx fetch' to have been run first.
    If showncs returns empty, the caller should retry with --fetch (-f).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from models.data import NCSData, VSIDInfo
from parsers.ncs_data import parse_ncs
from transport.ssh import ExpertSession

log = logging.getLogger(__name__)

# Remote temp file prefix for showncs output
_REMOTE_TMP_PREFIX = "/tmp/vsx_ncs_py"


def collect_ncs(
    session: ExpertSession,
    vsids: List[VSIDInfo],
) -> Tuple[bool, Dict[int, NCSData]]:
    """
    Collect vsx showncs output for all non-VS0 VSIDs.

    Parameters
    ----------
    session : active ExpertSession (expert mode, VS0)
    vsids   : list of VSIDInfo from vsid_discovery — used for iteration

    Returns
    -------
    showncs_available : bool
        True  — showncs produced output; ncs_by_vsid is populated
        False — showncs produced no output (needs vsx fetch, or R82 quirk)

    ncs_by_vsid : Dict[int, NCSData]
        Keyed by VSID integer.  VS0 is excluded (showncs is not run for VS0).
        VSIDs where showncs returned empty have NCSData(available=False).
    """
    ncs_by_vsid: Dict[int, NCSData] = {}

    # ----------------------------------------------------------------
    # Step 1 — Probe with first non-VS0 VSID to test availability
    # ----------------------------------------------------------------
    non_zero_vsids = [v.vsid for v in vsids if v.vsid != 0]

    if not non_zero_vsids:
        log.warning("NCS: no non-VS0 VSIDs to probe — skipping showncs")
        return False, ncs_by_vsid

    probe_vsid = non_zero_vsids[0]
    probe_path = f"{_REMOTE_TMP_PREFIX}_probe.txt"

    log.info("NCS: probing vsx showncs availability (VSID %d) ...", probe_vsid)
    ok = session.run_to_remote_file(
        f"vsx showncs {probe_vsid}",
        probe_path,
    )

    if not ok:
        log.warning(
            "NCS: 'vsx showncs %d' returned no output.\n"
            "     Topology map and WARP diagram will be unavailable.\n"
            "     On R82, run with --fetch (-f) first to populate NCS data.\n"
            "     To investigate manually: run 'vsx showncs %d' from VS0 expert mode.",
            probe_vsid, probe_vsid,
        )
        session.remove_remote_file(probe_path)
        return False, ncs_by_vsid

    # Read and parse the probe result — don't waste it
    probe_raw = session.read_remote_file(probe_path)
    session.remove_remote_file(probe_path)
    ncs_by_vsid[probe_vsid] = parse_ncs(probe_raw, vsid=probe_vsid)
    log.info("NCS: showncs available — collecting all VSIDs ...")

    # ----------------------------------------------------------------
    # Step 2 — Collect remaining VSIDs
    # ----------------------------------------------------------------
    for vsid in non_zero_vsids:
        if vsid == probe_vsid:
            continue  # already collected above

        remote_path = f"{_REMOTE_TMP_PREFIX}_{vsid}.txt"
        log.info("NCS: collecting VSID %d ...", vsid)

        wrote = session.run_to_remote_file(
            f"vsx showncs {vsid}",
            remote_path,
        )

        if not wrote:
            log.warning("NCS: vsx showncs %d returned no output", vsid)
            ncs_by_vsid[vsid] = NCSData(vsid=vsid, available=False)
            continue

        raw = session.read_remote_file(remote_path)
        session.remove_remote_file(remote_path)
        ncs_by_vsid[vsid] = parse_ncs(raw, vsid=vsid)

    # ----------------------------------------------------------------
    # Step 3 — Summary log
    # ----------------------------------------------------------------
    available_count = sum(1 for n in ncs_by_vsid.values() if n.available)
    log.info(
        "NCS: collected %d/%d VSIDs with data",
        available_count, len(non_zero_vsids),
    )

    return True, ncs_by_vsid
