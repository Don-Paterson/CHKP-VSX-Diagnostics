"""
models/thresholds.py
Environment threshold profiles for health assessment and delta comparison.

Three built-in profiles:
    lab        — loose thresholds for Hyper-V/Skillable lab environments.
                 Suppresses noise from warm VMs, uneven virtual disks,
                 and frequent diagnostic runs during course delivery.
    virtual    — moderate thresholds for VMware/cloud-hosted gateways
                 that run cooler than a lab but aren't bare-metal.
    production — tight thresholds matching v18 and CP best practice.
                 This was the previous hardcoded behaviour.

Usage
-----
    from models.thresholds import ThresholdProfile, get_profile
    profile = get_profile("lab")          # from CLI --profile arg
    assess(summary, profile=profile)
    compare(prev, curr, profile=profile)
    compare_members(snapshots, profile=profile)

Custom profiles can be created by starting from a preset and overriding
individual fields:
    profile = get_profile("production")
    profile.cpu_idle_warn_pct = 40        # slightly looser CPU threshold
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class ThresholdProfile:
    """
    All tunable thresholds in one place.
    Fields are grouped by the subsystem that uses them.

    Naming convention
    -----------------
    *_warn_pct   — percentage (0-100), flag when value >= or <= this
    *_warn_mb    — megabytes, flag when value exceeds this
    *_warn_pp    — percentage points, flag when delta exceeds this
    *_warn_pct_rel — relative percentage change, flag when change >= this
    *_seconds    — seconds
    """
    name: str = "production"
    description: str = ""

    # ── Assessor thresholds (health/assessor.py) ──────────────────────
    # Rule 8: CPU idle — flag if idle% falls below this
    cpu_idle_warn_pct: int = 50

    # Rule 9: Swap — flag if swap used exceeds this (MB)
    swap_warn_mb: int = 100

    # Rules 10 & 11: Connections — flag if usage >= this % of limit
    conn_warn_pct: int = 80

    # Rules 12 & 13: Disk — flag if usage >= this %
    disk_warn_pct: int = 80

    # ── Delta comparator thresholds (delta/comparator.py) ────────────
    # Minimum elapsed time between runs before resource flags are raised
    delta_min_gap_seconds: int = 120

    # CPU idle: flag if idle dropped by >= this many pp
    delta_cpu_drop_pp: int = 10

    # Swap: flag if swap increased by >= this MB
    delta_swap_increase_mb: int = 50

    # Disk: flag if disk% increased by >= this pp per run
    delta_disk_increase_pp: int = 5

    # Connections: flag if total connections grew by >= this relative %
    # (only applied when absolute count > 100 — lab guard)
    delta_conn_increase_pct_rel: int = 20

    # Per-VSID: flag if VSID connection% grew by >= this pp
    delta_vsid_conn_increase_pp: int = 10

    # ── Member comparator thresholds (collectors/member_comparator.py) ─
    # Flag if disk% spread across members exceeds this pp
    member_disk_spread_pp: int = 10

    # Flag if CPU idle% spread across members exceeds this pp
    member_cpu_spread_pp: int = 20

    # Flag if swap used spread across members exceeds this MB
    member_swap_spread_mb: int = 100


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

_PROFILES: Dict[str, ThresholdProfile] = {

    "production": ThresholdProfile(
        name        = "production",
        description = "Tight thresholds — bare-metal or dedicated virtual appliances",
        # Assessor
        cpu_idle_warn_pct   = 50,
        swap_warn_mb        = 100,
        conn_warn_pct       = 80,
        disk_warn_pct       = 80,
        # Delta
        delta_min_gap_seconds       = 120,
        delta_cpu_drop_pp           = 10,
        delta_swap_increase_mb      = 50,
        delta_disk_increase_pp      = 5,
        delta_conn_increase_pct_rel = 20,
        delta_vsid_conn_increase_pp = 10,
        # Member
        member_disk_spread_pp  = 10,
        member_cpu_spread_pp   = 20,
        member_swap_spread_mb  = 100,
    ),

    "virtual": ThresholdProfile(
        name        = "virtual",
        description = "Moderate thresholds — VMware or cloud-hosted gateways",
        # Assessor — slightly relaxed; VMs run warmer and use more swap
        cpu_idle_warn_pct   = 35,
        swap_warn_mb        = 200,
        conn_warn_pct       = 85,
        disk_warn_pct       = 85,
        # Delta — wider gaps expected between diagnostic runs
        delta_min_gap_seconds       = 180,
        delta_cpu_drop_pp           = 15,
        delta_swap_increase_mb      = 100,
        delta_disk_increase_pp      = 7,
        delta_conn_increase_pct_rel = 30,
        delta_vsid_conn_increase_pp = 15,
        # Member — more tolerance for spread in shared-host environments
        member_disk_spread_pp  = 15,
        member_cpu_spread_pp   = 30,
        member_swap_spread_mb  = 200,
    ),

    "lab": ThresholdProfile(
        name        = "lab",
        description = (
            "Loose thresholds — Hyper-V/Skillable lab environments. "
            "Suppresses noise from warm VMs, uneven virtual disks, "
            "and frequent diagnostic runs during course delivery."
        ),
        # Assessor — very loose; lab VMs routinely run at 10-30% idle,
        # use significant swap, and have uneven disk from snapshot activity
        cpu_idle_warn_pct   = 20,
        swap_warn_mb        = 500,
        conn_warn_pct       = 90,
        disk_warn_pct       = 90,
        # Delta — runs are often back-to-back during demos; use a 5-minute
        # suppression window and wide change tolerances
        delta_min_gap_seconds       = 300,
        delta_cpu_drop_pp           = 20,
        delta_swap_increase_mb      = 200,
        delta_disk_increase_pp      = 10,
        delta_conn_increase_pct_rel = 50,
        delta_vsid_conn_increase_pp = 20,
        # Member — Hyper-V disk allocation is not perfectly even
        member_disk_spread_pp  = 20,
        member_cpu_spread_pp   = 40,
        member_swap_spread_mb  = 300,
    ),
}

VALID_PROFILES = list(_PROFILES.keys())
DEFAULT_PROFILE = "production"


def get_profile(name: str) -> ThresholdProfile:
    """
    Return the named ThresholdProfile.
    Raises ValueError for unknown names — caller should validate against
    VALID_PROFILES before calling.
    """
    if name not in _PROFILES:
        raise ValueError(
            f"Unknown profile {name!r}. "
            f"Valid options: {', '.join(VALID_PROFILES)}"
        )
    return _PROFILES[name]


def profile_summary_lines(profile: ThresholdProfile) -> list:
    """
    Return a list of plain-text lines describing the active profile.
    Used in log file and console output headers.
    """
    return [
        f"  Profile      : {profile.name}",
        f"  Description  : {profile.description}",
        f"  CPU idle warn: < {profile.cpu_idle_warn_pct}%",
        f"  Swap warn    : > {profile.swap_warn_mb} MB",
        f"  Conn warn    : >= {profile.conn_warn_pct}%",
        f"  Disk warn    : >= {profile.disk_warn_pct}%",
    ]
