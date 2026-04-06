"""
renderers/export.py
Writes machine-readable exports alongside the HTML and log outputs.

render_export(summary, base_path, delta=None) -> (json_path, csv_path)

Two files are produced per run:

  <stem>.json  — Complete structured export, suitable for:
                   • Splunk HTTP Event Collector (JSON)
                   • Grafana JSON datasource
                   • Power BI JSON connector
                   • Python/PowerShell post-processing scripts

  <stem>.csv   — Flat tabular export with one row per VSID, suitable for:
                   • Power BI CSV import
                   • Grafana CSV datasource
                   • Excel pivot tables
                   • Splunk CSV lookup

JSON schema
-----------
{
  "schema_version": "1",
  "run": { run metadata },
  "platform": { version, JHF, disk, CPU, swap, load, uptime },
  "cluster": { mode, sync_status, sync_lost, failover_count, members: [...] },
  "vsids": [ { per-VSID fields } ],
  "connections": { total_current, total_limit, total_pct },
  "attention_items": [ { severity, category, message } ],
  "delta": { summary fields } | null,
  "member_comparison": { summary fields } | null,
  "hcp": { ran_ok, error_count, info_count, errors: [...] }
}

CSV columns (one row per VSID)
-------------------------------
run_id, collected_from, profile, cp_version, jhf_take,
cpu_idle_pct, swap_used_mb, disk_root_pct, disk_log_pct,
sync_status, failover_count,
vsid, vsid_name, vsid_type,
conn_current, conn_limit, conn_pct,
securexl_status,
iface_errors,
attention_count_critical, attention_count_warning,
delta_failover_increased, delta_sync_changed, delta_disk_root_delta, delta_disk_log_delta

All numeric fields are numbers (not strings) in JSON.
The CSV uses empty string for missing/N/A values.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from typing import Any, Dict, List, Optional

from models.data import HealthSummary
from models.snapshot import DeltaReport

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_export(
    summary: HealthSummary,
    output_dir: str,
    stem: str,
    delta: Optional[DeltaReport] = None,
) -> tuple:
    """
    Write JSON and CSV exports.

    Parameters
    ----------
    summary    : fully assessed HealthSummary
    output_dir : directory to write files into
    stem       : filename stem, e.g. "vsx_diag_A-VSX-01_20250406_143200"
    delta      : optional DeltaReport from this run's comparison

    Returns (json_path, csv_path).  Both paths are empty strings on failure.
    """
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f"{stem}.json")
    csv_path  = os.path.join(output_dir, f"{stem}.csv")

    data = _build_export(summary, delta)

    # ── JSON ──────────────────────────────────────────────────────────
    try:
        with open(json_path, "w", encoding="ascii", errors="replace") as f:
            json.dump(data, f, indent=2, default=str)
        log.info("JSON export written: %s (%.1f KB)",
                 json_path, os.path.getsize(json_path) / 1024)
        print(f"JSON export: {json_path}")
    except OSError as exc:
        log.warning("JSON export failed: %s", exc)
        json_path = ""

    # ── CSV ───────────────────────────────────────────────────────────
    try:
        rows = _build_csv_rows(summary, delta)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            if rows:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
        log.info("CSV export written: %s (%d rows)",
                 csv_path, len(rows))
        print(f"CSV export:  {csv_path}")
    except OSError as exc:
        log.warning("CSV export failed: %s", exc)
        csv_path = ""

    return json_path, csv_path


# ---------------------------------------------------------------------------
# JSON builder
# ---------------------------------------------------------------------------

def _build_export(
    s: HealthSummary,
    delta: Optional[DeltaReport],
) -> Dict[str, Any]:
    """Build the complete export dict."""

    # ── Run metadata ──────────────────────────────────────────────────
    run = {
        "run_id":           s.run_timestamp,
        "tool_version":     s.script_version,
        "collected_from":   s.topology.active_member or s.topology.connected_ip,
        "collected_from_ip": s.topology.connected_ip,
        "profile":          s.active_profile,
    }

    # ── Platform ──────────────────────────────────────────────────────
    diag0 = s.vsid_diags.get(0)
    platform = {
        "cp_version":       s.platform.cp_version_short,
        "cp_build":         s.platform.cp_build,
        "jhf_take":         _safe_int(s.platform.jhf_take),
        "kernel":           s.platform.kernel,
        "uptime":           s.platform.uptime_raw,
        "load_avg":         s.platform.load_avg,
        "disk_root_pct":    _pct_int(s.platform.disk_root_pct),
        "disk_log_pct":     _pct_int(s.platform.disk_log_pct),
        "cpu_idle_pct":     diag0.cpu_idle_pct if diag0 else None,
        "swap_used_mb":     diag0.swap_used_mb if diag0 else None,
        "corexl_instances": diag0.corexl_instances if diag0 else None,
    }

    # ── Cluster ───────────────────────────────────────────────────────
    ch = s.cluster_health
    cluster = {
        "mode":             ch.cluster_mode,
        "sync_status":      ch.sync_status,
        "sync_lost_updates": ch.sync_lost_updates,
        "failover_count":   ch.failover_count,
        "failover_transition": ch.failover_transition,
        "failover_time":    ch.failover_time,
        "members": [
            {
                "name":  m.name,
                "ip":    m.mgmt_ip,
                "state": ch.member_states.get(m.name, ""),
            }
            for m in s.topology.members
        ],
        "pnote_issues": [
            {"name": p.name, "status": p.status}
            for p in ch.pnote_issues
        ],
    }

    # ── Connections ───────────────────────────────────────────────────
    ov = s.vsx_overview
    conn_pct = None
    if ov.total_conn_limit > 0:
        conn_pct = round((ov.total_conn_current / ov.total_conn_limit) * 100, 1)
    connections = {
        "total_current": ov.total_conn_current,
        "total_limit":   ov.total_conn_limit,
        "total_pct":     conn_pct,
    }

    # ── VSIDs ─────────────────────────────────────────────────────────
    vsids = []
    for vsid_info in s.vsids:
        diag = s.vsid_diags.get(vsid_info.vsid)
        vs_conn_pct = None
        if vsid_info.conn_limit > 0:
            conn_curr = diag.conn_current if diag else vsid_info.conn_current
            vs_conn_pct = round((conn_curr / vsid_info.conn_limit) * 100, 1)

        vsids.append({
            "vsid":            vsid_info.vsid,
            "name":            vsid_info.name,
            "type":            vsid_info.vtype,
            "short_type":      vsid_info.short_type,
            "policy":          vsid_info.policy,
            "conn_current":    diag.conn_current if diag else vsid_info.conn_current,
            "conn_peak":       vsid_info.conn_peak,
            "conn_limit":      vsid_info.conn_limit,
            "conn_pct":        vs_conn_pct,
            "securexl_status": diag.securexl.status if diag else "",
            "cpu_idle_pct":    diag.cpu_idle_pct if diag else None,
            "mem_used_pct":    diag.mem_used_pct if diag else "",
            "swap_used_mb":    diag.swap_used_mb if diag else None,
            "enabled_blades":  diag.enabled_blades if diag else "",
            "iface_errors": [
                {
                    "dev":       err.dev,
                    "direction": err.direction,
                    "errors":    err.errors,
                    "drops":     err.drops,
                    "error_rate_pct": err.error_rate_pct,
                }
                for err in (diag.iface_errors if diag else [])
            ],
        })

    # ── Attention items ───────────────────────────────────────────────
    attention = [
        {
            "severity": a.severity,
            "category": a.category,
            "message":  a.message,
        }
        for a in s.attention_items
    ]

    # ── HCP ───────────────────────────────────────────────────────────
    hcp = {
        "ran_ok":       s.hcp.ran_ok,
        "timed_out":    s.hcp.timed_out,
        "not_available": s.hcp.not_available,
        "error_count":  len(s.hcp.errors),
        "info_count":   len(s.hcp.infos),
        "skipped_count": len(s.hcp.skipped),
        "passed_count": len(s.hcp.passed),
        "errors": [
            {"vsid": r.vsid, "test_name": r.test_name, "status": r.status}
            for r in s.hcp.errors
        ],
        "infos": [
            {"vsid": r.vsid, "test_name": r.test_name, "status": r.status}
            for r in s.hcp.infos
        ],
    }

    # ── Delta summary ─────────────────────────────────────────────────
    delta_out = None
    if delta is not None:
        delta_out = {
            "prev_run_id":        delta.prev_run_id,
            "elapsed_seconds":    delta.elapsed_seconds,
            "suppressed":         delta.suppressed,
            "different_members":  delta.different_members,
            "has_changes":        delta.has_changes,
            "has_flagged":        delta.has_flagged,
            "failover_increased": delta.failover_count.flagged,
            "sync_changed":       delta.sync_status.flagged,
            "disk_root_delta_pp": delta.disk_root_pct.delta,
            "disk_log_delta_pp":  delta.disk_log_pct.delta,
            "cpu_idle_delta_pp":  delta.cpu_idle_pct.delta,
            "new_pnote_count":    len(delta.new_pnotes),
            "new_hcp_issue_count": len(delta.new_hcp_issues),
            "flagged_items": [
                {"metric": _delta_label(item_name), "reason": item.flag_reason}
                for item_name, item in [
                    ("failover_count",    delta.failover_count),
                    ("sync_status",       delta.sync_status),
                    ("sync_lost_updates", delta.sync_lost_updates),
                    ("cpu_idle_pct",      delta.cpu_idle_pct),
                    ("swap_used_mb",      delta.swap_used_mb),
                    ("disk_root_pct",     delta.disk_root_pct),
                    ("disk_log_pct",      delta.disk_log_pct),
                    ("total_conn",        delta.total_conn_current),
                ]
                if item.flagged
            ],
        }

    # ── Member comparison summary ─────────────────────────────────────
    mc_out = None
    mc = s.member_comparison
    if mc is not None:
        mc_out = {
            "reachable_count":    mc.reachable_count,
            "total_count":        len(mc.snapshots),
            "unreachable":        mc.unreachable,
            "has_flagged_diffs":  mc.has_flagged_diffs,
            "flagged_diff_count": sum(1 for d in mc.diffs if d.flagged),
            "diffs": [
                {
                    "metric":       d.metric,
                    "flagged":      d.flagged,
                    "member_values": d.member_values,
                    "note":         d.note,
                }
                for d in mc.diffs
            ],
            "members": [
                {
                    "name":            snap.name,
                    "ip":              snap.ip,
                    "reachable":       snap.reachable,
                    "own_state":       snap.own_state,
                    "sync_status":     snap.sync_status,
                    "failover_count":  snap.failover_count,
                    "disk_root_pct":   snap.disk_root_pct,
                    "disk_log_pct":    snap.disk_log_pct,
                    "cpu_idle_pct":    snap.cpu_idle_pct,
                    "swap_used_mb":    snap.swap_used_mb,
                    "corexl_instances": snap.corexl_instances,
                    "load_avg":        snap.load_avg,
                }
                for snap in mc.snapshots
            ],
        }

    return {
        "schema_version":    SCHEMA_VERSION,
        "run":               run,
        "platform":          platform,
        "cluster":           cluster,
        "connections":       connections,
        "vsids":             vsids,
        "attention_items":   attention,
        "hcp":               hcp,
        "delta":             delta_out,
        "member_comparison": mc_out,
    }


# ---------------------------------------------------------------------------
# CSV builder
# ---------------------------------------------------------------------------

def _build_csv_rows(
    s: HealthSummary,
    delta: Optional[DeltaReport],
) -> List[Dict[str, Any]]:
    """
    Build flat CSV rows — one per VSID.
    Run-level metadata is duplicated on each row for easy pivot/join.
    """
    ch   = s.cluster_health
    diag0 = s.vsid_diags.get(0)

    # Run-level fields shared across all rows
    run_fields = {
        "run_id":           s.run_timestamp,
        "collected_from":   s.topology.active_member or "",
        "collected_from_ip": s.topology.connected_ip or "",
        "profile":          s.active_profile,
        "cp_version":       s.platform.cp_version_short,
        "jhf_take":         _safe_int(s.platform.jhf_take),
        "cpu_idle_pct":     diag0.cpu_idle_pct if diag0 else "",
        "swap_used_mb":     diag0.swap_used_mb if diag0 else "",
        "disk_root_pct":    _pct_int(s.platform.disk_root_pct),
        "disk_log_pct":     _pct_int(s.platform.disk_log_pct),
        "load_avg":         s.platform.load_avg or "",
        "sync_status":      ch.sync_status,
        "sync_lost_updates": ch.sync_lost_updates,
        "failover_count":   ch.failover_count,
        "total_conn_current": s.vsx_overview.total_conn_current,
        "total_conn_limit":   s.vsx_overview.total_conn_limit,
        "attention_critical": sum(1 for a in s.attention_items if a.severity == "CRITICAL"),
        "attention_warning":  sum(1 for a in s.attention_items if a.severity == "WARNING"),
        "hcp_errors":         len(s.hcp.errors),
        # Delta summary columns — empty if no delta
        "delta_prev_run_id":         delta.prev_run_id if delta else "",
        "delta_elapsed_seconds":     delta.elapsed_seconds if delta else "",
        "delta_failover_increased":  int(delta.failover_count.flagged) if delta else "",
        "delta_sync_changed":        int(delta.sync_status.flagged)    if delta else "",
        "delta_disk_root_pp":        delta.disk_root_pct.delta         if delta else "",
        "delta_disk_log_pp":         delta.disk_log_pct.delta          if delta else "",
        "delta_cpu_idle_pp":         delta.cpu_idle_pct.delta          if delta else "",
        "delta_has_flagged":         int(delta.has_flagged)            if delta else "",
    }

    rows = []
    for vsid_info in s.vsids:
        diag = s.vsid_diags.get(vsid_info.vsid)
        conn_curr = diag.conn_current if diag else vsid_info.conn_current
        vs_conn_pct = ""
        if vsid_info.conn_limit > 0:
            vs_conn_pct = round((conn_curr / vsid_info.conn_limit) * 100, 1)

        # Iface errors as compact string
        iface_err_str = ""
        if diag and diag.iface_errors:
            iface_err_str = "; ".join(
                f"{e.dev}_{e.direction}:{e.errors}err/{e.drops}drop"
                for e in diag.iface_errors
            )

        row = dict(run_fields)   # copy run-level fields
        row.update({
            "vsid":            vsid_info.vsid,
            "vsid_name":       vsid_info.name,
            "vsid_type":       vsid_info.vtype,
            "vsid_short_type": vsid_info.short_type,
            "conn_current":    conn_curr,
            "conn_peak":       vsid_info.conn_peak,
            "conn_limit":      vsid_info.conn_limit,
            "conn_pct":        vs_conn_pct,
            "securexl_status": diag.securexl.status if diag else "",
            "enabled_blades":  diag.enabled_blades   if diag else "",
            "iface_errors":    iface_err_str,
        })
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct_int(pct_str: str) -> Any:
    """Convert '34%' -> 34, or None on failure."""
    try:
        return int(str(pct_str).strip().rstrip("%"))
    except (ValueError, AttributeError):
        return None


def _safe_int(val: str) -> Any:
    """Convert string to int, or None on failure."""
    try:
        return int(str(val).strip())
    except (ValueError, AttributeError):
        return None


def _delta_label(attr: str) -> str:
    """Human-readable label for a delta attribute name."""
    return {
        "failover_count":    "Failover Count",
        "sync_status":       "Sync Status",
        "sync_lost_updates": "Sync Lost Updates",
        "cpu_idle_pct":      "CPU Idle %",
        "swap_used_mb":      "Swap Used MB",
        "disk_root_pct":     "Root Disk %",
        "disk_log_pct":      "Log Disk %",
        "total_conn":        "Total Connections",
    }.get(attr, attr)
