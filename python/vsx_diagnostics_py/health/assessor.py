"""
health/assessor.py
Applies all health assessment rules to a fully populated HealthSummary
and populates summary.attention_items.

assess(summary) -> HealthSummary   (mutates and returns the same object)

Rules implemented (matching v18 lines 1026-1159, plus HCP integration):

  1. Cluster sync status != OK
  2. Cluster sync lost updates > 0
  3. Cluster failover count > 0
  4. Cluster degraded state (ACTIVE! in last state change)
  5. Cluster member was DOWN (last state change)
  6. PNOTE issues (non-OK entries in Problem Notification table)
  7. SecureXL not enabled on any firewall VSID
  8. CPU idle < 50% (VS0)
  9. Swap in use > 100 MB (VS0)
 10. Total connections >= 80% of cluster limit
 11. Per-VSID connections >= 80% of VSID limit
 12. Root filesystem >= 80%
 13. Log filesystem >= 80%
 14. Interface errors with non-zero error rate (annotated if cluster-monitored)
 15. HCP ERROR results
 16. HCP INFO results (lower severity)

Severity mapping:
    CRITICAL — cluster sync lost, failover, degraded state, CPU, connections
    WARNING  — SecureXL, disk, interface errors, HCP errors, swap
    INFO     — HCP info items

Bond Health HCP ERROR on Hyper-V:
    LACP aggregator ID mismatches and port-state sync warnings are expected
    on Hyper-V virtual switches (no real LACP partner).  We detect this
    pattern and downgrade from WARNING to INFO with a lab annotation.
    Detection heuristic: Bond Health ERROR + all findings mention
    'aggregator ID' or 'port state' or 'actor port state'.
"""

from __future__ import annotations

import logging
from typing import List

from models.data import AttentionItem, HealthSummary

log = logging.getLogger(__name__)

# Thresholds (match v18 exactly)
_CPU_IDLE_WARN_PCT   = 50     # flag if idle < this
_SWAP_WARN_MB        = 100    # flag if swap used > this
_CONN_WARN_PCT       = 80     # flag if connections >= this % of limit
_DISK_WARN_PCT       = 80     # flag if disk usage >= this %


def assess(summary: HealthSummary) -> HealthSummary:
    """
    Apply all health rules to summary and populate summary.attention_items.
    Returns the same object (mutated in place).
    """
    summary.attention_items.clear()

    _check_cluster_sync(summary)
    _check_cluster_failover(summary)
    _check_cluster_degraded(summary)
    _check_pnotes(summary)
    _check_securexl(summary)
    _check_cpu(summary)
    _check_memory(summary)
    _check_connections(summary)
    _check_disk(summary)
    _check_iface_errors(summary)
    _check_hcp(summary)

    if summary.attention_items:
        log.warning(
            "Assessment: %d attention item(s) — %d CRITICAL, %d WARNING, %d INFO",
            len(summary.attention_items),
            sum(1 for a in summary.attention_items if a.severity == "CRITICAL"),
            sum(1 for a in summary.attention_items if a.severity == "WARNING"),
            sum(1 for a in summary.attention_items if a.severity == "INFO"),
        )
    else:
        log.info("Assessment: all checks passed — no attention items")

    return summary


# ---------------------------------------------------------------------------
# Individual rule functions
# ---------------------------------------------------------------------------

def _add(summary: HealthSummary, severity: str, category: str, message: str) -> None:
    summary.attention_items.append(
        AttentionItem(severity=severity, category=category, message=message)
    )


def _check_cluster_sync(summary: HealthSummary) -> None:
    """Rules 1 & 2 — sync status and lost updates."""
    ch = summary.cluster_health
    if not ch.sync_status:
        return

    if ch.sync_status != "OK":
        _add(summary, "CRITICAL", "Cluster Sync",
             f"Sync status: {ch.sync_status}")

    if ch.sync_lost_updates > 0:
        _add(summary, "CRITICAL", "Cluster Sync",
             f"Lost sync updates: {ch.sync_lost_updates}")


def _check_cluster_failover(summary: HealthSummary) -> None:
    """Rule 3 — failover count > 0."""
    ch = summary.cluster_health
    if ch.failover_count > 0:
        trans = ch.failover_transition or "unknown transition"
        time_ = ch.failover_time or "unknown time"
        _add(summary, "CRITICAL", "Cluster Failover",
             f"Failover occurred: {trans} at {time_} "
             f"(failover count: {ch.failover_count})")


