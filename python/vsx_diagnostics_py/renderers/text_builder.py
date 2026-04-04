"""
renderers/text_builder.py
Shared logic that builds the plain-text report sections from a HealthSummary.

Used by both console.py and logfile.py to avoid duplication.
Returns a list of strings (lines) that each renderer writes in its own way.

Section order matches v18 exactly:
  1.  Header
  2.  Environment
  3.  Cluster Members
  4.  Virtual Devices
  5.  Traffic Flow
  6.  Per-VSID Status Table
  7.  HEALTH
  8.  ATTENTION
"""

from __future__ import annotations

from typing import List

from models.data import HealthSummary, VSIDInfo, NCSData


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_summary_lines(s: HealthSummary) -> List[str]:
    """Return all executive summary lines as a list of strings."""
    lines: List[str] = []

    lines += _header(s)
    lines += _environment(s)
    lines += _cluster_members(s)
    lines += _virtual_devices(s)
    lines += _traffic_flow(s)
    lines += _vsid_status_table(s)
    lines += _health_section(s)
    lines += _attention_section(s)
    lines += _footer(s)

    return lines


def build_full_lines(s: HealthSummary) -> List[str]:
    """
    Return the full diagnostic output — all collected raw data sections
    followed by the executive summary.
    The logfile renderer uses this; the console renderer uses summary only.
    """
    lines: List[str] = []

    lines += _banner("VSX Gateway Health Diagnostics")
    lines += [
        f"  Script  : vsx_diagnostics_py",
        f"  Gateway : {s.topology.active_member or s.topology.connected_ip}",
        f"  Date    : {s.run_timestamp}",
        f"  FWDIR   : {s.topology.fwdir}",
        f"  Version : {s.platform.cp_version}",
        "",
    ]

    # Platform
    lines += _banner("Platform Information")
    lines += [
        f"  CP Version : {s.platform.cp_version_short} Build {s.platform.cp_build}",
        f"  JHF Take   : {s.platform.jhf_take or '?'}",
        f"  Kernel     : {s.platform.kernel}",
        f"  Uptime     : {s.platform.uptime_raw}",
        f"  Disk /     : {s.platform.disk_root_pct}",
        f"  Disk /log  : {s.platform.disk_log_pct}",
        "",
    ]
    if s.platform.cplic_raw:
        lines += _section("License")
        lines += [f"  {l}" for l in s.platform.cplic_raw.splitlines()]
        lines += [""]

    # VSX Overview
    lines += _banner("VSX Overview")
    lines += [s.vsx_overview.raw_output, ""]

    # VSID discovery
    lines += _banner("Virtual Device Discovery")
    lines += _vsid_table_raw(s)

    # NCS topology
    lines += _banner("Topology Map")
    lines += _ncs_topology(s)

    # CoreXL
    lines += _banner("CoreXL & CPU Affinity")
    diag0 = s.vsid_diags.get(0)
    if diag0:
        lines += _section("CoreXL Instance Status")
        lines += [diag0.corexl_stat or "  [not collected]", ""]
        lines += _section("Firewall Kernel Affinity")
        lines += [diag0.affinity_raw or "  [not collected]", ""]

    # Per-VSID detail
    for vsid_info in s.vsids:
        lines += _banner(f"VSID {vsid_info.vsid} - {vsid_info.name} ({vsid_info.vtype})")
        diag = s.vsid_diags.get(vsid_info.vsid)
        if not diag:
            lines += ["  [no data collected]", ""]
            continue
        lines += _section("Enabled Software Blades")
        lines += [f"  {diag.enabled_blades}", ""]
        lines += _section("CPU (1-second sample)")
        lines += [diag.cpu_raw or "  [not collected]", ""]
        if not vsid_info.is_switch:
            lines += _section("Routing Table")
            lines += [diag.route_table or "  [not collected]", ""]
        lines += _section("Interface Addresses")
        lines += [diag.ip_addr_raw or "  [not collected]", ""]
        if not vsid_info.is_switch:
            lines += _section("SecureXL Status")
            lines += [diag.securexl.raw_stat or "  [not collected]", ""]
        lines += _section("Connections Table Summary")
        lines += [diag.conn_table_summary or "  [not collected]", ""]
        if vsid_info.is_switch:
            lines += _section("Bridge Interfaces")
            lines += [diag.bridge_raw or "  [not collected]", ""]
        lines += [f"-- End VSID {vsid_info.vsid} --", ""]

    # Cluster health
    lines += _banner("Cluster Health")
    ch = s.cluster_health
    if ch.cphaprob_raw:
        lines += _section("Cluster Member State")
        lines += [ch.cphaprob_raw, ""]
        lines += _section("Cluster Interfaces")
        lines += [ch.cphaprob_if_raw or "  [not collected]", ""]
        lines += _section("Cluster Synchronisation")
        lines += [ch.syncstat_raw or "  [not collected]", ""]
        lines += _section("Cluster HA Statistics")
        lines += [ch.cpstat_ha_raw or "  [not collected]", ""]
    else:
        lines += ["  [ClusterXL not active or not a cluster member — skipped]", ""]

    # HCP — write raw output to log regardless of parse success
    if s.hcp.raw_summary:
        lines += _banner("HCP Health Check Results")
        lines += [s.hcp.raw_summary, ""]

    # Executive summary
    lines += _banner("Executive Summary")
    lines += build_summary_lines(s)

    return lines


