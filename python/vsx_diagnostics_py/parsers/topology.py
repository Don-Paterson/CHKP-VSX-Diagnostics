"""
collectors/topology.py
Collects cluster topology and runs preflight checks.

collect_preflight(session)  -> PlatformInfo (partial - hostname, FWDIR, CP version)
collect_topology(session, fwdir, active_member) -> ClusterTopology

These are the first two collectors that run after the SSH session is open,
mapping to v18's "Preflight Checks" and "Cluster Topology" sections.

Preflight checks performed:
  - id -u == 0  (expert / root)
  - $FWDIR is set
  - vsx command is available

collect_topology:
  - Reads $FWDIR/state/local/VSX/local.vsall via cat
  - Parses it with parsers.vsall.parse_vsall
  - Populates ClusterTopology including connected_ip and fwdir
"""

from __future__ import annotations

import logging

from models.data import ClusterTopology, PlatformInfo
from parsers.vsall import parse_vsall
from transport.ssh import ExpertSession

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

class PreflightError(Exception):
    """Raised when a hard preflight check fails."""


def collect_preflight(session: ExpertSession) -> PlatformInfo:
    """
    Run preflight checks and collect basic platform info.

    Returns a partially-populated PlatformInfo (hostname, fwdir, cp_version).
    The rest of PlatformInfo (JHF take, disk, uptime etc.) is filled in
    by collectors/platform.py later.

    Raises PreflightError if any hard check fails.
    """
    info = PlatformInfo()

    # --- Root / expert mode check ---
    log.info("Preflight: checking root ...")
    uid = session.run("id -u").strip()
    if uid != "0":
        raise PreflightError(
            f"Not running as root (id -u returned {uid!r}). "
            "Ensure SSH user has expert/root access."
        )
    log.info("Preflight: OK - running as root")

    # --- Hostname ---
    info.hostname = session.run("hostname").strip()
    log.info("Preflight: hostname=%s", info.hostname)

    # --- FWDIR ---
    log.info("Preflight: checking $FWDIR ...")
    fwdir = session.run("echo $FWDIR").strip()
    if not fwdir:
        raise PreflightError(
            "$FWDIR is not set. Ensure CP.sh profile is sourced "
            "and the gateway has Check Point software installed."
        )
    log.info("Preflight: FWDIR=%s", fwdir)

    # Store fwdir on PlatformInfo so caller can pass it to collect_topology
    # We reuse the hostname field's slot; fwdir goes into topology.fwdir.
    # Stash it temporarily as an attribute for the orchestrator to pick up.
    info._fwdir = fwdir  # type: ignore[attr-defined]  # temporary carrier

    # --- vsx command availability ---
    log.info("Preflight: checking vsx command ...")
    which_vsx = session.run("command -v vsx 2>/dev/null").strip()
    if not which_vsx:
        raise PreflightError(
            "'vsx' command not found. "
            "Is this a VSX gateway? Is CP.sh sourced?"
        )
    log.info("Preflight: vsx found at %s", which_vsx)

    # --- CP version (quick, used in summary header) ---
    log.info("Preflight: collecting CP version ...")
    fw_ver_raw = session.run("fw ver 2>/dev/null | head -1").strip()
    info.cp_version = fw_ver_raw

    # Parse short version string e.g. "R82" from
    # "This is Check Point's software version R82 - Build 123"
    import re
    m = re.search(r'\b(R\d+[\d.]*)\b', fw_ver_raw)
    if m:
        info.cp_version_short = m.group(1)
    m2 = re.search(r'\bBuild\s+(\d+)', fw_ver_raw)
    if m2:
        info.cp_build = m2.group(1)

    log.info(
        "Preflight: CP version=%s build=%s",
        info.cp_version_short or "?", info.cp_build or "?",
    )

    return info


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------

def collect_topology(
    session: ExpertSession,
    fwdir: str,
    active_member: str = "",
) -> ClusterTopology:
    """
    Read and parse $FWDIR/state/local/VSX/local.vsall.

    Parameters
    ----------
    session       : active ExpertSession
    fwdir         : $FWDIR path (from collect_preflight)
    active_member : hostname of the gateway we connected to

    Returns a ClusterTopology.  If the vsall file is missing or empty,
    returns a topology with an empty members list and logs a warning —
    this is non-fatal; subsequent collectors can still run.
    """
    vsall_path = f"{fwdir}/state/local/VSX/local.vsall"
    log.info("Topology: reading %s ...", vsall_path)

    raw = session.run(f"cat '{vsall_path}' 2>/dev/null").strip()

    if not raw:
        log.warning(
            "Topology: %s not found or empty. "
            "Cluster member info will be unavailable. "
            "Try running with --fetch (-f) to populate vsall first.",
            vsall_path,
        )
        topology = ClusterTopology(
            active_member=active_member,
            fwdir=fwdir,
            connected_ip=session.connected_ip,
        )
        return topology

    log.debug("Topology: vsall is %d bytes", len(raw))
    topology = parse_vsall(raw, active_member=active_member)
    topology.fwdir        = fwdir
    topology.connected_ip = session.connected_ip

    # Log member table for diagnostics
    for m in topology.members:
        marker = " <-- active" if m.name == active_member else ""
        log.info(
            "  Member: %-16s  mgmt=%-15s  sync=%s%s",
            m.name, m.mgmt_ip or "?", m.sync_ip or "?", marker,
        )

    return topology
