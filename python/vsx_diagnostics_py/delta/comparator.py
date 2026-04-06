"""
delta/comparator.py
Pure comparison function: takes two RunSnapshots, returns a DeltaReport.

compare(prev, curr) -> DeltaReport

No I/O, no SSH, no rendering — pure data transformation.
This makes it independently testable with synthetic snapshots.

Flagging rules
--------------
Metric                  Flag condition                      Suppressed when elapsed < MIN_DELTA_SECONDS
----------------------  ----------------------------------  -------------------------------------------
failover_count          any increase                        no  (cluster events are always notable)
sync_status             change away from "OK"               no
sync_lost_updates       any increase                        no
member_states           any state change per member         no
cpu_idle_pct            drop >= CPU_DROP_WARN_PP            yes
swap_used_mb            increase >= SWAP_INCREASE_WARN_MB   yes
disk_root_pct           increase >= DISK_INCREASE_WARN_PP   yes
disk_log_pct            increase >= DISK_INCREASE_WARN_PP   yes
total_conn_current      increase >= CONN_INCREASE_WARN_PCT  yes (relative %)
per-VSID conn_pct       increase >= VSID_CONN_WARN_PP       yes
per-VSID securexl       any change                          yes
per-iface errors/drops  any increase                        yes

Counter rollback (curr < prev) is flagged as direction="reset" with
flag_reason noting the likely cause (reboot or interface bounce).
Rollback items are flagged regardless of the MIN_DELTA_SECONDS guard
because they indicate a structural event.

PNOTE / HCP set differences are always computed; flagging of new_pnotes
and new_hcp_issues respects the suppression guard.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

from models.snapshot import (
    DeltaItem,
    DeltaReport,
    IfaceErrorDelta,
    RunSnapshot,
    VSIDDelta,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

MIN_DELTA_SECONDS    = 120    # suppress resource flags if runs are this close together
CPU_DROP_WARN_PP     = 10     # percentage points drop in cpu_idle to flag
SWAP_INCREASE_WARN_MB = 50    # MB increase in swap to flag
DISK_INCREASE_WARN_PP = 5     # percentage point increase in disk usage to flag
CONN_INCREASE_WARN_PCT = 20   # relative % increase in total connections to flag
VSID_CONN_WARN_PP    = 10     # pp increase in per-VSID connection % to flag


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compare(prev: RunSnapshot, curr: RunSnapshot) -> DeltaReport:
    """
    Compare two RunSnapshots and return a fully populated DeltaReport.
    prev and curr must not be None (caller checks before calling).
    """
    elapsed = _elapsed_seconds(prev.run_id, curr.run_id)
    suppress = elapsed < MIN_DELTA_SECONDS

    report = DeltaReport(
        prev_run_id      = prev.run_id,
        curr_run_id      = curr.run_id,
        elapsed_seconds  = elapsed,
        suppressed       = suppress,
        different_members = (
            prev.collected_from_host != curr.collected_from_host
            and bool(prev.collected_from_host) and bool(curr.collected_from_host)
        ),
        prev_member = prev.collected_from_host,
        curr_member = curr.collected_from_host,
    )

    # ── Cluster (never suppressed — state events are always notable) ──
    report.failover_count = _compare_numeric(
        prev.failover_count, curr.failover_count,
        flag_if=lambda d, _p, _c: d > 0,
        flag_reason_fn=lambda d, _p, c: f"failover count increased by {d} (now {c})",
        suppress=False,
    )

    report.sync_status = _compare_string(
        prev.sync_status, curr.sync_status,
        flag_if=lambda p, c: c != "OK" or (p == "OK" and c != "OK"),
        flag_reason_fn=lambda p, c: f"sync changed: {p!r} -> {c!r}",
        suppress=False,
    )

    report.sync_lost_updates = _compare_numeric(
        prev.sync_lost_updates, curr.sync_lost_updates,
        flag_if=lambda d, _p, _c: d > 0,
        flag_reason_fn=lambda d, _p, c: f"sync lost updates increased by {d} (now {c})",
        suppress=False,
    )

    report.member_states = _compare_member_states(prev.member_states, curr.member_states)

    # ── Platform ──────────────────────────────────────────────────────
    report.cpu_idle_pct = _compare_numeric_float(
        prev.cpu_idle_pct, curr.cpu_idle_pct,
        flag_if=lambda d, _p, _c: d is not None and d <= -CPU_DROP_WARN_PP,
        flag_reason_fn=lambda d, _p, c: (
            f"CPU idle dropped {abs(d):.1f} pp (now {c:.1f}%)"
        ),
        suppress=suppress,
    )

    report.swap_used_mb = _compare_numeric(
        prev.swap_used_mb, curr.swap_used_mb,
        flag_if=lambda d, _p, _c: d >= SWAP_INCREASE_WARN_MB,
        flag_reason_fn=lambda d, _p, c: f"swap increased by {d} MB (now {c} MB)",
        suppress=suppress,
    )

    report.disk_root_pct = _compare_numeric(
        prev.disk_root_pct, curr.disk_root_pct,
        flag_if=lambda d, _p, _c: d >= DISK_INCREASE_WARN_PP,
        flag_reason_fn=lambda d, _p, c: f"root disk grew {d} pp (now {c}%)",
        suppress=suppress,
    )

    report.disk_log_pct = _compare_numeric(
        prev.disk_log_pct, curr.disk_log_pct,
        flag_if=lambda d, _p, _c: d >= DISK_INCREASE_WARN_PP,
        flag_reason_fn=lambda d, _p, c: f"/var/log disk grew {d} pp (now {c}%)",
        suppress=suppress,
    )

    # ── Connections (global) ──────────────────────────────────────────
    report.total_conn_current = _compare_conn_relative(
        prev.total_conn_current, curr.total_conn_current,
        suppress=suppress,
    )

    # ── Per-VSID ──────────────────────────────────────────────────────
    all_vsid_keys = set(prev.vsids) | set(curr.vsids)
    for vsid_str in sorted(all_vsid_keys, key=lambda s: int(s)):
        vsid_int = int(vsid_str)
        prev_vs = prev.vsids.get(vsid_str)
        curr_vs = curr.vsids.get(vsid_str)
        report.vsid_deltas[vsid_int] = _compare_vsid(
            vsid_int, prev_vs, curr_vs, suppress=suppress
        )

    # ── PNOTE set difference ──────────────────────────────────────────
    report.new_pnotes, report.resolved_pnotes, report.changed_pnotes = _set_diff_pnotes(
        prev.pnote_issues, curr.pnote_issues
    )

    # ── HCP set difference ────────────────────────────────────────────
    if prev.hcp_ran_ok and curr.hcp_ran_ok:
        report.new_hcp_issues, report.resolved_hcp_issues = _set_diff_hcp(
            prev.hcp_results, curr.hcp_results
        )

    log.info(
        "Delta: elapsed=%ds suppressed=%s has_changes=%s has_flagged=%s",
        elapsed, suppress, report.has_changes, report.has_flagged,
    )
    return report


# ---------------------------------------------------------------------------
# Metric comparators
# ---------------------------------------------------------------------------

def _compare_numeric(
    prev_val: int,
    curr_val: int,
    flag_if,
    flag_reason_fn,
    suppress: bool,
) -> DeltaItem:
    """Compare two integer metrics."""
    delta = curr_val - prev_val

    if delta == 0:
        direction = "unchanged"
    elif delta > 0:
        direction = "up"
    else:
        # Counter went down — may be a reset
        direction = "reset" if curr_val < prev_val else "down"

    # Counter rollback is always flagged (structural event)
    is_rollback = direction == "reset"
    if is_rollback:
        return DeltaItem(
            prev=prev_val, curr=curr_val, delta=delta,
            direction="reset", flagged=True,
            flag_reason=(
                f"counter decreased {prev_val} -> {curr_val} "
                "(likely reboot or interface reset)"
            ),
        )

    flagged = (not suppress) and flag_if(delta, prev_val, curr_val)
    return DeltaItem(
        prev=prev_val, curr=curr_val, delta=delta,
        direction=direction,
        flagged=flagged,
        flag_reason=flag_reason_fn(delta, prev_val, curr_val) if flagged else "",
    )


def _compare_numeric_float(
    prev_val: Optional[float],
    curr_val: Optional[float],
    flag_if,
    flag_reason_fn,
    suppress: bool,
) -> DeltaItem:
    """Compare two optional float metrics (e.g. cpu_idle_pct)."""
    if prev_val is None or curr_val is None:
        return DeltaItem(prev=prev_val, curr=curr_val, direction="n/a")

    delta = curr_val - prev_val
    direction = "unchanged" if abs(delta) < 0.1 else ("up" if delta > 0 else "down")

    flagged = (not suppress) and flag_if(delta, prev_val, curr_val)
    return DeltaItem(
        prev=prev_val, curr=curr_val, delta=round(delta, 1),
        direction=direction,
        flagged=flagged,
        flag_reason=flag_reason_fn(delta, prev_val, curr_val) if flagged else "",
    )


def _compare_string(
    prev_val: str,
    curr_val: str,
    flag_if,
    flag_reason_fn,
    suppress: bool,
) -> DeltaItem:
    """Compare two string metrics."""
    if not prev_val and not curr_val:
        return DeltaItem(prev=prev_val, curr=curr_val, direction="n/a")
    if not prev_val:
        return DeltaItem(prev=prev_val, curr=curr_val, direction="new")
    if not curr_val:
        return DeltaItem(prev=prev_val, curr=curr_val, direction="gone")

    if prev_val == curr_val:
        return DeltaItem(prev=prev_val, curr=curr_val, delta=None, direction="unchanged")

    flagged = (not suppress) and flag_if(prev_val, curr_val)
    return DeltaItem(
        prev=prev_val, curr=curr_val, delta=None,
        direction="changed",
        flagged=flagged,
        flag_reason=flag_reason_fn(prev_val, curr_val) if flagged else "",
    )


def _compare_conn_relative(
    prev_val: int, curr_val: int, suppress: bool
) -> DeltaItem:
    """
    Flag if connections increased by >= CONN_INCREASE_WARN_PCT relative %.
    Avoids flagging in low-connection lab environments where delta of 5
    connections is 50% relative but meaningless.
    """
    delta = curr_val - prev_val
    if delta == 0:
        return DeltaItem(prev=prev_val, curr=curr_val, delta=0, direction="unchanged")

    if prev_val > 0:
        rel_pct = (delta / prev_val) * 100
    else:
        rel_pct = 100.0 if delta > 0 else 0.0

    direction = "up" if delta > 0 else "down"
    flagged = (
        not suppress
        and delta > 0
        and rel_pct >= CONN_INCREASE_WARN_PCT
        and curr_val > 100          # ignore trivial absolute counts in lab
    )
    reason = (
        f"connections increased {rel_pct:.0f}% ({prev_val} -> {curr_val})"
        if flagged else ""
    )
    return DeltaItem(
        prev=prev_val, curr=curr_val, delta=delta,
        direction=direction, flagged=flagged, flag_reason=reason,
    )


def _compare_member_states(
    prev_states: Dict[str, str],
    curr_states: Dict[str, str],
) -> Dict[str, DeltaItem]:
    """
    Per-member state comparison.  Never suppressed — member state changes
    are always notable regardless of run gap.
    """
    all_members = set(prev_states) | set(curr_states)
    result: Dict[str, DeltaItem] = {}

    for member in sorted(all_members):
        prev_s = prev_states.get(member, "")
        curr_s = curr_states.get(member, "")

        if not prev_s and not curr_s:
            result[member] = DeltaItem(direction="n/a")
            continue
        if not prev_s:
            result[member] = DeltaItem(
                prev=prev_s, curr=curr_s, direction="new",
                flagged=True,
                flag_reason=f"{member} appeared with state {curr_s!r}",
            )
            continue
        if not curr_s:
            result[member] = DeltaItem(
                prev=prev_s, curr=curr_s, direction="gone",
                flagged=True,
                flag_reason=f"{member} disappeared (was {prev_s!r})",
            )
            continue
        if prev_s == curr_s:
            result[member] = DeltaItem(prev=prev_s, curr=curr_s, direction="unchanged")
            continue

        # State changed — always flag
        result[member] = DeltaItem(
            prev=prev_s, curr=curr_s, delta=None,
            direction="changed", flagged=True,
            flag_reason=f"{member}: {prev_s} -> {curr_s}",
        )

    return result


# ---------------------------------------------------------------------------
# Per-VSID comparison
# ---------------------------------------------------------------------------

def _compare_vsid(
    vsid: int,
    prev_vs,   # VSIDSnapshot or None
    curr_vs,   # VSIDSnapshot or None
    suppress: bool,
) -> VSIDDelta:
    """Build a VSIDDelta from two optional VSIDSnapshots."""
    name = (curr_vs or prev_vs).name if (curr_vs or prev_vs) else ""
    vdelta = VSIDDelta(vsid=vsid, name=name)

    if prev_vs is None or curr_vs is None:
        # VSID appeared or disappeared — not a delta metric, just annotate
        return vdelta

    # Connection count
    vdelta.conn_current = _compare_numeric(
        prev_vs.conn_current, curr_vs.conn_current,
        flag_if=lambda d, _p, _c: False,   # raw count not flagged; pct is
        flag_reason_fn=lambda d, _p, c: "",
        suppress=suppress,
    )

    # Connection % — only meaningful when limit > 0
    if curr_vs.conn_limit > 0 and prev_vs.conn_limit > 0:
        prev_pct = (prev_vs.conn_current / prev_vs.conn_limit) * 100
        curr_pct = (curr_vs.conn_current / curr_vs.conn_limit) * 100
        vdelta.conn_pct = _compare_numeric_float(
            prev_pct, curr_pct,
            flag_if=lambda d, _p, _c: d >= VSID_CONN_WARN_PP,
            flag_reason_fn=lambda d, _p, c: (
                f"VSID {vsid} connection usage up {d:.1f} pp (now {c:.1f}%)"
            ),
            suppress=suppress,
        )

    # SecureXL
    vdelta.securexl_status = _compare_string(
        prev_vs.securexl_status, curr_vs.securexl_status,
        flag_if=lambda p, c: True,          # any change is notable
        flag_reason_fn=lambda p, c: f"VSID {vsid} SecureXL: {p!r} -> {c!r}",
        suppress=suppress,
    )

    # Interface errors
    vdelta.iface_error_deltas = _compare_iface_errors(
        vsid, prev_vs.iface_errors, curr_vs.iface_errors, suppress=suppress
    )

    return vdelta


def _compare_iface_errors(
    vsid: int,
    prev_errors: List[dict],
    curr_errors: List[dict],
    suppress: bool,
) -> List[IfaceErrorDelta]:
    """
    Compare cumulative interface error counters between two runs.
    Keys are (dev, direction).  A counter going up means errors accumulated.
    A counter going down means a reset (reboot / interface bounce).
    """
    def _key(e: dict) -> Tuple[str, str]:
        return (e.get("dev", ""), e.get("direction", ""))

    prev_map = {_key(e): e for e in prev_errors}
    curr_map = {_key(e): e for e in curr_errors}
    all_keys = set(prev_map) | set(curr_map)

    results: List[IfaceErrorDelta] = []

    for key in sorted(all_keys):
        dev, direction = key
        pe = prev_map.get(key, {"errors": 0, "drops": 0})
        ce = curr_map.get(key, {"errors": 0, "drops": 0})

        p_err, c_err = int(pe.get("errors", 0)), int(ce.get("errors", 0))
        p_drp, c_drp = int(pe.get("drops", 0)), int(ce.get("drops", 0))
        d_err = c_err - p_err
        d_drp = c_drp - p_drp

        if d_err == 0 and d_drp == 0:
            continue    # no change — omit from delta list

        # Determine flag and reason
        is_reset = (c_err < p_err) or (c_drp < p_drp)
        if is_reset:
            flagged = True
            reason = (
                f"VSID {vsid} {dev} {direction}: counter reset "
                f"(errors {p_err}->{c_err}, drops {p_drp}->{c_drp}) "
                "— likely reboot or interface bounce"
            )
        else:
            flagged = not suppress and (d_err > 0 or d_drp > 0)
            reason = (
                f"VSID {vsid} {dev} {direction}: "
                f"+{d_err} errors, +{d_drp} drops since last run"
                if flagged else ""
            )

        results.append(IfaceErrorDelta(
            vsid=vsid, dev=dev, direction=direction,
            prev_errors=p_err, curr_errors=c_err, delta_errors=d_err,
            prev_drops=p_drp, curr_drops=c_drp, delta_drops=d_drp,
            flagged=flagged, flag_reason=reason,
        ))

    return results


# ---------------------------------------------------------------------------
# PNOTE / HCP set differences
# ---------------------------------------------------------------------------

def _set_diff_pnotes(
    prev_list: List[dict],
    curr_list: List[dict],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    Return (new_pnotes, resolved_pnotes, changed_pnotes).
    - new: name in curr only
    - resolved: name in prev only
    - changed: name in both but status differs
    """
    prev_by_name = {p.get("name", ""): p for p in prev_list}
    curr_by_name = {p.get("name", ""): p for p in curr_list}

    new_names      = set(curr_by_name) - set(prev_by_name)
    resolved_names = set(prev_by_name) - set(curr_by_name)
    common_names   = set(prev_by_name) & set(curr_by_name)

    changed = [
        {"name": n,
         "prev_status": prev_by_name[n].get("status", ""),
         "curr_status": curr_by_name[n].get("status", "")}
        for n in sorted(common_names)
        if prev_by_name[n].get("status") != curr_by_name[n].get("status")
    ]

    return (
        [curr_by_name[n] for n in sorted(new_names)],
        [prev_by_name[n] for n in sorted(resolved_names)],
        changed,
    )


