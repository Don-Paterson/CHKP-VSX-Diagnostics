"""
models/member.py
Dataclasses for per-member health snapshots and cross-member comparison.

These are separate from the main HealthSummary (which reflects the active
member only) and from RunSnapshot (which is for run-to-run delta).

MemberSnapshot   — health data collected from one cluster member
MemberComparison — cross-member differences, passed to renderers
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class MemberIfaceError:
    """Non-zero error/drop counter on one interface of one member."""
    dev: str = ""
    direction: str = ""     # "rx" / "tx"
    errors: int = 0
    drops: int = 0


@dataclass
class MemberSnapshot:
    """
    Health data collected from one cluster member via a direct SSH connection.
    Collected for every reachable member including the primary session member.
    """
    # ── Identity ──────────────────────────────────────────────────────
    name: str = ""              # e.g. "A-VSX-01" (from vsall / hostname cmd)
    ip: str = ""                # management IP we connected to
    reachable: bool = False     # False if SSH connect failed

    # ── Cluster state ─────────────────────────────────────────────────
    # This member's own view of the cluster — may differ from active member's view
    member_states: Dict[str, str] = field(default_factory=dict)
    own_state: str = ""         # this member's state as seen by itself
    failover_count: int = 0
    sync_status: str = ""
    sync_lost_updates: int = 0

    # ── Platform ──────────────────────────────────────────────────────
    cp_version_short: str = ""  # "R82"
    jhf_take: str = ""          # "91"
    uptime_raw: str = ""
    load_avg: str = ""          # "0.12, 0.08, 0.05"
    disk_root_pct: int = 0      # parsed from "34%" -> 34
    disk_log_pct: int = 0
    cpu_idle_pct: Optional[float] = None   # from mpstat 1 1 in VS0
    swap_used_mb: int = 0

    # ── CoreXL ────────────────────────────────────────────────────────
    corexl_instances: int = 0

    # ── Interface errors (VS0 context) ────────────────────────────────
    iface_errors: List[MemberIfaceError] = field(default_factory=list)

    # ── Raw outputs (for log file) ────────────────────────────────────
    cphaprob_raw: str = ""
    syncstat_raw: str = ""
    error_msg: str = ""         # set if collection failed partway through


@dataclass
class MemberDiff:
    """
    One metric that differs across members.
    members_values: {"A-VSX-01": value, "A-VSX-02": value, ...}
    """
    metric: str = ""
    member_values: Dict[str, str] = field(default_factory=dict)
    flagged: bool = False
    note: str = ""


@dataclass
class MemberComparison:
    """
    Cross-member comparison result.
    Produced by compare_members() from a list of MemberSnapshots.
    Passed as Optional[MemberComparison] to all renderers.
    None means only one member was reachable (nothing to compare).
    """
    snapshots: List[MemberSnapshot] = field(default_factory=list)

    # Members that could not be reached
    unreachable: List[str] = field(default_factory=list)

    # Metrics that differ across members
    diffs: List[MemberDiff] = field(default_factory=list)

    # Members whose cluster state view disagrees with the primary
    state_disagreements: List[str] = field(default_factory=list)

    # Members with non-zero interface errors
    members_with_iface_errors: List[str] = field(default_factory=list)

    @property
    def reachable_count(self) -> int:
        return sum(1 for s in self.snapshots if s.reachable)

    @property
    def has_diffs(self) -> bool:
        return bool(self.diffs or self.unreachable)

    @property
    def has_flagged_diffs(self) -> bool:
        return any(d.flagged for d in self.diffs)
