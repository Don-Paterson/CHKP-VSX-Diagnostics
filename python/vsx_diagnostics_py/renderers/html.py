"""
renderers/html.py
Generates a self-contained HTML report from a HealthSummary.

render_html(summary, path) -> None

Features:
  - Single file, no external dependencies (all CSS/JS inline)
  - Dark theme matching SmartConsole aesthetic
  - RAG status badges (green/amber/red) on ATTENTION items by severity
  - Collapsible sections for raw diagnostic detail
  - Per-VSID status table
  - Cluster members table with state colouring
  - HCP findings section (if available)
  - Link to local HCP tar.gz archive (if downloaded)
  - Works in any modern browser on A-GUI (Chrome, Edge, Firefox)

Path determined by main.py:
    C:\vsx_diagnostics\vsx_diag_<hostname>_<timestamp>.html
"""

from __future__ import annotations

import html as html_module
import logging
import os
from typing import List, Optional

from models.data import AttentionItem, HealthSummary
from models.member import MemberComparison
from models.snapshot import DeltaItem, DeltaReport

log = logging.getLogger(__name__)

# Severity -> CSS class
_SEV_CLASS = {
    "CRITICAL": "badge-critical",
    "WARNING":  "badge-warning",
    "INFO":     "badge-info",
}

# Member state -> CSS class
_STATE_CLASS = {
    "ACTIVE":   "state-active",
    "STANDBY":  "state-standby",
    "BACKUP":   "state-backup",
    "READY":    "state-ready",
    "DOWN":     "state-down",
}


def render_html(
    summary: HealthSummary,
    path: str,
    delta: Optional[DeltaReport] = None,
) -> None:
    """Write the self-contained HTML report to path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)

    content = _build_html(summary, delta)
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(content)

    size_kb = os.path.getsize(path) / 1024
    log.info("HTML report written: %s (%.1f KB)", path, size_kb)
    print(f"HTML report: {path}")


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def _build_html(s: HealthSummary, delta: Optional[DeltaReport] = None) -> str:
    sections: List[str] = []

    sections.append(_header_section(s))
    sections.append(_environment_section(s))
    sections.append(_cluster_members_section(s))
    sections.append(_vsid_table_section(s))
    sections.append(_health_section(s))
    if delta is not None:
        sections.append(_delta_section(delta))
    if s.member_comparison is not None:
        sections.append(_member_comparison_section(s.member_comparison))
    sections.append(_attention_section(s))
    sections.append(_virtual_devices_section(s))
    sections.append(_hcp_section(s))
    sections.append(_raw_detail_section(s))

    body = "\n".join(sections)
    return _page(title=f"VSX Diagnostics — {s.topology.active_member}", body=body)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _header_section(s: HealthSummary) -> str:
    host    = s.topology.active_member or s.topology.connected_ip or "?"
    version = (
        f"{s.platform.cp_version_short or 'CP'} "
        f"Build {s.platform.cp_build or '?'} "
        f"JHF Take {s.platform.jhf_take or '?'}"
    )
    overall = "HEALTHY" if s.health_ok else "ISSUES DETECTED"
    cls     = "overall-ok" if s.health_ok else "overall-issue"

    return f"""
<div class="page-header">
  <h1>VSX Diagnostics</h1>
  <div class="header-meta">
    <span class="host">{e(host)}</span>
    <span class="version">{e(version)}</span>
    <span class="timestamp">{e(s.run_timestamp)}</span>
  </div>
  <div class="overall {cls}">{overall}</div>