def _check_cluster_degraded(summary: HealthSummary) -> None:
    """Rules 4 & 5 — degraded state or DOWN in last state change."""
    ch = summary.cluster_health
    sc = ch.last_state_change
    if not sc:
        return

    time_ = ch.last_state_change_time or "unknown time"

    if '(!)' in sc:
        _add(summary, "CRITICAL", "Cluster State",
             f"Degraded state detected: '{sc}' at {time_} (now resolved)")
    elif sc.upper().startswith("DOWN"):
        _add(summary, "CRITICAL", "Cluster State",
             f"Member was DOWN: '{sc}' at {time_} (now resolved)")


def _check_pnotes(summary: HealthSummary) -> None:
    """Rule 6 — non-OK PNOTE entries."""
    issues = summary.cluster_health.pnote_issues
    if issues:
        detail = ", ".join(f"{p.name}:{p.status}" for p in issues)
        _add(summary, "WARNING", "PNOTE",
             f"Problem Notification issues: {detail}")


def _check_securexl(summary: HealthSummary) -> None:
    """Rule 7 — SecureXL not enabled on any firewall VSID."""
    for vsid_info in summary.firewall_vsids:
        diag = summary.vsid_diags.get(vsid_info.vsid)
        if not diag:
            continue
        status = diag.securexl.status
        if status and status not in ("enabled", "n/a"):
            _add(summary, "WARNING", "SecureXL",
                 f"SecureXL not enabled on VSID {vsid_info.vsid} "
                 f"({vsid_info.name}): {status}")


def _check_cpu(summary: HealthSummary) -> None:
    """Rule 8 — CPU idle below threshold (VS0)."""
    diag0 = summary.vsid_diags.get(0)
    if not diag0 or diag0.cpu_idle_pct is None:
        return

    if diag0.cpu_idle_pct < _CPU_IDLE_WARN_PCT:
        _add(summary, "CRITICAL", "CPU",
             f"CPU idle below {_CPU_IDLE_WARN_PCT}%: "
             f"{diag0.cpu_idle_pct:.1f}% idle "
             f"(load avg: {summary.platform.load_avg or '?'})")


def _check_memory(summary: HealthSummary) -> None:
    """Rule 9 — swap usage above threshold (VS0)."""
    diag0 = summary.vsid_diags.get(0)
    if not diag0:
        return

    if diag0.swap_used_mb > _SWAP_WARN_MB:
        _add(summary, "WARNING", "Memory",
             f"Swap in use: {diag0.swap_used_mb} MB "
             f"(threshold: {_SWAP_WARN_MB} MB)")


def _check_connections(summary: HealthSummary) -> None:
    """Rules 10 & 11 — total and per-VSID connection capacity."""
    ov = summary.vsx_overview

    # Rule 10 — total cluster connections
    if ov.total_conn_limit > 0:
        pct = (ov.total_conn_current * 100) // ov.total_conn_limit
        if pct >= _CONN_WARN_PCT:
            _add(summary, "CRITICAL", "Connections",
                 f"Total connection usage at {pct}% of cluster limit "
                 f"({ov.total_conn_current}/{ov.total_conn_limit})")

    # Rule 11 — per-VSID
    for vsid_info in summary.vsids:
        if vsid_info.conn_limit <= 0:
            continue
        diag = summary.vsid_diags.get(vsid_info.vsid)
        conn = diag.conn_current if diag else vsid_info.conn_current
        pct = (conn * 100) // vsid_info.conn_limit
        if pct >= _CONN_WARN_PCT:
            _add(summary, "CRITICAL", "Connections",
                 f"VSID {vsid_info.vsid} ({vsid_info.name}) at {pct}% "
                 f"connection capacity ({conn}/{vsid_info.conn_limit})")


def _check_disk(summary: HealthSummary) -> None:
    """Rules 12 & 13 — disk usage."""
    root_pct = _pct_int(summary.platform.disk_root_pct)
    log_pct  = _pct_int(summary.platform.disk_log_pct)

    if root_pct is not None and root_pct >= _DISK_WARN_PCT:
        _add(summary, "WARNING", "Disk",
             f"Root filesystem at {summary.platform.disk_root_pct}")

    if log_pct is not None and log_pct >= _DISK_WARN_PCT:
        _add(summary, "WARNING", "Disk",
             f"Log filesystem (/var/log) at {summary.platform.disk_log_pct}")