# ---------------------------------------------------------------------------
# Summary sections
# ---------------------------------------------------------------------------

def _header(s: HealthSummary) -> List[str]:
    return [
        "",
        "=" * 62,
        "  VSX Diagnostics — Executive Summary",
        f"  {s.run_timestamp}",
        "=" * 62,
        "",
    ]


def _environment(s: HealthSummary) -> List[str]:
    p  = s.platform
    t  = s.topology
    ov = s.vsx_overview
    ch = s.cluster_health

    member_count  = len(t.members) or "?"
    gw_count      = len(s.firewall_vsids)
    version_str   = (
        f"{p.cp_version_short or 'Check Point'} "
        f"Build {p.cp_build or '?'} + JHF Take {p.jhf_take or '?'}"
    )

    lines = [
        "ENVIRONMENT",
        f"  {version_str}",
        f"  {member_count}-member VSX cluster ({ch.cluster_mode or 'unknown mode'})",
        f"  Licensed for {ov.vs_license_count or '?'} Virtual Systems, "
        f"{gw_count} configured",
        f"  Management: {t.mgmt_server or '?'}  "
        f"Cluster VIP: {t.cluster_vip or '?'}",
        "",
    ]
    return lines


def _cluster_members(s: HealthSummary) -> List[str]:
    lines = ["CLUSTER MEMBERS"]
    ch = s.cluster_health

    for member in s.topology.members:
        state  = ch.member_states.get(member.name, "?")
        marker = " (this gateway)" if member.name == s.topology.active_member else ""
        lines.append(
            f"  {member.name} ({member.mgmt_ip or '?'}) — {state}{marker}"
        )
    lines.append("")
    return lines


def _virtual_devices(s: HealthSummary) -> List[str]:
    lines = ["VIRTUAL DEVICES"]

    for vsid_info in s.vsids:
        ncs = s.ncs.get(vsid_info.vsid)
        vt  = vsid_info.vtype

        if vt == "VSX Gateway":
            lines.append(f"  VS0  {vsid_info.name} (VSX Gateway)")
            lines.append(f"        eth0  -> Management")
            lines.append(f"        eth2  -> Cluster Sync")

        elif vt == "Virtual Switch":
            lines.append(f"  VS{vsid_info.vsid}  {vsid_info.name} (Virtual Switch)")
            lines.append(f"        br1[eth3] -> Physical network uplink")
            # WARP junctions from firewall NCS data
            for fw_vs in s.firewall_vsids:
                fw_ncs = s.ncs.get(fw_vs.vsid)
                if fw_ncs and fw_ncs.warp_pairs:
                    wp = fw_ncs.warp_pairs[0]
                    lines.append(
                        f"        br1[{wp.name_b}] -> Junction from {fw_vs.name}"
                    )

        elif vt == "Virtual System":
            lines.append(
                f"  VS{vsid_info.vsid}  {vsid_info.name} (Firewall)"
                f" — Policy: {vsid_info.policy or 'none'}"
            )
            if ncs:
                for iface in ncs.interfaces:
                    if iface.cluster_ip:
                        lines.append(
                            f"        {iface.dev} -> "
                            f"{iface.cluster_ip}/{iface.cluster_mask}"
                        )
                for wp in ncs.warp_pairs:
                    sw_names = [v.name for v in s.switch_vsids]
                    sw = sw_names[0] if sw_names else "VSW"
                    lines.append(
                        f"        {wp.name_a} ({wp.cluster_ip}) "
                        f"--WARP[{wp.name_b}]--> {sw}"
                    )

        elif vt == "Virtual Router":
            lines.append(
                f"  VS{vsid_info.vsid}  {vsid_info.name} (Virtual Router)"
                f" — Policy: {vsid_info.policy or 'none'}"
            )

    lines.append("")
    return lines