def _set_diff_hcp(
    prev_results: List[dict],
    curr_results: List[dict],
) -> Tuple[List[dict], List[dict]]:
    """
    Return (new_issues, resolved_issues).
    A "new issue" is a test_name+vsid that was PASSED (or absent) in prev
    and is ERROR/WARNING/INFO in curr.
    A "resolved issue" is the reverse.
    """
    _PROBLEM = {"ERROR", "WARNING", "INFO"}

    def _key(r: dict):
        return (r.get("vsid", 0), r.get("test_name", ""))

    prev_map = {_key(r): r for r in prev_results}
    curr_map = {_key(r): r for r in curr_results}
    all_keys = set(prev_map) | set(curr_map)

    new_issues:      List[dict] = []
    resolved_issues: List[dict] = []

    for key in sorted(all_keys):
        prev_r = prev_map.get(key)
        curr_r = curr_map.get(key)

        prev_status = prev_r.get("status", "PASSED") if prev_r else "PASSED"
        curr_status = curr_r.get("status", "PASSED") if curr_r else "PASSED"

        prev_problem = prev_status in _PROBLEM
        curr_problem = curr_status in _PROBLEM

        if curr_problem and not prev_problem:
            new_issues.append({
                "vsid":        key[0],
                "test_name":   key[1],
                "prev_status": prev_status,
                "curr_status": curr_status,
            })
        elif prev_problem and not curr_problem:
            resolved_issues.append({
                "vsid":        key[0],
                "test_name":   key[1],
                "prev_status": prev_status,
                "curr_status": curr_status,
            })

    return new_issues, resolved_issues


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _elapsed_seconds(prev_run_id: str, curr_run_id: str) -> int:
    """
    Parse two ISO timestamp strings and return elapsed time in seconds.
    Returns 0 on any parse error (safe — will not suppress unexpectedly).
    """
    fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        t_prev = datetime.datetime.strptime(prev_run_id[:19], fmt)
        t_curr = datetime.datetime.strptime(curr_run_id[:19], fmt)
        return max(0, int((t_curr - t_prev).total_seconds()))
    except (ValueError, TypeError):
        log.debug("Could not parse run_id timestamps: %r %r", prev_run_id, curr_run_id)
        return 0