def _check_iface_errors(summary: HealthSummary) -> None:
    """Rule 14 — interface errors with error rate annotation."""
    monitored = set(summary.cluster_health.monitored_ifaces)

    for vsid_info in summary.vsids:
        diag = summary.vsid_diags.get(vsid_info.vsid)
        if not diag:
            continue
        for err in diag.iface_errors:
            mon_note = (
                "" if err.dev in monitored
                else " [not cluster-monitored — will not trigger failover]"
            )
            if err.errors > 0 and err.error_rate_pct is not None:
                msg = (
                    f"VSID {vsid_info.vsid} ({vsid_info.name}) "
                    f"{err.dev}: {err.direction}_err={err.errors} / "
                    f"{err.packets} packets "
                    f"({err.error_rate_pct:.2f}% error rate){mon_note}"
                )
            else:
                msg = (
                    f"VSID {vsid_info.vsid} ({vsid_info.name}) "
                    f"{err.dev}: {err.direction}_err={err.errors} "
                    f"{err.direction}_drop={err.drops}{mon_note}"
                )
            _add(summary, "WARNING", "Interface Errors", msg)


def _check_hcp(summary: HealthSummary) -> None:
    """Rules 15 & 16 — HCP ERROR and INFO results."""
    hcp = summary.hcp
    if not hcp.ran_ok:
        if hcp.not_available:
            log.info("HCP: not available on this gateway — skipping HCP checks")
        elif hcp.timed_out:
            _add(summary, "WARNING", "HCP",
                 "hcp -r all timed out — health check results unavailable")
        return

    # Gather all non-PASSED, non-SKIPPED results
    # hcp uses: ERROR, WARNING (note: different from our severity levels), INFO
    non_passing = [r for r in hcp.results
                   if r.status not in ("PASSED", "SKIPPED")]

    for result in non_passing:
        # Bond Health on Hyper-V — check if it's lab noise
        if result.test_name == "Bond Health" and result.status == "ERROR":
            detail = hcp.detail_for("Bond Health")
            if detail and _is_hyperv_bond_noise(detail.finding):
                _add(summary, "INFO", "HCP / Bond Health",
                     "LACP bond sync warnings detected "
                     "[expected on Hyper-V virtual switch — not a real fault]")
                continue

        detail = hcp.detail_for(result.test_name)
        finding = _first_line(detail.finding) if detail else ""
        msg = f"[VS {result.vsid}] {result.test_name}"
        if finding:
            msg += f": {finding}"

        # Map hcp status to our severity
        if result.status == "ERROR":
            our_severity = "WARNING"
        elif result.status == "WARNING":
            our_severity = "WARNING"
        else:  # INFO
            our_severity = "INFO"

        _add(summary, our_severity, "HCP", msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct_int(pct_str: str) -> int | None:
    """Convert '34%' -> 34.  Returns None if not parseable."""
    try:
        return int(pct_str.strip().rstrip('%'))
    except (ValueError, AttributeError):
        return None


def _first_line(text: str) -> str:
    """
    Return first meaningful non-table line of text, stripped.
    Prefers lines that are not inside pipe-delimited ASCII tables.
    Falls back to first non-empty non-separator line if no prose found.
    """
    first_non_empty = ""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        # Skip pure separator lines (+-=| only)
        if set(s) <= set('|-+= '):
            continue
        # Prefer lines that are not pipe-table rows
        if not (s.startswith('|') and s.endswith('|')):
            return s[:120]
        # Remember first non-separator line as fallback
        if not first_non_empty:
            first_non_empty = s[:120]
    return first_non_empty


def _is_hyperv_bond_noise(finding: str) -> bool:
    """
    Heuristic: Bond Health ERROR is Hyper-V virtual switch noise if ALL
    findings mention aggregator ID mismatch or port state sync warnings.
    These are expected when LACP has no real physical partner.
    """
    if not finding:
        return False
    hyperv_indicators = (
        'aggregator id',
        'actor port state',
        'partner port state',
        'port is not synced',
    )
    lines = [l.lower() for l in finding.splitlines() if l.strip()]
    if not lines:
        return False
    # All non-table-border lines that contain actual text should be indicators
    content_lines = [
        l for l in lines
        if not set(l.strip()) <= set('|-+= ')
    ]
    if not content_lines:
        return False
    return all(
        any(ind in line for ind in hyperv_indicators)
        for line in content_lines
    )