def _traffic_flow(s: HealthSummary) -> List[str]:
    if not s.firewall_vsids:
        return []

    sw_names = [v.name for v in s.switch_vsids]
    sw_name  = sw_names[0] if sw_names else "VSW"

    parts = []
    for fw_vs in s.firewall_vsids:
        ncs = s.ncs.get(fw_vs.vsid)
        if s.showncs_available and ncs and ncs.warp_pairs:
            wp = ncs.warp_pairs[0]
            parts.append(f"{fw_vs.name}[{wp.name_a}/{wp.name_b}]")
        else:
            parts.append(fw_vs.name)

    flow = f"  {'  <--VSW-->  '.join(parts)}"

    lines = ["TRAFFIC FLOW", flow, ""]
    if s.showncs_available:
        lines += [
            f"  All firewalls connect to {sw_name} via WARP interface pairs.",
            f"  {sw_name} bridges WARP junctions and eth3 to the physical network.",
            f"  Inter-VS traffic transits the virtual switch at layer 2.",
        ]
    else:
        lines += [
            f"  Firewalls connect to {sw_name} "
            f"(WARP interface names unavailable — vsx showncs not usable).",
        ]
    lines.append("")
    return lines


def _vsid_status_table(s: HealthSummary) -> List[str]:
    hdr = f"  {'VS':<4} {'Name':<18} {'Type':<5} {'SecureXL':<10} " \
          f"{'Mem%':<6} {'Conns/Limit':<14} Blades"
    sep = f"  {'----':<4} {'------------------':<18} {'-----':<5} " \
          f"{'----------':<10} {'------':<6} {'--------------':<14} ------"

    lines = ["PER-VSID STATUS", hdr, sep]

    for vsid_info in s.vsids:
        diag = s.vsid_diags.get(vsid_info.vsid)
        sxl    = diag.securexl.status if diag and not vsid_info.is_switch else "n/a"
        mem    = diag.mem_used_pct    if diag else "n/a"
        conn   = diag.conn_current    if diag else 0
        blades = (diag.enabled_blades or "n/a") if diag else "n/a"
        limit  = vsid_info.conn_limit or 0
        conn_s = f"{conn}/{limit}" if limit else f"{conn}/-"

        lines.append(
            f"  {vsid_info.vsid:<4} {vsid_info.name:<18} "
            f"{vsid_info.short_type:<5} {sxl:<10} {mem:<6} "
            f"{conn_s:<14} {blades[:38]}"
        )

    lines.append("")
    return lines


