"""
models/data.py
All dataclasses for vsx_diagnostics_py.

Each class maps directly to a data structure collected or derived during
the diagnostic run.  No SSH, no parsing logic here - pure data.

Design rules:
  - All fields have defaults so partial population is safe.
  - str fields default to "" (never None) unless the field genuinely
    distinguishes "not collected" from "empty result", in which case
    Optional[str] = None is used.
  - List/dict fields default to field(default_factory=...).
  - Severity on AttentionItem is a plain str ("WARNING" / "CRITICAL")
    to keep the renderers simple - no enum import needed in renderers.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from models.member import MemberComparison


# ---------------------------------------------------------------------------
# Cluster topology  (from local.vsall)
# ---------------------------------------------------------------------------

@dataclass
class ClusterMember:
    """One physical cluster member parsed from local.vsall."""
    name: str                       # e.g. "A-VSX-01"
    mgmt_ip: str = ""               # eth0 address
    sync_ip: str = ""               # eth2 address
    state: str = ""                 # ACTIVE / STANDBY / BACKUP / READY / DOWN
                                    # populated later from cphaprob stat


@dataclass
class ClusterTopology:
    """Everything derived from local.vsall plus the active member we connected to."""
    members: List[ClusterMember] = field(default_factory=list)
    cluster_vip: str = ""           # cluster_ip on eth0
    mgmt_server: str = ""           # masters_addresses
    icn_net: str = ""               # Internal Comms Network (funny IPs) network
    icn_mask: str = ""
    active_member: str = ""         # hostname we actually SSH'd into
    fwdir: str = ""                 # $FWDIR on the active member
    connected_ip: str = ""          # IP we connected to (10.1.1.2 / .3 / .4)


# ---------------------------------------------------------------------------
# VSX stat / VSID discovery  (from vsx stat -v and vsx stat -l)
# ---------------------------------------------------------------------------

@dataclass
class VSIDInfo:
    """One virtual device discovered from vsx stat -l."""
    vsid: int
    name: str = ""
    vtype: str = ""                 # "Virtual System" / "Virtual Switch" / "Virtual Router"
                                    # / "VSX Gateway"
    policy: str = ""
    conn_current: int = 0
    conn_peak: int = 0
    conn_limit: int = 0

    # Derived convenience properties
    @property
    def short_type(self) -> str:
        return {
            "VSX Gateway":    "GW",
            "Virtual System": "VS",
            "Virtual Switch": "VSW",
            "Virtual Router": "VR",
        }.get(self.vtype, self.vtype[:5] if self.vtype else "?")

    @property
    def is_firewall(self) -> bool:
        return self.vtype in ("Virtual System", "VSX Gateway")

    @property
    def is_switch(self) -> bool:
        return self.vtype == "Virtual Switch"

    @property
    def is_router(self) -> bool:
        return self.vtype == "Virtual Router"


@dataclass
class VSXOverview:
    """Global VSX counters from vsx stat -v."""
    total_conn_current: int = 0
    total_conn_limit: int = 0
    vs_license_count: int = 0       # "Number of Virtual Systems allowed by license"
    raw_output: str = ""            # full vsx stat -v text for the log


# ---------------------------------------------------------------------------
# NCS data  (from vsx showncs <vsid>)
# ---------------------------------------------------------------------------

@dataclass
class NCSInterface:
    dev: str = ""
    local_ip: str = ""
    local_mask: str = ""
    cluster_ip: str = ""
    cluster_mask: str = ""


@dataclass
class NCSWarpPair:
    name_a: str = ""                # wrp side (firewall)
    name_b: str = ""                # wrpj side (switch junction)
    cluster_ip: str = ""            # cluster_ip on name_a


@dataclass
class NCSRoute:
    dest: str = ""
    mask: str = ""
    gw: str = ""
    dev: str = ""


@dataclass
class NCSData:
    """Parsed output of vsx showncs <vsid> for one VSID."""
    vsid: int = 0
    available: bool = False         # False if showncs returned no output
    interfaces: List[NCSInterface] = field(default_factory=list)
    warp_pairs: List[NCSWarpPair] = field(default_factory=list)
    routes: List[NCSRoute] = field(default_factory=list)
    bridge_members: List[str] = field(default_factory=list)
    raw_output: str = ""


# ---------------------------------------------------------------------------
# Per-VSID diagnostics  (collected via vsenv subshell per VSID)
# ---------------------------------------------------------------------------

@dataclass
class IfaceError:
    """Non-zero RX or TX error/drop on one interface in one VSID."""
    vsid: int = 0
    dev: str = ""
    direction: str = ""             # "rx" or "tx"
    errors: int = 0
    drops: int = 0
    packets: int = 0

    @property
    def error_rate_pct(self) -> Optional[float]:
        if self.packets > 0 and self.errors > 0:
            return round((self.errors / self.packets) * 100, 2)
        return None


@dataclass
class SecureXLStatus:
    """SecureXL acceleration status for one VSID."""
    vsid: int = 0
    status: str = ""                # "enabled" / "disabled" / "n/a"
    raw_stat: str = ""              # full fwaccel stat output
    raw_stats_s: str = ""          # full fwaccel stats -s output


@dataclass
class VSIDDiag:
    """
    All per-VSID diagnostic data collected in one vsenv subshell.
    Fields map 1-to-1 with the collect_vs_diag() function in v18.
    """
    vsid: int = 0
    vtype: str = ""
    vname: str = ""

    # Software blades
    enabled_blades: str = ""        # normalised short label

    # CPU
    cpu_idle_pct: Optional[float] = None
    cpu_raw: str = ""               # raw mpstat / top output

    # Memory
    mem_used_mb: int = 0
    mem_total_mb: int = 0
    mem_used_pct: str = ""          # e.g. "42%"
    swap_used_mb: int = 0

    # Routing
    route_table: str = ""
    default_gw: str = ""

    # Interfaces
    ip_addr_raw: str = ""

    # Interface errors  (non-zero entries only)
    iface_errors: List[IfaceError] = field(default_factory=list)

    # SecureXL
    securexl: SecureXLStatus = field(default_factory=SecureXLStatus)

    # Connections
    conn_table_summary: str = ""
    conn_current: int = 0           # parsed from fw tab -t connections -s

    # NAT (firewall VSIDs only)
    nat_table_summary: str = ""

    # Bridge / WARP (switch VSIDs only)
    bridge_raw: str = ""
    warp_ifaces_raw: str = ""

    # CoreXL affinity (VS0 only)
    corexl_stat: str = ""
    corexl_instances: int = 0
    affinity_raw: str = ""          # deduplicated


# ---------------------------------------------------------------------------
# Cluster health  (from cphaprob / cpstat)
# ---------------------------------------------------------------------------

@dataclass
class PNOTEEntry:
    name: str = ""
    status: str = ""                # "OK" or problem string


@dataclass
class ClusterHealth:
    """Everything from the Cluster Health section of v18."""

    # cphaprob stat
    cluster_mode: str = ""          # e.g. "High Availability"
    member_states: Dict[str, str] = field(default_factory=dict)
                                    # {"A-VSX-01": "ACTIVE", ...}
    failover_count: int = 0
    failover_transition: str = ""
    failover_time: str = ""
    last_state_change: str = ""
    last_state_change_time: str = ""
    cphaprob_raw: str = ""

    # cphaprob -a if
    cphaprob_if_raw: str = ""
    monitored_ifaces: List[str] = field(default_factory=list)

    # cphaprob syncstat
    sync_status: str = ""           # "OK" / "SYNC_LOST" / etc.
    sync_lost_updates: int = 0
    syncstat_raw: str = ""

    # cpstat ha -f all
    cpstat_ha_raw: str = ""
    pnote_entries: List[PNOTEEntry] = field(default_factory=list)

    @property
    def pnote_issues(self) -> List[PNOTEEntry]:
        return [p for p in self.pnote_entries if p.status not in ("OK", "")]


# ---------------------------------------------------------------------------
# Platform information
# ---------------------------------------------------------------------------

@dataclass
class PlatformInfo:
    hostname: str = ""
    cp_version: str = ""            # raw fw ver output line 1
    cp_version_short: str = ""      # e.g. "R82"
    cp_build: str = ""              # build number
    jhf_take: str = ""              # JHF Take number
    kernel: str = ""                # uname -r
    uptime_raw: str = ""
    load_avg: str = ""              # "0.12, 0.08, 0.05"
    disk_root_pct: str = ""         # e.g. "34%"
    disk_log_pct: str = ""          # e.g. "12%"
    cplic_raw: str = ""             # cplic print (first 20 lines)
    cpinfo_raw: str = ""            # cpinfo -y all

    # CPView historical CPU data (VS0 / gateway-wide)
    cpview_available: bool = False          # False if cpview not present or failed
    cpview_cpu_5m_idle: Optional[float] = None   # 5-minute average CPU idle %
    cpview_cpu_15m_idle: Optional[float] = None  # 15-minute average CPU idle %
    cpview_cpu_1h_idle: Optional[float] = None   # 1-hour average CPU idle % (if available)
    cpview_raw: str = ""                    # raw cpview output for log



# ---------------------------------------------------------------------------
# HCP (Health Check Point) results
# ---------------------------------------------------------------------------

@dataclass
class HCPResult:
    """One test result row from the hcp -r all summary table."""
    vsid: int = 0
    test_name: str = ""
    status: str = ""            # "PASSED" / "INFO" / "ERROR" / "SKIPPED"
    runtime_sec: float = 0.0   # from the failed-tests table (when present)


@dataclass
class HCPTestDetail:
    """
    Full detail block for one non-PASSED test.
    Extracted from the pipe-table section of hcp -r all output.
    """
    vsid: int = 0
    test_name: str = ""
    status: str = ""
    description: str = ""
    finding: str = ""           # raw finding text (may include ASCII tables)
    suggested: str = ""         # suggested solutions text


@dataclass
class HCPCollection:
    """All HCP data collected from one gateway run."""
    hostname: str = ""
    ran_ok: bool = False        # False if hcp timed out or was not available
    timed_out: bool = False
    not_available: bool = False # True if hcp binary not found
    raw_summary: str = ""       # full terminal output of hcp -r all
    results: List[HCPResult] = field(default_factory=list)
    details: List[HCPTestDetail] = field(default_factory=list)
    # Path to the downloaded tar.gz on A-GUI (empty if download failed)
    local_archive_path: str = ""

    @property
    def errors(self) -> List[HCPResult]:
        return [r for r in self.results if r.status == "ERROR"]

    @property
    def infos(self) -> List[HCPResult]:
        return [r for r in self.results if r.status == "INFO"]

    @property
    def skipped(self) -> List[HCPResult]:
        return [r for r in self.results if r.status == "SKIPPED"]

    @property
    def passed(self) -> List[HCPResult]:
        return [r for r in self.results if r.status == "PASSED"]

    def detail_for(self, test_name: str) -> "HCPTestDetail | None":
        """Look up the detail block for a given test name."""
        for d in self.details:
            if d.test_name.strip() == test_name.strip():
                return d
        return None


# ---------------------------------------------------------------------------
# Health assessment output
# ---------------------------------------------------------------------------

@dataclass
class AttentionItem:
    """One item in the ATTENTION section of the executive summary."""
    severity: str = "WARNING"       # "WARNING" or "CRITICAL"
    category: str = ""              # e.g. "Sync", "SecureXL", "Disk"
    message: str = ""

    def __str__(self) -> str:
        return f"[{self.severity}] {self.category}: {self.message}"


@dataclass
class HealthSummary:
    """
    The fully assessed health picture - input to all renderers.
    This is the single object passed to console.py, logfile.py, html.py.
    """
    # --- Run metadata ---
    script_version: str = ""
    run_timestamp: str = ""         # ISO-format string

    # --- Collected data ---
    platform: PlatformInfo = field(default_factory=PlatformInfo)
    topology: ClusterTopology = field(default_factory=ClusterTopology)
    vsx_overview: VSXOverview = field(default_factory=VSXOverview)
    vsids: List[VSIDInfo] = field(default_factory=list)
    ncs: Dict[int, NCSData] = field(default_factory=dict)
                                    # keyed by vsid
    vsid_diags: Dict[int, VSIDDiag] = field(default_factory=dict)
                                    # keyed by vsid
    cluster_health: ClusterHealth = field(default_factory=ClusterHealth)
    hcp: HCPCollection = field(default_factory=HCPCollection)

    # --- Assessment output ---
    attention_items: List[AttentionItem] = field(default_factory=list)

    # --- Flags ---
    do_fetch: bool = False
    showncs_available: bool = False

    # --- Active threshold profile ---
    active_profile: str = "production"   # name of the ThresholdProfile used this run

    # --- All-member collection ---
    # None until collect_all_members() has run; None if only 1 member reachable
    member_comparison: Optional["MemberComparison"] = None

    @property
    def health_ok(self) -> bool:
        return len(self.attention_items) == 0

    @property
    def vsids_by_id(self) -> Dict[int, VSIDInfo]:
        return {v.vsid: v for v in self.vsids}

    @property
    def firewall_vsids(self) -> List[VSIDInfo]:
        return [v for v in self.vsids if v.is_firewall]

    @property
    def switch_vsids(self) -> List[VSIDInfo]:
        return [v for v in self.vsids if v.is_switch]

    @property
    def router_vsids(self) -> List[VSIDInfo]:
        return [v for v in self.vsids if v.is_router]
