"""
collectors/per_vsid.py
Collects per-VSID diagnostic data and CoreXL/affinity from VS0.

collect_corexl(session)                    -> VSIDDiag (vsid=0, partial)
collect_per_vsid(session, vsid_info)       -> VSIDDiag
collect_all_vsids(session, vsids)          -> Dict[int, VSIDDiag]

Each per-VSID collection runs in a fresh exec_command channel via
session.run_in_vs(vsid, cmd) — the Python equivalent of v18's
run_in_vs() bash subshell.  vsenv's exec() kills that channel only;
the main interactive shell is completely unaffected.

Commands run per VSID (mirroring v18 collect_vs_diag()):
    enabled_blades              — software blades (normalised for R82)
    mpstat 1 1                  — CPU (1-second sample)
    ip route / ip route default — routing table + default gw
    ip addr                     — interface addresses
    ip -s link                  — interface stats (errors/drops)
    fwaccel stat                — SecureXL status
    fwaccel stats -s            — SecureXL template stats
    fw tab -t connections -s    — connection table summary
    fw tab -t fwx_alloc -s      — NAT table (firewall VSIDs only)
    brctl show / bridge link    — bridge info (switch VSIDs only)
    ip link show | grep wrp     — WARP interfaces (switch VSIDs only)
    free -m                     — memory

CoreXL (VS0 context, no vsenv):
    fw ctl multik stat          — CoreXL instance count
    fw ctl affinity -l          — CPU affinity (deduplicated)

R82 blade normalisation (v18 lines 672-677):
    "Virtual Switch context does not support software blades..." -> "n/a (vsw-ctx)"
    "Virtual Router context does not support software blades..." -> "n/a (vr-ctx)"
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from models.data import SecureXLStatus, VSIDDiag, VSIDInfo
from parsers.affinity import parse_affinity, parse_corexl_instances
from parsers.iface_errors import parse_iface_errors
from parsers.securexl import parse_securexl_status
from transport.ssh import ExpertSession

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blade label normalisation (R82)
# ---------------------------------------------------------------------------

def _normalise_blades(raw: str, vtype: str) -> str:
    """
    Normalise enabled_blades output for R82 verbose context messages.
    Maps long error strings to short labels matching v18 lines 672-677.
    """
    if vtype == "Virtual Switch":
        return "n/a (vsw)"

    s = raw.strip()
    if not s:
        return "n/a"

    if s.startswith("Virtual Switch context does not support"):
        return "n/a (vsw-ctx)"
    if s.startswith("Virtual Router context does not support"):
        return "n/a (vr-ctx)"
    if "not available" in s.lower() or "not found" in s.lower():
        return "n/a"

    return s


# ---------------------------------------------------------------------------
# Memory parsing helper
# ---------------------------------------------------------------------------

def _parse_free(raw: str) -> tuple:
    """
    Parse 'free -m' output.
    Returns (used_mb, total_mb, swap_used_mb, pct_str).
    """
    used_mb = total_mb = swap_used_mb = 0
    pct_str = "n/a"

    for line in raw.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            try:
                total_mb = int(parts[1])
                used_mb  = int(parts[2])
                if total_mb > 0:
                    pct_str = f"{round((used_mb / total_mb) * 100)}%"
            except (IndexError, ValueError):
                pass
        elif line.startswith("Swap:"):
            parts = line.split()
            try:
                swap_used_mb = int(parts[2])
            except (IndexError, ValueError):
                pass

    return used_mb, total_mb, swap_used_mb, pct_str


# ---------------------------------------------------------------------------
# CPU idle parsing helper
# ---------------------------------------------------------------------------

def _parse_cpu_idle(raw: str) -> Optional[float]:
    """
    Extract CPU idle % from mpstat 1 1 output.
    Looks for the 'Average:' line, last field.
    Returns None if not parseable.
    """
    for line in raw.splitlines():
        if line.startswith("Average:"):
            parts = line.split()
            try:
                return float(parts[-1])
            except (ValueError, IndexError):
                pass
    return None


# ---------------------------------------------------------------------------
# Connection count helper
# ---------------------------------------------------------------------------

def _parse_conn_count(raw: str) -> int:
    """
    Sum the 4th column (current entries) from fw tab -t connections -s output.
    Mirrors v18: awk 'NR>1 {sum+=$4} END {print sum+0}'
    """
    total = 0
    for i, line in enumerate(raw.splitlines()):
        if i == 0:
            continue  # skip header
        parts = line.split()
        if len(parts) >= 4:
            try:
                total += int(parts[3])
            except ValueError:
                pass
    return total


# ---------------------------------------------------------------------------
# CoreXL collector (VS0, no vsenv)
# ---------------------------------------------------------------------------

def collect_corexl(session: ExpertSession) -> VSIDDiag:
    """
    Collect CoreXL instance count and affinity from VS0 context.
    Returns a VSIDDiag(vsid=0) with corexl_* and affinity_raw populated.
    The rest of VS0 diagnostics are collected by collect_per_vsid(vsid=0).
    """
    diag = VSIDDiag(vsid=0, vtype="VSX Gateway", vname="VS0")

    log.info("CoreXL: collecting fw ctl multik stat ...")
    raw_multik = session.run("fw ctl multik stat 2>&1")
    diag.corexl_stat      = raw_multik
    diag.corexl_instances = parse_corexl_instances(raw_multik)
    log.info("CoreXL: %d active instances", diag.corexl_instances)

    log.info("CoreXL: collecting fw ctl affinity -l ...")
    raw_affinity      = session.run("fw ctl affinity -l 2>&1")
    diag.affinity_raw = parse_affinity(raw_affinity)

    return diag


# ---------------------------------------------------------------------------
# Per-VSID collector
# ---------------------------------------------------------------------------

def collect_per_vsid(
    session: ExpertSession,
    vsid_info: VSIDInfo,
) -> VSIDDiag:
    """
    Collect all per-VSID diagnostic data in a fresh vsenv subshell.

    Each session.run_in_vs() call opens a new exec_command channel,
    sources CP profiles, runs vsenv N, then runs the command.
    vsenv's exec() kills that channel only — the main shell is safe.
    """
    vsid  = vsid_info.vsid
    vtype = vsid_info.vtype
    vname = vsid_info.name
    is_sw = vsid_info.is_switch

    log.info("Per-VSID: collecting VSID %d (%s - %s) ...", vsid, vname, vtype)

    diag = VSIDDiag(vsid=vsid, vtype=vtype, vname=vname)

    def vs(cmd: str, timeout: int = 60) -> str:
        """Shorthand: run cmd in vsenv context for this VSID."""
        return session.run_in_vs(vsid, cmd, timeout=timeout)

    # ----------------------------------------------------------------
    # Enabled blades
    # ----------------------------------------------------------------
    if is_sw:
        diag.enabled_blades = "n/a (vsw)"
    else:
        raw_blades = vs("enabled_blades 2>/dev/null || echo '[not available]'")
        diag.enabled_blades = _normalise_blades(raw_blades, vtype)
    log.debug("VSID %d blades: %r", vsid, diag.enabled_blades)

    # ----------------------------------------------------------------
    # CPU (1-second mpstat sample)
    # ----------------------------------------------------------------
    raw_cpu = vs(
        "if command -v mpstat >/dev/null 2>&1; then "
        "  mpstat 1 1 2>&1; "
        "else "
        "  top -bn1 2>/dev/null | grep '^%Cpu' || echo '[unavailable]'; "
        "fi",
        timeout=15,
    )
    diag.cpu_raw       = raw_cpu
    diag.cpu_idle_pct  = _parse_cpu_idle(raw_cpu)
    log.debug("VSID %d cpu_idle=%s", vsid, diag.cpu_idle_pct)

    # ----------------------------------------------------------------
    # Memory
    # ----------------------------------------------------------------
    raw_mem = vs("free -m 2>/dev/null")
    diag.mem_used_mb, diag.mem_total_mb, diag.swap_used_mb, diag.mem_used_pct = \
        _parse_free(raw_mem)
    log.debug(
        "VSID %d mem: %s used=%d/%d MB swap=%d MB",
        vsid, diag.mem_used_pct, diag.mem_used_mb, diag.mem_total_mb, diag.swap_used_mb,
    )

    # ----------------------------------------------------------------
    # Routing (skip for Virtual Switch)
    # ----------------------------------------------------------------
    if not is_sw:
        diag.route_table = vs("ip route 2>&1")
        default_lines = [
            l for l in diag.route_table.splitlines()
            if l.startswith("default")
        ]
        diag.default_gw = default_lines[0] if default_lines else ""

    # ----------------------------------------------------------------
    # Interface addresses
    # ----------------------------------------------------------------
    diag.ip_addr_raw = vs("ip addr 2>&1")

    # ----------------------------------------------------------------
    # Interface errors  (ip -s link)
    # ----------------------------------------------------------------
    raw_link = vs("ip -s link 2>&1")
    diag.iface_errors = parse_iface_errors(raw_link, vsid=vsid)

    # ----------------------------------------------------------------
    # SecureXL (skip for Virtual Switch)
    # ----------------------------------------------------------------
    if not is_sw:
        raw_fwaccel = vs(
            "if command -v fwaccel >/dev/null 2>&1; then "
            "  fwaccel stat 2>&1; "
            "else "
            "  echo '[fwaccel not available]'; "
            "fi"
        )
        raw_fwaccel_s = vs("fwaccel stats -s 2>&1 || echo '[unavailable]'")
        diag.securexl = SecureXLStatus(
            vsid      = vsid,
            status    = parse_securexl_status(raw_fwaccel),
            raw_stat  = raw_fwaccel,
            raw_stats_s = raw_fwaccel_s,
        )
        log.debug("VSID %d SecureXL: %s", vsid, diag.securexl.status)

    # ----------------------------------------------------------------
    # Connection table
    # ----------------------------------------------------------------
    raw_conn = vs("fw tab -t connections -s 2>&1 || echo '[unavailable]'")
    diag.conn_table_summary = raw_conn
    diag.conn_current       = _parse_conn_count(raw_conn)

    if not is_sw:
        diag.nat_table_summary = vs(
            "fw tab -t fwx_alloc -s 2>&1 || echo '[unavailable]'"
        )

    # ----------------------------------------------------------------
    # Virtual Switch specifics
    # ----------------------------------------------------------------
    if is_sw:
        diag.bridge_raw = vs(
            "if command -v brctl >/dev/null 2>&1; then "
            "  brctl show 2>&1; "
            "else "
            "  bridge link 2>&1; "
            "fi"
            " || echo '[unavailable]'"
        )
        diag.warp_ifaces_raw = vs(
            "ip link show 2>/dev/null | grep -i wrp || echo '[none found]'"
        )

    log.info(
        "VSID %d: blades=%r  mem=%s  conn=%d  sxl=%s  iface_errors=%d",
        vsid,
        diag.enabled_blades,
        diag.mem_used_pct,
        diag.conn_current,
        diag.securexl.status if not is_sw else "n/a",
        len(diag.iface_errors),
    )
    return diag


# ---------------------------------------------------------------------------
# Collect all VSIDs
# ---------------------------------------------------------------------------

def collect_all_vsids(
    session: ExpertSession,
    vsids: List[VSIDInfo],
) -> Dict[int, VSIDDiag]:
    """
    Collect CoreXL (VS0 context) then per-VSID diagnostics for all VSIDs.

    Returns dict keyed by VSID integer.
    CoreXL data is merged into the VS0 VSIDDiag entry.
    """
    results: Dict[int, VSIDDiag] = {}

    # CoreXL first (VS0, no vsenv needed)
    corexl_diag = collect_corexl(session)

    for vsid_info in vsids:
        diag = collect_per_vsid(session, vsid_info)

        # Merge CoreXL data into VS0 entry
        if vsid_info.vsid == 0:
            diag.corexl_stat      = corexl_diag.corexl_stat
            diag.corexl_instances = corexl_diag.corexl_instances
            diag.affinity_raw     = corexl_diag.affinity_raw

        results[vsid_info.vsid] = diag

    log.info(
        "Per-VSID collection complete: %d VSIDs collected",
        len(results),
    )
    return results