def _health_section(s: HealthSummary) -> List[str]:
    diag0 = s.vsid_diags.get(0)
    ch    = s.cluster_health

    sync_line = (
        f"  Cluster sync   : {ch.sync_status} (eth2, delta sync)"
        if ch.sync_status else
        "  Cluster sync   : [not available]"
    )
    failover_line = (
        f"  Cluster failover: {ch.failover_count} since last reset"
        if ch.failover_count else
        "  Cluster failover: none since last reset"
    )
    pnote_line = (
        "  PNOTEs         : ISSUES DETECTED"
        if ch.pnote_issues else
        "  PNOTEs         : All OK"
    )

    sxl_ok = all(
        (s.vsid_diags.get(v.vsid) and
         s.vsid_diags[v.vsid].securexl.status in ("enabled", "n/a"))
        for v in s.firewall_vsids
    )
    sxl_line = (
        "  SecureXL       : OK (all firewall VSIDs)"
        if sxl_ok else
        "  SecureXL       : ISSUE (see ATTENTION)"
    )

    idle  = f"{diag0.cpu_idle_pct:.1f}%" if diag0 and diag0.cpu_idle_pct is not None else "?"
    load  = s.platform.load_avg or "?"
    cores = diag0.corexl_instances if diag0 else "?"
    cpu_line = f"  CPU            : {idle} idle, load avg {load}, {cores} CoreXL instances"

    if diag0:
        mem_line = (
            f"  Memory         : {diag0.mem_used_pct} used "
            f"({diag0.mem_used_mb}/{diag0.mem_total_mb} MB), "
            f"swap: {diag0.swap_used_mb} MB"
        )
    else:
        mem_line = "  Memory         : [not collected]"

    ov = s.vsx_overview
    conn_line = f"  Connections    : {ov.total_conn_current}/{ov.total_conn_limit} total"
    disk_line = (
        f"  Disk           : root {s.platform.disk_root_pct}, "
        f"/var/log {s.platform.disk_log_pct}"
    )

    lines = [
        "HEALTH",
        sync_line,
        failover_line,
        pnote_line,
        sxl_line,
        cpu_line,
        mem_line,
        conn_line,
        disk_line,
        "",
    ]
    return lines


def _attention_section(s: HealthSummary) -> List[str]:
    lines = ["ATTENTION"]
    if not s.attention_items:
        lines.append("  No issues detected.")
    else:
        for item in s.attention_items:
            lines.append(f"  [{item.severity}] {item.category}: {item.message}")
    lines.append("")
    return lines


def _footer(s: HealthSummary) -> List[str]:
    return [
        "=" * 62,
        f"  Diagnostics complete — {s.run_timestamp}",
        "=" * 62,
        "",
    ]


# ---------------------------------------------------------------------------
# Full-report helpers
# ---------------------------------------------------------------------------

def _banner(title: str) -> List[str]:
    return ["", "=" * 62, f"  {title}", "=" * 62, ""]


def _section(title: str) -> List[str]:
    return ["", f">> {title}", "-" * 62]


def _vsid_table_raw(s: HealthSummary) -> List[str]:
    hdr = f"  {'VSID':<6} {'Name':<22} {'Type':<18} {'Policy':<24} Conn/Peak/Limit"
    sep = f"  {'------':<6} {'----------------------':<22} {'------------------':<18} " \
          f"{'------------------------':<24} ---------------"
    lines = [hdr, sep]
    for v in s.vsids:
        lines.append(
            f"  {v.vsid:<6} {v.name:<22} {v.vtype:<18} {v.policy:<24} "
            f"{v.conn_current}/{v.conn_peak}/{v.conn_limit}"
        )
    return lines + [""]


def _ncs_topology(s: HealthSummary) -> List[str]:
    if not s.showncs_available:
        return ["  [skipped — vsx showncs not available; run with --fetch (-f)]", ""]

    lines: List[str] = []
    for vsid_info in s.vsids:
        if vsid_info.vsid == 0:
            continue
        ncs = s.ncs.get(vsid_info.vsid)
        if not ncs or not ncs.available:
            lines.append(f"  VSID {vsid_info.vsid} — {vsid_info.name}: [showncs unavailable]")
            continue
        lines += [
            f"  VSID {vsid_info.vsid} — {vsid_info.name} ({vsid_info.vtype})",
            "  " + "-" * 42,
        ]
        if ncs.interfaces:
            lines.append("  Interfaces:")
            for iface in ncs.interfaces:
                if iface.cluster_ip:
                    lines.append(
                        f"    {iface.dev}  local={iface.local_ip}  "
                        f"cluster={iface.cluster_ip}/{iface.cluster_mask}"
                    )
                elif iface.local_ip:
                    lines.append(
                        f"    {iface.dev}  local={iface.local_ip}/{iface.local_mask}"
                    )
        if ncs.warp_pairs:
            lines.append("  WARP Interconnections:")
            for wp in ncs.warp_pairs:
                lines.append(f"    WARP pair: {wp.name_a} <---> {wp.name_b}")
        if ncs.routes:
            lines.append("  Static Routes:")
            for r in ncs.routes:
                if r.gw:
                    lines.append(f"    {r.dest}/{r.mask} via {r.gw}")
                elif r.dev:
                    lines.append(f"    {r.dest}/{r.mask} dev {r.dev}")
        lines.append("")
    return lines