</div>
"""


def _environment_section(s: HealthSummary) -> str:
    t  = s.topology
    ov = s.vsx_overview
    ch = s.cluster_health
    p  = s.platform

    rows = [
        ("CP Version",     f"{p.cp_version_short} Build {p.cp_build} + JHF Take {p.jhf_take or '?'}"),
        ("Cluster Mode",   ch.cluster_mode or "?"),
        ("Members",        str(len(t.members))),
        ("Virtual Systems",f"{len(s.firewall_vsids)} configured / {ov.vs_license_count} licensed"),
        ("Cluster VIP",    t.cluster_vip or "?"),
        ("Management",     t.mgmt_server or "?"),
        ("Kernel",         p.kernel or "?"),
        ("Uptime",         p.uptime_raw or "?"),
        ("Load Average",   p.load_avg or "?"),
        ("Disk /",         p.disk_root_pct or "?"),
        ("Disk /var/log",  p.disk_log_pct or "?"),
        ("Threshold Profile", s.active_profile),
    ]
    rows_html = "\n".join(
        f"<tr><td class='key'>{e(k)}</td><td>{e(v)}</td></tr>"
        for k, v in rows
    )
    return _card("Environment", f"<table class='kv-table'>{rows_html}</table>")


def _cluster_members_section(s: HealthSummary) -> str:
    if not s.topology.members:
        return _card("Cluster Members", "<p class='muted'>No member data available.</p>")

    ch = s.cluster_health
    rows = []
    for m in s.topology.members:
        state = ch.member_states.get(m.name, "?")
        cls   = _STATE_CLASS.get(state.upper(), "state-unknown")
        marker = " ★" if m.name == s.topology.active_member else ""
        rows.append(
            f"<tr>"
            f"<td>{e(m.name)}{e(marker)}</td>"
            f"<td>{e(m.mgmt_ip or '?')}</td>"
            f"<td>{e(m.sync_ip or '?')}</td>"
            f"<td><span class='state-badge {cls}'>{e(state)}</span></td>"
            f"</tr>"
        )
    thead = "<tr><th>Member</th><th>Mgmt IP</th><th>Sync IP</th><th>State</th></tr>"
    table = f"<table class='data-table'><thead>{thead}</thead><tbody>{''.join(rows)}</tbody></table>"
    return _card("Cluster Members", table)


def _vsid_table_section(s: HealthSummary) -> str:
    rows = []
    for v in s.vsids:
        diag  = s.vsid_diags.get(v.vsid)
        sxl   = diag.securexl.status if diag and not v.is_switch else "n/a"
        mem   = diag.mem_used_pct    if diag else "n/a"
        conn  = diag.conn_current    if diag else 0
        limit = v.conn_limit or 0
        conns = f"{conn}/{limit}" if limit else f"{conn}/-"
        blades= (diag.enabled_blades or "n/a") if diag else "n/a"

        sxl_cls = "ok" if sxl == "enabled" else ("muted" if sxl == "n/a" else "warn")
        rows.append(
            f"<tr>"
            f"<td>{v.vsid}</td>"
            f"<td>{e(v.name)}</td>"
            f"<td>{e(v.short_type)}</td>"
            f"<td class='{sxl_cls}'>{e(sxl)}</td>"
            f"<td>{e(mem)}</td>"
            f"<td>{e(conns)}</td>"
            f"<td class='blades'>{e(blades[:60])}</td>"
            f"</tr>"
        )
    thead = ("<tr><th>VSID</th><th>Name</th><th>Type</th>"
             "<th>SecureXL</th><th>Mem%</th><th>Conns/Limit</th><th>Blades</th></tr>")
    table = (f"<table class='data-table'>"
             f"<thead>{thead}</thead><tbody>{''.join(rows)}</tbody></table>")
    return _card("Per-VSID Status", table)


def _health_section(s: HealthSummary) -> str:
    diag0 = s.vsid_diags.get(0)
    ch    = s.cluster_health
    ov    = s.vsx_overview

    def row(label: str, value: str, cls: str = "") -> str:
        return f"<tr><td class='key'>{e(label)}</td><td class='{cls}'>{e(value)}</td></tr>"

    sync_cls  = "ok" if ch.sync_status == "OK" else "warn"
    rows = [
        row("Cluster Sync",    ch.sync_status or "n/a", sync_cls),
        row("Lost Updates",    str(ch.sync_lost_updates),
            "warn" if ch.sync_lost_updates else "ok"),
        row("Failovers",       str(ch.failover_count),
            "warn" if ch.failover_count else "ok"),
        row("PNOTEs",          "Issues" if ch.pnote_issues else "OK",
            "warn" if ch.pnote_issues else "ok"),
        row("CPU Idle",
            f"{diag0.cpu_idle_pct:.1f}%" if diag0 and diag0.cpu_idle_pct is not None else "?",
            "warn" if diag0 and diag0.cpu_idle_pct is not None and diag0.cpu_idle_pct < 50 else "ok"),
        row("Memory (VS0)",
            f"{diag0.mem_used_pct} ({diag0.mem_used_mb}/{diag0.mem_total_mb} MB)" if diag0 else "?"),
        row("Swap (VS0)",
            f"{diag0.swap_used_mb} MB" if diag0 else "?",
            "warn" if diag0 and diag0.swap_used_mb > 100 else "ok"),
        row("Connections",     f"{ov.total_conn_current}/{ov.total_conn_limit}"),
        row("Disk /",          s.platform.disk_root_pct or "?",
            "warn" if _pct_int(s.platform.disk_root_pct or "0") >= 80 else "ok"),
        row("Disk /var/log",   s.platform.disk_log_pct or "?",
            "warn" if _pct_int(s.platform.disk_log_pct or "0") >= 80 else "ok"),
    ]
    table = f"<table class='kv-table'>{''.join(rows)}</table>"
    return _card("Health Indicators", table)


def _attention_section(s: HealthSummary) -> str:
    if not s.attention_items:
        return _card("Attention Items",
                     "<p class='ok-banner'>✓ No issues detected.</p>")

    items_html = "\n".join(_attention_item(a) for a in s.attention_items)
    return _card("Attention Items", f"<div class='attention-list'>{items_html}</div>")


def _attention_item(item: AttentionItem) -> str:
    cls = _SEV_CLASS.get(item.severity, "badge-info")
    return (
        f"<div class='attention-item'>"
        f"<span class='badge {cls}'>{e(item.severity)}</span>"
        f"<span class='cat'>{e(item.category)}</span>"
        f"<span class='msg'>{e(item.message)}</span>"
        f"</div>"
    )


def _virtual_devices_section(s: HealthSummary) -> str:
    lines: List[str] = []
    for v in s.vsids:
        ncs = s.ncs.get(v.vsid)
        lines.append(f"<div class='vdev'>")
        lines.append(
            f"<div class='vdev-header'>"
            f"<span class='vsid-badge'>VS{v.vsid}</span> "
            f"<strong>{e(v.name)}</strong> "
            f"<span class='vtype'>{e(v.short_type)}</span>"
            f"</div>"
        )
        if v.vtype != "VSX Gateway" and ncs and ncs.interfaces:
            lines.append("<ul class='iface-list'>")
            for iface in ncs.interfaces:
                if iface.cluster_ip:
                    lines.append(
                        f"<li>{e(iface.dev)} → "
                        f"{e(iface.cluster_ip)}/{e(iface.cluster_mask)}</li>"
                    )
            if ncs.warp_pairs:
                for wp in ncs.warp_pairs:
                    sw = next((v2.name for v2 in s.switch_vsids), "VSW")
                    lines.append(
                        f"<li class='warp'>{e(wp.name_a)} ({e(wp.cluster_ip)}) "
                        f"⟷ WARP ⟷ {e(wp.name_b)} → {e(sw)}</li>"
                    )
            lines.append("</ul>")
        lines.append("</div>")

    return _card("Virtual Devices", "\n".join(lines))


def _hcp_section(s: HealthSummary) -> str:
    hcp = s.hcp
    if not hcp.ran_ok:
        msg = "hcp not available." if hcp.not_available else \
              "hcp timed out." if hcp.timed_out else \
              "hcp not run."
        return _card("HCP Health Check", f"<p class='muted'>{msg}</p>")

    counts = (
        f"<span class='hcp-count passed'>{len(hcp.passed)} PASSED</span> "
        f"<span class='hcp-count warn'>{len(hcp.errors)} ERROR</span> "
        f"<span class='hcp-count info'>{len(hcp.infos)} INFO</span> "
        f"<span class='hcp-count muted'>{len(hcp.skipped)} SKIPPED</span>"
    )

    detail_blocks = []
    for d in hcp.details:
        cls = "badge-critical" if d.status == "ERROR" else "badge-info"
        finding_pre = (
            f"<pre class='finding'>{e(d.finding)}</pre>" if d.finding else ""
        )
        suggested = (
            f"<p class='suggested'><strong>Suggested:</strong> {e(d.suggested)}</p>"
            if d.suggested else ""
        )
        detail_blocks.append(
            f"<div class='hcp-detail'>"
            f"<div class='hcp-detail-header'>"
            f"<span class='badge {cls}'>{e(d.status)}</span> "
            f"<strong>[VS {d.vsid}] {e(d.test_name)}</strong>"
            f"</div>"
            f"<p class='desc'>{e(d.description)}</p>"
            f"{finding_pre}"
            f"{suggested}"
            f"</div>"
        )

    archive_link = ""
    if hcp.local_archive_path:
        archive_link = (
            f"<p class='archive-link'>📦 HCP report archive: "
            f"<code>{e(hcp.local_archive_path)}</code> "
            f"(extract and open index.html)</p>"
        )

    inner = (
        f"<div class='hcp-counts'>{counts}</div>"
        f"{''.join(detail_blocks)}"
        f"{archive_link}"
    )
    return _card("HCP Health Check", inner, collapsible=False)


def _raw_detail_section(s: HealthSummary) -> str:
    """Collapsible raw data sections for full diagnostic detail."""
    blocks: List[str] = []

    for v in s.vsids:
        diag = s.vsid_diags.get(v.vsid)
        if not diag:
            continue
        content_parts = []
        if diag.cpu_raw:
            content_parts.append(f"<h4>CPU</h4><pre>{e(diag.cpu_raw)}</pre>")
        if diag.route_table:
            content_parts.append(f"<h4>Routing Table</h4><pre>{e(diag.route_table)}</pre>")
        if diag.ip_addr_raw:
            content_parts.append(f"<h4>Interface Addresses</h4><pre>{e(diag.ip_addr_raw)}</pre>")
        if diag.securexl.raw_stat:
            content_parts.append(f"<h4>SecureXL</h4><pre>{e(diag.securexl.raw_stat)}</pre>")
        if diag.conn_table_summary:
            content_parts.append(f"<h4>Connections</h4><pre>{e(diag.conn_table_summary)}</pre>")
        if v.vsid == 0 and diag.corexl_stat:
            content_parts.append(f"<h4>CoreXL</h4><pre>{e(diag.corexl_stat)}</pre>")
        if v.vsid == 0 and diag.affinity_raw:
            content_parts.append(f"<h4>Affinity</h4><pre>{e(diag.affinity_raw)}</pre>")
        if content_parts:
            blocks.append(
                _collapsible(
                    f"VS{v.vsid} — {v.name} ({v.short_type}) Raw Data",
                    "\n".join(content_parts),
                )
            )

    ch = s.cluster_health
    if ch.cphaprob_raw:
        blocks.append(_collapsible(
            "Cluster Health Raw Data",
            f"<h4>cphaprob stat</h4><pre>{e(ch.cphaprob_raw)}</pre>"
            f"<h4>cphaprob syncstat</h4><pre>{e(ch.syncstat_raw)}</pre>"
            f"<h4>cpstat ha -f all</h4><pre>{e(ch.cpstat_ha_raw)}</pre>",
        ))

    if not blocks:
        return ""

    return _card("Raw Diagnostic Data", "\n".join(blocks), collapsible=False)


# ---------------------------------------------------------------------------
# HTML primitives
# ---------------------------------------------------------------------------

def e(text: str) -> str:
    """HTML-escape a string."""
    return html_module.escape(str(text))


def _member_comparison_section(mc: MemberComparison) -> str:
    """Render the all-member comparison card."""
    reachable = [s for s in mc.snapshots if s.reachable]
    total     = len(mc.snapshots)

    # ── Header line ───────────────────────────────────────────────────
    meta_parts = [
        f"<span class='mc-meta-item'>"
        f"<strong>{len(reachable)}</strong> of <strong>{total}</strong> members reachable"
        f"</span>"
    ]
    if mc.unreachable:
        meta_parts.append(
            f"<span class='mc-meta-item mc-warn'>"
            f"&#9888; Unreachable: {e(', '.join(mc.unreachable))}"
            f"</span>"
        )
    meta_html = "<div class='mc-meta'>" + "".join(meta_parts) + "</div>"

    if len(reachable) < 2:
        body = (meta_html +
                "<p class='mc-note'>Fewer than 2 members reachable "
                "— cross-member comparison skipped.</p>")
        return _card("All-Member Comparison", body)

    # ── Per-member summary table ───────────────────────────────────────
    thead = (
        "<thead><tr>"
        "<th>Member</th><th>State</th><th>Ver</th><th>JHF</th>"
        "<th>CPU idle%</th><th>Root%</th><th>Log%</th>"
        "<th>Sync</th><th>Failovers</th><th>Load Avg</th>"
        "</tr></thead>"
    )
    rows = []
    for snap in sorted(reachable, key=lambda s: s.name):
        state_cls = {
            "ACTIVE":  "state-active",
            "STANDBY": "state-standby",
            "BACKUP":  "state-backup",
            "READY":   "state-ready",
            "DOWN":    "state-down",
        }.get(snap.own_state, "")
        cpu = f"{snap.cpu_idle_pct:.1f}%" if snap.cpu_idle_pct is not None else "n/a"
        sync_cls = "" if snap.sync_status == "OK" else "mc-warn-cell"
        rows.append(
            f"<tr>"
            f"<td><strong>{e(snap.name)}</strong></td>"
            f"<td><span class='badge {state_cls}'>{e(snap.own_state or '?')}</span></td>"
            f"<td>{e(snap.cp_version_short)}</td>"
            f"<td>{e(snap.jhf_take)}</td>"
            f"<td>{e(cpu)}</td>"
            f"<td>{snap.disk_root_pct}%</td>"
            f"<td>{snap.disk_log_pct}%</td>"
            f"<td class='{sync_cls}'>{e(snap.sync_status)}</td>"
            f"<td>{snap.failover_count}</td>"
            f"<td>{e(snap.load_avg)}</td>"
            f"</tr>"
        )
    summary_table = (
        f"<table class='mc-table'>{thead}"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )

    # ── Differences table ─────────────────────────────────────────────
    diff_html = ""
    if mc.diffs:
        diff_rows = []
        for diff in mc.diffs:
            flag_cls = "mc-diff-flagged" if diff.flagged else "mc-diff-info"
            icon = "&#9888;" if diff.flagged else "&#8505;"
            vals_html = " &nbsp;|&nbsp; ".join(
                f"<span class='mc-member-name'>{e(m)}</span>: {e(v)}"
                for m, v in sorted(diff.member_values.items())
            )
            note_html = (
                f"<br><small class='mc-note-text'>{e(diff.note)}</small>"
                if diff.note else ""
            )
            diff_rows.append(
                f"<tr class='{flag_cls}'>"
                f"<td class='mc-diff-icon'>{icon}</td>"
                f"<td class='mc-diff-metric'>{e(diff.metric)}</td>"
                f"<td class='mc-diff-vals'>{vals_html}{note_html}</td>"
                f"</tr>"
            )
        diff_html = (
            "<h4 style='margin:14px 0 6px'>Differences</h4>"
            "<table class='mc-diff-table'>"
            "<thead><tr><th></th><th>Metric</th><th>Values</th></tr></thead>"
            f"<tbody>{''.join(diff_rows)}</tbody>"
            "</table>"
        )
    else:
        diff_html = "<p class='mc-ok'>&#10003; All members consistent — no differences detected.</p>"

    body = meta_html + summary_table + diff_html
    return _card("All-Member Comparison", body)


def _delta_section(delta: DeltaReport) -> str:
    """Render the full delta comparison card."""
    elapsed_str = _fmt_elapsed(delta.elapsed_seconds)

    meta_parts = [
        f"<span class='delta-meta-item'>Previous run: <strong>{e(delta.prev_run_id)}</strong></span>",
        f"<span class='delta-meta-item'>Elapsed: <strong>{e(elapsed_str)}</strong></span>",
    ]
    if delta.different_members:
        meta_parts.append(
            f"<span class='delta-meta-item delta-note'>"
            f"&#9432; Member changed: {e(delta.prev_member)} &rarr; {e(delta.curr_member)}"
            f" &mdash; failover counts may differ"
            f"</span>"
        )
    if delta.suppressed:
        meta_parts.append(
            "<span class='delta-meta-item delta-note'>"
            "&#9432; Runs &lt;2 min apart &mdash; threshold flags suppressed"
            "</span>"
        )
    meta_html = "<div class='delta-meta'>" + "".join(meta_parts) + "</div>"

    if not delta.has_changes:
        body = meta_html + "<p class='delta-nochange'>&#10003; No changes detected since previous run.</p>"
        return _card("Delta Comparison", body)

    rows: List[str] = []

    # ── Cluster ───────────────────────────────────────────────────────
    rows += _delta_group_header("Cluster")
    rows += _delta_item_row("Failover count",    delta.failover_count)
    rows += _delta_item_row("Sync status",       delta.sync_status)
    rows += _delta_item_row("Sync lost updates", delta.sync_lost_updates)
    for member, item in sorted(delta.member_states.items()):
        rows += _delta_item_row(f"{member} state", item)

    # ── Platform ──────────────────────────────────────────────────────
    rows += _delta_group_header("Platform")
    rows += _delta_item_row("CPU idle %",   delta.cpu_idle_pct,  fmt="{:.1f}%")
    rows += _delta_item_row("Swap used",    delta.swap_used_mb,  fmt="{} MB")
    rows += _delta_item_row("Root disk %",  delta.disk_root_pct, fmt="{}%")
    rows += _delta_item_row("Log disk %",   delta.disk_log_pct,  fmt="{}%")

    # ── Connections ────────────────────────────────────────────────────
    rows += _delta_group_header("Connections")
    rows += _delta_item_row("Total connections", delta.total_conn_current)

    # ── Per-VSID ──────────────────────────────────────────────────────
    any_vsid_change = any(vd.has_changes for vd in delta.vsid_deltas.values())
    if any_vsid_change:
        rows += _delta_group_header("Per-VSID")
        for vsid_int, vd in sorted(delta.vsid_deltas.items()):
            if not vd.has_changes:
                continue
            label = f"VSID {vsid_int} ({e(vd.name)})"
            rows += _delta_item_row(f"{label} &mdash; conn count",  vd.conn_current)
            rows += _delta_item_row(f"{label} &mdash; conn %",      vd.conn_pct, fmt="{:.1f}%")
            rows += _delta_item_row(f"{label} &mdash; SecureXL",    vd.securexl_status)
            for ie in vd.iface_error_deltas:
                cls = "delta-flagged" if ie.flagged else "delta-changed"
                flag_icon = " &#9888;" if ie.flagged else ""
                rows.append(
                    f"<tr class='{cls}'>"
                    f"<td class='delta-label'>{label} &mdash; {e(ie.dev)} {e(ie.direction)}</td>"
                    f"<td class='delta-prev'>{ie.prev_errors} err / {ie.prev_drops} drop</td>"
                    f"<td class='delta-arrow'>&rarr;</td>"
                    f"<td class='delta-curr'>{ie.curr_errors} err / {ie.curr_drops} drop</td>"
                    f"<td class='delta-change'>"
                    f"+{ie.delta_errors} err, +{ie.delta_drops} drop{flag_icon}"
                    f"</td>"
                    f"</tr>"
                )

    # ── PNOTE changes ─────────────────────────────────────────────────
    if delta.new_pnotes or delta.resolved_pnotes or delta.changed_pnotes:
        rows += _delta_group_header("PNOTE Changes")
        for p in delta.new_pnotes:
            rows.append(
                f"<tr class='delta-flagged'>"
                f"<td class='delta-label'>{e(p.get('name','?'))}</td>"
                f"<td colspan='3' class='delta-curr'>NEW &mdash; {e(p.get('status','?'))}</td>"
                f"<td class='delta-change'>&#9888; new issue</td>"
                f"</tr>"
            )
        for p in delta.resolved_pnotes:
            rows.append(
                f"<tr class='delta-resolved'>"
                f"<td class='delta-label'>{e(p.get('name','?'))}</td>"
                f"<td colspan='3' class='delta-prev'>RESOLVED</td>"
                f"<td class='delta-change'>&#10003; cleared</td>"
                f"</tr>"
            )
        for p in delta.changed_pnotes:
            rows.append(
                f"<tr class='delta-changed'>"
                f"<td class='delta-label'>{e(p.get('name','?'))}</td>"
                f"<td class='delta-prev'>{e(p.get('prev_status','?'))}</td>"
                f"<td class='delta-arrow'>&rarr;</td>"
                f"<td class='delta-curr'>{e(p.get('curr_status','?'))}</td>"
                f"<td class='delta-change'>status changed</td>"
                f"</tr>"
            )

    # ── HCP changes ───────────────────────────────────────────────────
    if delta.new_hcp_issues or delta.resolved_hcp_issues:
        rows += _delta_group_header("HCP Status Changes")
        for h in delta.new_hcp_issues:
            rows.append(
                f"<tr class='delta-flagged'>"
                f"<td class='delta-label'>VS{h.get('vsid','?')} {e(h.get('test_name','?'))}</td>"
                f"<td class='delta-prev'>{e(h.get('prev_status','?'))}</td>"
                f"<td class='delta-arrow'>&rarr;</td>"
                f"<td class='delta-curr'>{e(h.get('curr_status','?'))}</td>"
                f"<td class='delta-change'>&#9888; new issue</td>"
                f"</tr>"
            )
        for h in delta.resolved_hcp_issues:
            rows.append(
                f"<tr class='delta-resolved'>"
                f"<td class='delta-label'>VS{h.get('vsid','?')} {e(h.get('test_name','?'))}</td>"
                f"<td class='delta-prev'>{e(h.get('prev_status','?'))}</td>"
                f"<td class='delta-arrow'>&rarr;</td>"
                f"<td class='delta-curr'>{e(h.get('curr_status','?'))}</td>"
                f"<td class='delta-change'>&#10003; cleared</td>"
                f"</tr>"
            )

    table = (
        "<table class='delta-table'>"
        "<thead><tr>"
        "<th>Metric</th><th>Previous</th><th></th><th>Current</th><th>Change</th>"
        "</tr></thead>"
        "<tbody>" + "\n".join(rows) + "</tbody>"
        "</table>"
    )

    body = meta_html + table
    return _card("Delta Comparison", body)


def _delta_group_header(title: str) -> List[str]:
    return [
        f"<tr class='delta-group-header'>"
        f"<td colspan='5'>{e(title)}</td>"
        f"</tr>"
    ]


def _fmt_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def _delta_item_row(label: str, item: DeltaItem, fmt: str = "{}") -> List[str]:
    """Render one DeltaItem as a table row. Returns [] if unchanged/n/a."""
    if item.direction in ("n/a", "unchanged"):
        return []

    cls = "delta-flagged" if item.flagged else "delta-changed"
    flag_icon = " &#9888;" if item.flagged else ""

    if item.direction == "reset":
        prev_s = str(item.prev)
        curr_s = str(item.curr)
        change_s = "&#9888; counter reset (reboot?)"
        cls = "delta-flagged"
    elif item.direction in ("new", "gone"):
        prev_s = "&mdash;" if item.direction == "new" else str(item.prev)
        curr_s = str(item.curr) if item.direction == "new" else "&mdash;"
        change_s = f"[{item.direction}]"
    elif item.delta is not None:
        try:
            prev_s = fmt.format(item.prev)
            curr_s = fmt.format(item.curr)
            sign = "+" if item.delta >= 0 else ""
            change_s = f"{sign}{fmt.format(item.delta)}{flag_icon}"
        except (TypeError, ValueError):
            prev_s, curr_s = str(item.prev), str(item.curr)
            change_s = str(item.delta)
    else:
        prev_s = str(item.prev)
        curr_s = str(item.curr)
        change_s = "changed" + flag_icon

    reason_html = ""
    if item.flagged and item.flag_reason:
        reason_html = f"<br><small class='delta-reason'>{e(item.flag_reason)}</small>"

    return [
        f"<tr class='{cls}'>"
        f"<td class='delta-label'>{label}</td>"
        f"<td class='delta-prev'>{prev_s}</td>"
        f"<td class='delta-arrow'>&rarr;</td>"
        f"<td class='delta-curr'>{curr_s}</td>"
        f"<td class='delta-change'>{change_s}{reason_html}</td>"
        f"</tr>"
    ]


def _card(title: str, content: str, collapsible: bool = False) -> str:
    if collapsible:
        inner = _collapsible(title, content)
        return f"<div class='card'>{inner}</div>"
    return (
        f"<div class='card'>"
        f"<h2 class='card-title'>{e(title)}</h2>"
        f"<div class='card-body'>{content}</div>"
        f"</div>"
    )


def _collapsible(title: str, content: str) -> str:
    # Use a unique ID based on title for the checkbox toggle
    uid = title.lower().replace(" ", "-").replace("/", "-")[:40]
    return (
        f"<div class='collapsible'>"
        f"<input type='checkbox' id='c-{uid}' class='toggle'>"
        f"<label for='c-{uid}' class='toggle-label'>{e(title)}</label>"
        f"<div class='toggle-content'>{content}</div>"
        f"</div>"
    )


def _pct_int(s: str) -> int:
    try:
        return int(s.strip().rstrip('%'))
    except (ValueError, AttributeError):
        return 0


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{e(title)}</title>
<style>
/* ---- Reset & base ---- */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  font-size: 14px;
  background: #1a1a2e;
  color: #e0e0e0;
  line-height: 1.5;
}}
a {{ color: #7eb8f7; }}

/* ---- Layout ---- */
.container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}

/* ---- Page header ---- */
.page-header {{
  background: #16213e;
  border-bottom: 2px solid #0f3460;
  padding: 20px 24px;
  margin-bottom: 20px;
  border-radius: 8px;
}}
.page-header h1 {{ font-size: 1.6rem; color: #7eb8f7; margin-bottom: 6px; }}
.header-meta {{ display: flex; gap: 20px; color: #888; font-size: 0.85rem; margin-bottom: 10px; }}
.overall {{ display: inline-block; padding: 6px 16px; border-radius: 4px;
           font-weight: 700; font-size: 1rem; letter-spacing: 0.05em; }}
.overall-ok    {{ background: #1a4731; color: #4caf84; border: 1px solid #4caf84; }}
.overall-issue {{ background: #4a1a1a; color: #f44336; border: 1px solid #f44336; }}

/* ---- Cards ---- */
.card {{
  background: #16213e;
  border: 1px solid #0f3460;
  border-radius: 8px;
  margin-bottom: 16px;
  overflow: hidden;
}}
.card-title {{
  font-size: 1rem;
  font-weight: 600;
  color: #7eb8f7;
  padding: 12px 16px;
  background: #0f3460;
  border-bottom: 1px solid #1a4a80;
}}
.card-body {{ padding: 16px; }}

/* ---- Tables ---- */
.kv-table, .data-table {{ width: 100%; border-collapse: collapse; }}
.kv-table td, .data-table td, .data-table th {{
  padding: 6px 12px;
  border-bottom: 1px solid #0f3460;
  vertical-align: top;
}}
.data-table th {{
  background: #0f3460;
  color: #7eb8f7;
  font-weight: 600;
  text-align: left;
}}
.kv-table td.key {{
  color: #888;
  width: 180px;
  font-size: 0.85rem;
}}
td.ok    {{ color: #4caf84; }}
td.warn  {{ color: #ffa726; }}
td.muted {{ color: #666; }}
td.blades {{ font-size: 0.8rem; color: #aaa; }}

/* ---- Badges ---- */
.badge {{
  display: inline-block;
  padding: 2px 8px;
  border-radius: 3px;
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  margin-right: 6px;
}}
.badge-critical {{ background: #4a1a1a; color: #f44336; border: 1px solid #f44336; }}
.badge-warning  {{ background: #3d2a00; color: #ffa726; border: 1px solid #ffa726; }}
.badge-info     {{ background: #0d2d4a; color: #7eb8f7; border: 1px solid #7eb8f7; }}

/* ---- State badges ---- */
.state-badge {{
  display: inline-block;
  padding: 2px 10px;
  border-radius: 3px;
  font-size: 0.8rem;
  font-weight: 600;
}}
.state-active  {{ background: #1a4731; color: #4caf84; }}
.state-standby {{ background: #1a2a4a; color: #7eb8f7; }}
.state-backup  {{ background: #2a2a00; color: #ffeb3b; }}
.state-ready   {{ background: #1a3a2a; color: #81c784; }}
.state-down    {{ background: #4a1a1a; color: #f44336; }}
.state-unknown {{ background: #2a2a2a; color: #888; }}

/* ---- Attention items ---- */
.attention-list {{ display: flex; flex-direction: column; gap: 8px; }}
.attention-item {{
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 12px;
  background: #0f1a2e;
  border-radius: 4px;
  border-left: 3px solid #333;
}}
.attention-item .cat  {{ color: #888; font-size: 0.85rem; min-width: 130px; }}
.attention-item .msg  {{ color: #e0e0e0; flex: 1; }}
.ok-banner {{
  color: #4caf84;
  font-size: 1.1rem;
  padding: 12px;
  text-align: center;
}}

/* ---- Virtual devices ---- */
.vdev {{ margin-bottom: 12px; padding: 10px 14px; background: #0f1a2e; border-radius: 4px; }}
.vdev-header {{ margin-bottom: 6px; }}
.vsid-badge {{
  display: inline-block;
  background: #0f3460;
  color: #7eb8f7;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 0.8rem;
  margin-right: 6px;
}}
.vtype {{ color: #666; font-size: 0.85rem; }}
.iface-list {{ list-style: none; padding-left: 20px; font-size: 0.85rem; color: #aaa; }}
.iface-list li {{ padding: 1px 0; }}
.iface-list .warp {{ color: #7eb8f7; }}

/* ---- HCP ---- */
.hcp-counts {{ margin-bottom: 12px; }}
.hcp-count {{ margin-right: 12px; font-size: 0.85rem; }}
.hcp-count.passed {{ color: #4caf84; }}
.hcp-count.warn   {{ color: #ffa726; }}
.hcp-count.info   {{ color: #7eb8f7; }}
.hcp-count.muted  {{ color: #666; }}
.hcp-detail {{
  margin-bottom: 12px;
  padding: 10px 14px;
  background: #0f1a2e;
  border-radius: 4px;
  border-left: 3px solid #0f3460;
}}
.hcp-detail-header {{ margin-bottom: 6px; }}
.desc {{ color: #888; font-size: 0.85rem; margin-bottom: 6px; }}
.finding {{
  background: #0a0f1e;
  border: 1px solid #0f3460;
  padding: 8px;
  font-size: 0.8rem;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  margin-bottom: 6px;
  border-radius: 3px;
}}
.suggested {{ font-size: 0.85rem; color: #aaa; }}
.archive-link {{
  margin-top: 12px;
  font-size: 0.85rem;
  color: #7eb8f7;
  padding: 8px 12px;
  background: #0f1a2e;
  border-radius: 4px;
}}

/* ---- All-member comparison ---- */
.mc-meta {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; align-items: center; }}
.mc-meta-item {{ font-size: 0.85rem; color: #aaa; }}
.mc-meta-item strong {{ color: #e0e0e0; }}
.mc-warn {{ color: #ffa726; }}
.mc-ok {{ color: #4caf84; padding: 6px 0; }}
.mc-note {{ color: #888; font-size: 0.85rem; padding: 6px 0; }}
.mc-table {{ width: 100%; border-collapse: collapse; font-size: 0.83rem; margin-bottom: 14px; }}
.mc-table thead th {{
  text-align: left; padding: 6px 10px;
  background: #0f2040; color: #7eb8f7;
  border-bottom: 1px solid #0f3460;
}}
.mc-table td {{ padding: 5px 10px; border-bottom: 1px solid #0f1a2e; }}
.mc-warn-cell {{ color: #ffa726; font-weight: 600; }}
.mc-diff-table {{ width: 100%; border-collapse: collapse; font-size: 0.83rem; }}
.mc-diff-table thead th {{
  text-align: left; padding: 5px 10px;
  background: #0a1628; color: #555;
  font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em;
  border-bottom: 1px solid #0f1a2e;
}}
.mc-diff-table td {{ padding: 5px 10px; border-bottom: 1px solid #0f1a2e; vertical-align: top; }}
.mc-diff-flagged {{ background: rgba(255,167,38,0.07); }}
.mc-diff-flagged .mc-diff-icon {{ color: #ffa726; }}
.mc-diff-flagged .mc-diff-metric {{ color: #ffa726; font-weight: 600; }}
.mc-diff-info .mc-diff-icon {{ color: #7eb8f7; }}
.mc-diff-info .mc-diff-metric {{ color: #7eb8f7; }}
.mc-diff-icon {{ width: 22px; text-align: center; }}
.mc-diff-metric {{ width: 28%; }}
.mc-diff-vals {{ color: #ccc; }}
.mc-member-name {{ color: #7eb8f7; font-weight: 600; }}
.mc-note-text {{ color: #888; }}

/* ---- Delta comparison ---- */
.delta-meta {{ margin-bottom: 12px; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }}
.delta-meta-item {{ font-size: 0.85rem; color: #aaa; }}
.delta-meta-item strong {{ color: #e0e0e0; }}
.delta-note {{ color: #ffa726; }}
.delta-nochange {{ color: #4caf84; padding: 8px 0; }}
.delta-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
.delta-table thead th {{
  text-align: left; padding: 6px 10px;
  background: #0f2040; color: #7eb8f7;
  border-bottom: 1px solid #0f3460;
}}
.delta-group-header td {{
  background: #0a1628; color: #555;
  font-size: 0.78rem; font-weight: 600; letter-spacing: 0.08em;
  text-transform: uppercase; padding: 8px 10px 4px;
}}
.delta-table td {{ padding: 5px 10px; border-bottom: 1px solid #0f1a2e; vertical-align: top; }}
.delta-label {{ color: #bbb; width: 34%; }}
.delta-prev {{ color: #888; width: 16%; }}
.delta-arrow {{ color: #555; width: 4%; text-align: center; }}
.delta-curr {{ color: #e0e0e0; width: 16%; }}
.delta-change {{ width: 30%; }}
.delta-changed .delta-change {{ color: #7eb8f7; }}
.delta-flagged {{ background: rgba(255,167,38,0.07); }}
.delta-flagged .delta-change {{ color: #ffa726; font-weight: 600; }}
.delta-resolved .delta-change {{ color: #4caf84; }}
.delta-reason {{ color: #888; font-size: 0.78rem; display: block; margin-top: 2px; }}

/* ---- Collapsible sections ---- */
.toggle {{ display: none; }}
.toggle-label {{
  display: block;
  padding: 10px 16px;
  background: #0f2040;
  cursor: pointer;
  font-weight: 600;
  color: #7eb8f7;
  border-bottom: 1px solid #0f3460;
  user-select: none;
}}
.toggle-label::before {{ content: '▶ '; font-size: 0.75rem; }}
.toggle:checked + .toggle-label::before {{ content: '▼ '; }}
.toggle-content {{ display: none; padding: 12px 16px; }}
.toggle:checked ~ .toggle-content {{ display: block; }}

/* ---- Pre / code ---- */
pre {{
  background: #0a0f1e;
  border: 1px solid #0f3460;
  padding: 10px;
  font-size: 0.78rem;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  border-radius: 3px;
  color: #ccc;
}}
h4 {{ color: #7eb8f7; margin: 10px 0 4px; font-size: 0.9rem; }}
</style>
</head>
<body>
<div class="container">
{body}
</div>
</body>
</html>"""
