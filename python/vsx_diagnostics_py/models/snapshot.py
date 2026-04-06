"""
models/snapshot.py
Serialisable snapshot of the delta-relevant fields from a diagnostic run,
and the dataclasses that represent a comparison between two snapshots.

Design rules
------------
- RunSnapshot holds only the fields needed for delta comparison — not raw
  command output.  This keeps snapshot.json small (< 5 KB typical).
- All numeric fields are stored as Python ints or floats, never as
  percentage strings.  The % stripping happens once in serialiser.py.
- DeltaItem is generic: prev/curr/delta hold Any, typed by the caller.
  flagged=True means the change should appear in the attention section.
- DeltaReport is a pure data structure — no I/O, no SSH, no rendering.
  It is the output of comparator.compare(prev, curr) and is passed as
  Optional[DeltaReport] into all renderers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# RunSnapshot — one serialisable diagnostic run
# ---------------------------------------------------------------------------

@dataclass
class VSIDSnapshot:
    """Per-VSID delta-relevant fields."""
    vsid: int
    name: str = ""
    vtype: str = ""                   # "Virtual System" / "Virtual Switch" / ...
    conn_current: int = 0
    conn_limit: int = 0
    securexl_status: str = ""         # "enabled" / "disabled" / "n/a"
    # Cumulative error counters — non-zero entries only.
    # Each entry: {"dev": str, "direction": str, "errors": int, "drops": int}
    iface_errors: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class RunSnapshot:
    """
    Slimmed-down, JSON-serialisable projection of HealthSummary.
    Written to disk after every successful run as <stem>.snapshot.json.
    """
    # ── Meta ──────────────────────────────────────────────────────────
    run_id: str = ""                  # ISO timestamp "2025-04-06T14:32:00"
    tool_version: str = ""
    collected_from_ip: str = ""       # topology.connected_ip
    collected_from_host: str = ""     # topology.active_member
    cp_version_short: str = ""        # "R82" / "R81.10"
    jhf_take: str = ""

    # ── Cluster ───────────────────────────────────────────────────────
    # {"A-VSX-01": "ACTIVE", "A-VSX-02": "STANDBY", ...}
    member_states: Dict[str, str] = field(default_factory=dict)
    sync_status: str = ""             # "OK" / "SYNC_LOST" / ...
    sync_lost_updates: int = 0
    failover_count: int = 0

    # ── Platform ──────────────────────────────────────────────────────
    disk_root_pct: int = 0            # parsed from "34%" → 34
    disk_log_pct: int = 0
    cpu_idle_pct: Optional[float] = None   # from vsid_diags[0]; None if not collected
    swap_used_mb: int = 0                  # from vsid_diags[0]

    # ── VSX connections (global) ──────────────────────────────────────
    total_conn_current: int = 0
    total_conn_limit: int = 0

    # ── Per-VSID ──────────────────────────────────────────────────────
    vsids: Dict[str, VSIDSnapshot] = field(default_factory=dict)
    # Keys are str(vsid) for JSON compatibility; comparator converts to int.

    # ── PNOTE ─────────────────────────────────────────────────────────
    # [{"name": "...", "status": "..."}]  — non-OK entries only
    pnote_issues: List[Dict[str, str]] = field(default_factory=list)

    # ── HCP ───────────────────────────────────────────────────────────
    hcp_ran_ok: bool = False
    # [{"vsid": int, "test_name": str, "status": str}]
    hcp_results: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DeltaItem — one compared metric
# ---------------------------------------------------------------------------

@dataclass
class DeltaItem:
    """
    Represents the change in a single metric between two runs.

    direction values
    ----------------
    "up"        — numeric value increased
    "down"      — numeric value decreased (may be good or bad)
    "unchanged" — identical values
    "changed"   — non-numeric value changed (e.g. sync_status string)
    "new"       — value present in curr, absent in prev
    "gone"      — value present in prev, absent in curr
    "reset"     — numeric counter went down (likely a reboot/bounce)
    "n/a"       — one or both sides are None (not comparable)
    """
    prev: Any = None
    curr: Any = None
    delta: Any = None           # curr - prev for numerics; None for strings/bools
    direction: str = "n/a"
    flagged: bool = False
    flag_reason: str = ""       # empty if not flagged


# ---------------------------------------------------------------------------
# VSIDDelta — per-VSID change summary
# ---------------------------------------------------------------------------

@dataclass
class IfaceErrorDelta:
    """Change in error/drop counters on one interface in one direction."""
    vsid: int = 0
    dev: str = ""
    direction: str = ""         # "rx" / "tx"
    prev_errors: int = 0
    curr_errors: int = 0
    delta_errors: int = 0
    prev_drops: int = 0
    curr_drops: int = 0
    delta_drops: int = 0
    flagged: bool = False
    flag_reason: str = ""


@dataclass
class VSIDDelta:
    vsid: int = 0
    name: str = ""
    conn_current: DeltaItem = field(default_factory=DeltaItem)
    conn_pct: DeltaItem = field(default_factory=DeltaItem)   # current/limit * 100
    securexl_status: DeltaItem = field(default_factory=DeltaItem)
    iface_error_deltas: List[IfaceErrorDelta] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return (
            self.conn_current.flagged
            or self.conn_pct.flagged
            or self.securexl_status.flagged
            or any(e.flagged for e in self.iface_error_deltas)
        )


# ---------------------------------------------------------------------------
# DeltaReport — full comparison result
# ---------------------------------------------------------------------------

@dataclass
class DeltaReport:
    """
    Output of comparator.compare(prev, curr).
    Passed as Optional[DeltaReport] into all renderers.
    None means no previous snapshot was available.
    """
    prev_run_id: str = ""
    curr_run_id: str = ""
    elapsed_seconds: int = 0

    # True when elapsed < MIN_DELTA_SECONDS — deltas computed but not flagged.
    suppressed: bool = False

    # ── Cluster ───────────────────────────────────────────────────────
    failover_count: DeltaItem = field(default_factory=DeltaItem)
    sync_status: DeltaItem = field(default_factory=DeltaItem)
    sync_lost_updates: DeltaItem = field(default_factory=DeltaItem)
    # per-member state changes: {"A-VSX-01": DeltaItem, ...}
    member_states: Dict[str, DeltaItem] = field(default_factory=dict)

    # ── Platform ──────────────────────────────────────────────────────
    cpu_idle_pct: DeltaItem = field(default_factory=DeltaItem)
    swap_used_mb: DeltaItem = field(default_factory=DeltaItem)
    disk_root_pct: DeltaItem = field(default_factory=DeltaItem)
    disk_log_pct: DeltaItem = field(default_factory=DeltaItem)

    # ── Connections (global) ──────────────────────────────────────────
    total_conn_current: DeltaItem = field(default_factory=DeltaItem)

    # ── Per-VSID ──────────────────────────────────────────────────────
    vsid_deltas: Dict[int, VSIDDelta] = field(default_factory=dict)

    # ── PNOTE ─────────────────────────────────────────────────────────
    new_pnotes: List[Dict[str, str]] = field(default_factory=list)
    resolved_pnotes: List[Dict[str, str]] = field(default_factory=list)
    changed_pnotes: List[Dict[str, str]] = field(default_factory=list)  # name in both, status changed

    # ── HCP ───────────────────────────────────────────────────────────
    # Each entry: {"vsid": int, "test_name": str, "prev_status": str, "curr_status": str}
    new_hcp_issues: List[Dict[str, Any]] = field(default_factory=list)
    resolved_hcp_issues: List[Dict[str, Any]] = field(default_factory=list)

    # ── Collector provenance ──────────────────────────────────────────
    # Noted when prev and curr were collected from different cluster members.
    different_members: bool = False
    prev_member: str = ""
    curr_member: str = ""

    @property
    def flagged_items(self) -> List[DeltaItem]:
        """All top-level DeltaItems that are flagged."""
        items = [
            self.failover_count, self.sync_status, self.sync_lost_updates,
            self.cpu_idle_pct, self.swap_used_mb,
            self.disk_root_pct, self.disk_log_pct,
            self.total_conn_current,
        ]
        items += list(self.member_states.values())
        return [i for i in items if i.flagged]

    @property
    def has_changes(self) -> bool:
        """True if any metric changed, flagged or not."""
        top_changed = any(
            i.direction not in ("unchanged", "n/a")
            for i in [
                self.failover_count, self.sync_status, self.sync_lost_updates,
                self.cpu_idle_pct, self.swap_used_mb,
                self.disk_root_pct, self.disk_log_pct,
                self.total_conn_current,
            ]
        )
        vsid_changed = any(v.has_changes for v in self.vsid_deltas.values())
        pnote_changed = bool(self.new_pnotes or self.resolved_pnotes or self.changed_pnotes)
        hcp_changed = bool(self.new_hcp_issues or self.resolved_hcp_issues)
        member_changed = any(
            i.direction not in ("unchanged", "n/a")
            for i in self.member_states.values()
        )
        return top_changed or vsid_changed or pnote_changed or hcp_changed or member_changed

    @property
    def has_flagged(self) -> bool:
        """True if any metric is flagged (i.e. warrants attention)."""
        if self.suppressed:
            return False
        if self.flagged_items:
            return True
        if any(v.has_changes for v in self.vsid_deltas.values()):
            return True
        if self.new_pnotes or self.new_hcp_issues:
            return True
        return False
