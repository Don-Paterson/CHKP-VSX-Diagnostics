"""
collectors/member_comparator.py
Pure function: compare(snapshots) -> MemberComparison

Takes a list of MemberSnapshots (one per cluster member) and produces
a MemberComparison highlighting per-member differences.

No SSH, no I/O — pure data transformation, independently testable.

Metrics compared
----------------
Metric                      Flag if
--------------------------  ----------------------------------------
cp_version_short            any member differs from others
jhf_take                    any member differs from others
failover_count              any member differs from others
sync_status                 any member differs from others
sync_lost_updates           any member differs from others
corexl_instances            any member differs from others
disk_root_pct               spread > DISK_SPREAD_WARN_PP across members
disk_log_pct                spread > DISK_SPREAD_WARN_PP across members
cpu_idle_pct                spread > CPU_SPREAD_WARN_PP across members
swap_used_mb                spread > SWAP_SPREAD_WARN_MB across members
member_states (view)        any member's view of the cluster differs from
                            the primary member's view (split-brain indicator)
iface_errors                per-member: any non-zero error/drop counters
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from models.member import MemberComparison, MemberDiff, MemberSnapshot
from models.thresholds import ThresholdProfile, get_profile, DEFAULT_PROFILE

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fallback constants — only used if called without a profile
# ---------------------------------------------------------------------------

DISK_SPREAD_WARN_PP   = 10
CPU_SPREAD_WARN_PP    = 20
SWAP_SPREAD_WARN_MB   = 100


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compare_members(
    snapshots: List[MemberSnapshot],
    profile: Optional[ThresholdProfile] = None,
) -> Optional[MemberComparison]:
    """
    Compare all MemberSnapshots and return a MemberComparison.
    Returns comparison with empty diffs if fewer than 2 members are reachable.

    Parameters
    ----------
    snapshots : one MemberSnapshot per cluster member
    profile   : ThresholdProfile; defaults to 'production' if None
    """
    if profile is None:
        profile = get_profile(DEFAULT_PROFILE)

    reachable = [s for s in snapshots if s.reachable]

    comparison = MemberComparison(snapshots=snapshots)
    comparison.unreachable = [s.name for s in snapshots if not s.reachable]

    if len(reachable) < 2:
        log.info("Member comparison: only %d reachable member(s) — skipping cross-member diff", len(reachable))
        return comparison

    # ── Exact-match metrics ────────────────────────────────────────────
    for attr, label in [
        ("cp_version_short", "CP Version"),
        ("jhf_take",         "JHF Take"),
        ("failover_count",   "Failover Count"),
        ("sync_status",      "Sync Status"),
        ("sync_lost_updates","Sync Lost Updates"),
        ("corexl_instances", "CoreXL Instances"),
    ]:
        diff = _exact_diff(reachable, attr, label)
        if diff:
            comparison.diffs.append(diff)

    # ── Spread-based metrics ───────────────────────────────────────────
    for attr, label, threshold, fmt in [
        ("disk_root_pct", "Root Disk %",    profile.member_disk_spread_pp,  "{}%"),
        ("disk_log_pct",  "Log Disk %",     profile.member_disk_spread_pp,  "{}%"),
        ("cpu_idle_pct",  "CPU Idle %",     profile.member_cpu_spread_pp,   "{:.1f}%"),
        ("swap_used_mb",  "Swap Used",      profile.member_swap_spread_mb,  "{} MB"),
    ]:
        diff = _spread_diff(reachable, attr, label, threshold, fmt)
        if diff:
            comparison.diffs.append(diff)

    # ── Cluster state view disagreements ──────────────────────────────
    primary = reachable[0]
    for snap in reachable[1:]:
        if snap.member_states != primary.member_states:
            comparison.state_disagreements.append(snap.name)
            comparison.diffs.append(MemberDiff(
                metric=f"Cluster state view ({snap.name})",
                member_values={
                    primary.name: _fmt_states(primary.member_states),
                    snap.name:    _fmt_states(snap.member_states),
                },
                flagged=True,
                note=(
                    f"{snap.name}'s view of the cluster differs from "
                    f"{primary.name} — possible split-brain or stale state"
                ),
            ))

    # ── Per-member interface errors ────────────────────────────────────
    for snap in reachable:
        if snap.iface_errors:
            comparison.members_with_iface_errors.append(snap.name)
            err_summary = ", ".join(
                f"{e.dev} {e.direction}_err={e.errors} drop={e.drops}"
                for e in snap.iface_errors
            )
            comparison.diffs.append(MemberDiff(
                metric=f"Interface Errors ({snap.name})",
                member_values={snap.name: err_summary},
                flagged=True,
                note=f"{snap.name} has non-zero interface error counters",
            ))

    log.info(
        "Member comparison: %d reachable, %d diff(s), %d flagged",
        len(reachable),
        len(comparison.diffs),
        sum(1 for d in comparison.diffs if d.flagged),
    )
    return comparison


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

def _exact_diff(
    snapshots: List[MemberSnapshot],
    attr: str,
    label: str,
) -> Optional[MemberDiff]:
    """
    Return a MemberDiff if the attribute value is not identical across
    all reachable snapshots.  Returns None if all agree.
    """
    values: Dict[str, str] = {}
    for s in snapshots:
        val = getattr(s, attr, None)
        values[s.name] = str(val) if val is not None else "n/a"

    unique = set(values.values())
    if len(unique) <= 1:
        return None  # all identical

    return MemberDiff(
        metric=label,
        member_values=values,
        flagged=True,
        note=f"{label} differs across members: {', '.join(sorted(unique))}",
    )


def _spread_diff(
    snapshots: List[MemberSnapshot],
    attr: str,
    label: str,
    threshold: float,
    fmt: str,
) -> Optional[MemberDiff]:
    """
    Return a MemberDiff if the spread (max - min) across reachable members
    exceeds threshold.  Returns None if all within threshold or insufficient data.
    """
    values: Dict[str, Optional[float]] = {}
    for s in snapshots:
        val = getattr(s, attr, None)
        if isinstance(val, (int, float)):
            values[s.name] = float(val)
        else:
            values[s.name] = None

    numeric = [v for v in values.values() if v is not None]
    if len(numeric) < 2:
        return None

    spread = max(numeric) - min(numeric)
    if spread <= threshold:
        return None

    try:
        member_values = {
            name: (fmt.format(v) if v is not None else "n/a")
            for name, v in values.items()
        }
    except (ValueError, TypeError):
        member_values = {name: str(v) for name, v in values.items()}

    return MemberDiff(
        metric=label,
        member_values=member_values,
        flagged=True,
        note=(
            f"{label} spread is {fmt.format(spread)} across members "
            f"(threshold: {fmt.format(threshold)})"
        ),
    )


def _fmt_states(states: Dict[str, str]) -> str:
    """Format member_states dict as a compact string."""
    return " / ".join(f"{k}:{v}" for k, v in sorted(states.items()))
