"""
delta/serialiser.py
Converts a HealthSummary into a RunSnapshot, writes it to disk as JSON,
and finds/loads the most recent prior snapshot for comparison.

Public API
----------
snapshot_from_summary(summary)          -> RunSnapshot
save_snapshot(snapshot, output_dir, stem) -> str   (path written)
load_prev_snapshot(output_dir, current_run_id) -> Optional[RunSnapshot]

File naming
-----------
Snapshots are written alongside the .log and .html files:
    <output_dir>/vsx_diag_<hostname>_<timestamp>.snapshot.json

load_prev_snapshot() globs for *.snapshot.json in output_dir, parses the
run_id field from each file, and returns the one with the latest run_id
that is strictly earlier than current_run_id.  This is safe even if the
directory contains snapshots from multiple hostnames.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from typing import Optional

from models.data import HealthSummary
from models.snapshot import RunSnapshot, VSIDSnapshot

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def snapshot_from_summary(summary: HealthSummary) -> RunSnapshot:
    """
    Build a RunSnapshot from a fully populated HealthSummary.
    No I/O — pure data projection.
    """
    snap = RunSnapshot(
        run_id               = summary.run_timestamp,
        tool_version         = summary.script_version,
        collected_from_ip    = summary.topology.connected_ip,
        collected_from_host  = summary.topology.active_member,
        cp_version_short     = summary.platform.cp_version_short,
        jhf_take             = summary.platform.jhf_take,
        member_states        = dict(summary.cluster_health.member_states),
        sync_status          = summary.cluster_health.sync_status,
        sync_lost_updates    = summary.cluster_health.sync_lost_updates,
        failover_count       = summary.cluster_health.failover_count,
        disk_root_pct        = _pct_int(summary.platform.disk_root_pct),
        disk_log_pct         = _pct_int(summary.platform.disk_log_pct),
        total_conn_current   = summary.vsx_overview.total_conn_current,
        total_conn_limit     = summary.vsx_overview.total_conn_limit,
        hcp_ran_ok           = summary.hcp.ran_ok,
    )

    # CPU / swap from VS0
    diag0 = summary.vsid_diags.get(0)
    if diag0:
        snap.cpu_idle_pct = diag0.cpu_idle_pct
        snap.swap_used_mb = diag0.swap_used_mb

    # PNOTE issues (non-OK entries only)
    snap.pnote_issues = [
        {"name": p.name, "status": p.status}
        for p in summary.cluster_health.pnote_issues
    ]

    # HCP results
    snap.hcp_results = [
        {"vsid": r.vsid, "test_name": r.test_name, "status": r.status}
        for r in summary.hcp.results
    ]

    # Per-VSID snapshots
    for vsid_info in summary.vsids:
        vsid_snap = VSIDSnapshot(
            vsid            = vsid_info.vsid,
            name            = vsid_info.name,
            vtype           = vsid_info.vtype,
            conn_current    = vsid_info.conn_current,
            conn_limit      = vsid_info.conn_limit,
        )
        diag = summary.vsid_diags.get(vsid_info.vsid)
        if diag:
            vsid_snap.securexl_status = diag.securexl.status
            vsid_snap.iface_errors = [
                {
                    "dev":       err.dev,
                    "direction": err.direction,
                    "errors":    err.errors,
                    "drops":     err.drops,
                }
                for err in diag.iface_errors
            ]
        # JSON keys must be strings
        snap.vsids[str(vsid_info.vsid)] = vsid_snap

    return snap


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_snapshot(snapshot: RunSnapshot, output_dir: str, stem: str) -> str:
    """
    Write snapshot to <output_dir>/<stem>.snapshot.json.
    Returns the path written.
    """
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{stem}.snapshot.json")

    data = _snapshot_to_dict(snapshot)
    try:
        with open(path, "w", encoding="ascii") as f:
            json.dump(data, f, indent=2)
        log.info("Snapshot saved: %s", path)
    except OSError as exc:
        log.warning("Could not save snapshot: %s", exc)
        return ""

    return path


def load_prev_snapshot(output_dir: str, current_run_id: str) -> Optional[RunSnapshot]:
    """
    Find and load the most recent snapshot in output_dir whose run_id is
    strictly earlier than current_run_id.

    Returns None if no suitable snapshot is found or on any parse error.
    """
    pattern = os.path.join(output_dir, "*.snapshot.json")
    candidates = []

    for path in glob.glob(pattern):
        try:
            with open(path, "r", encoding="ascii") as f:
                data = json.load(f)
            run_id = data.get("run_id", "")
            # Lexicographic comparison works for ISO timestamps
            if run_id and run_id < current_run_id:
                candidates.append((run_id, path, data))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            log.debug("Skipping malformed snapshot %s: %s", path, exc)

    if not candidates:
        log.info("No previous snapshot found in %s", output_dir)
        return None

    # Most recent candidate
    candidates.sort(key=lambda t: t[0], reverse=True)
    _, best_path, best_data = candidates[0]

    try:
        snap = _snapshot_from_dict(best_data)
        log.info("Loaded previous snapshot: %s (run_id=%s)", best_path, snap.run_id)
        return snap
    except Exception as exc:
        log.warning("Could not deserialise snapshot %s: %s", best_path, exc)
        return None


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _snapshot_to_dict(snap: RunSnapshot) -> dict:
    """Convert RunSnapshot to a plain dict suitable for json.dump."""
    d = {
        "run_id":               snap.run_id,
        "tool_version":         snap.tool_version,
        "collected_from_ip":    snap.collected_from_ip,
        "collected_from_host":  snap.collected_from_host,
        "cp_version_short":     snap.cp_version_short,
        "jhf_take":             snap.jhf_take,
        "member_states":        snap.member_states,
        "sync_status":          snap.sync_status,
        "sync_lost_updates":    snap.sync_lost_updates,
        "failover_count":       snap.failover_count,
        "disk_root_pct":        snap.disk_root_pct,
        "disk_log_pct":         snap.disk_log_pct,
        "cpu_idle_pct":         snap.cpu_idle_pct,
        "swap_used_mb":         snap.swap_used_mb,
        "total_conn_current":   snap.total_conn_current,
        "total_conn_limit":     snap.total_conn_limit,
        "pnote_issues":         snap.pnote_issues,
        "hcp_ran_ok":           snap.hcp_ran_ok,
        "hcp_results":          snap.hcp_results,
        "vsids": {
            vsid_str: {
                "vsid":             vs.vsid,
                "name":             vs.name,
                "vtype":            vs.vtype,
                "conn_current":     vs.conn_current,
                "conn_limit":       vs.conn_limit,
                "securexl_status":  vs.securexl_status,
                "iface_errors":     vs.iface_errors,
            }
            for vsid_str, vs in snap.vsids.items()
        },
    }
    return d


def _snapshot_from_dict(d: dict) -> RunSnapshot:
    """Reconstruct a RunSnapshot from a parsed JSON dict."""
    snap = RunSnapshot(
        run_id               = d.get("run_id", ""),
        tool_version         = d.get("tool_version", ""),
        collected_from_ip    = d.get("collected_from_ip", ""),
        collected_from_host  = d.get("collected_from_host", ""),
        cp_version_short     = d.get("cp_version_short", ""),
        jhf_take             = d.get("jhf_take", ""),
        member_states        = d.get("member_states", {}),
        sync_status          = d.get("sync_status", ""),
        sync_lost_updates    = int(d.get("sync_lost_updates", 0)),
        failover_count       = int(d.get("failover_count", 0)),
        disk_root_pct        = int(d.get("disk_root_pct", 0)),
        disk_log_pct         = int(d.get("disk_log_pct", 0)),
        cpu_idle_pct         = d.get("cpu_idle_pct"),   # may be None
        swap_used_mb         = int(d.get("swap_used_mb", 0)),
        total_conn_current   = int(d.get("total_conn_current", 0)),
        total_conn_limit     = int(d.get("total_conn_limit", 0)),
        pnote_issues         = d.get("pnote_issues", []),
        hcp_ran_ok           = bool(d.get("hcp_ran_ok", False)),
        hcp_results          = d.get("hcp_results", []),
    )

    for vsid_str, vd in d.get("vsids", {}).items():
        snap.vsids[vsid_str] = VSIDSnapshot(
            vsid            = int(vd.get("vsid", vsid_str)),
            name            = vd.get("name", ""),
            vtype           = vd.get("vtype", ""),
            conn_current    = int(vd.get("conn_current", 0)),
            conn_limit      = int(vd.get("conn_limit", 0)),
            securexl_status = vd.get("securexl_status", ""),
            iface_errors    = vd.get("iface_errors", []),
        )

    return snap


def _pct_int(pct_str: str) -> int:
    """Convert '34%' -> 34.  Returns 0 if not parseable."""
    try:
        return int(str(pct_str).strip().rstrip("%"))
    except (ValueError, AttributeError):
        return 0
