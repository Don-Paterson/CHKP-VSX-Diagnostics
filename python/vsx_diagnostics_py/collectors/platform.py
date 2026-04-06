"""
collectors/platform.py
Collects platform information from the active cluster member.

collect_platform(session, existing_info) -> PlatformInfo

Commands run (all VS0 context via session.run()):
    fw ver              — CP version string
    cpinfo -y all       — JHF Take number
    uname -r            — kernel version
    uptime              — uptime string and load average
    df -h /             — root filesystem usage %
    df -h /var/log      — log filesystem usage %
    cplic print         — license summary (first 20 lines)

Maps to v18's "Platform Information" section (lines 813-825) plus the
header section version/JHF/uptime collection (lines 142-151).

Note: cp_version, cp_version_short, cp_build and hostname are already
populated by collect_preflight() — we receive an existing PlatformInfo
and fill in the remaining fields rather than starting fresh.
"""

from __future__ import annotations

import re
import logging

from collectors.cpview import collect_cpview
from models.data import PlatformInfo
from transport.ssh import ExpertSession

log = logging.getLogger(__name__)


def collect_platform(
    session: ExpertSession,
    existing: PlatformInfo,
) -> PlatformInfo:
    """
    Populate remaining PlatformInfo fields.

    Parameters
    ----------
    session  : active ExpertSession
    existing : PlatformInfo already partially filled by collect_preflight()
               (hostname, cp_version, cp_version_short, cp_build already set)

    Returns the same object with all remaining fields populated.
    """
    info = existing

    # ----------------------------------------------------------------
    # JHF Take number  (v18 lines 146-147)
    # cpinfo -y all is slow (~10s) — run with generous timeout
    # ----------------------------------------------------------------
    log.info("Platform: collecting JHF take (cpinfo -y all) ...")
    raw_cpinfo = session.run("cpinfo -y all 2>/dev/null", timeout=60)
    info.cpinfo_raw = raw_cpinfo

    # Pattern: "HOTFIX_R82_JUMBO_HF_MAIN ... Take: 91"
    m = re.search(
        r'HOTFIX_R\w+JUMBO_HF_MAIN.*?Take:\s*(\d+)',
        raw_cpinfo,
        re.IGNORECASE,
    )
    if m:
        info.jhf_take = m.group(1)
        log.info("Platform: JHF Take %s", info.jhf_take)
    else:
        log.warning("Platform: JHF take not found in cpinfo output")

    # ----------------------------------------------------------------
    # Kernel version
    # ----------------------------------------------------------------
    log.info("Platform: collecting kernel version ...")
    info.kernel = session.run("uname -r 2>/dev/null").strip()
    log.info("Platform: kernel=%s", info.kernel)

    # ----------------------------------------------------------------
    # Uptime and load average  (v18 lines 150-151)
    # ----------------------------------------------------------------
    log.info("Platform: collecting uptime ...")
    raw_uptime = session.run("uptime 2>/dev/null").strip()
    info.uptime_raw = raw_uptime

    # Extract load average: "load average: 0.12, 0.08, 0.05"
    m = re.search(r'load average:\s*(.+)', raw_uptime, re.IGNORECASE)
    if m:
        info.load_avg = m.group(1).strip()
    log.info("Platform: load_avg=%s", info.load_avg or "?")

    # ----------------------------------------------------------------
    # Disk usage  (v18 lines 821-822)
    # ----------------------------------------------------------------
    log.info("Platform: collecting disk usage ...")
    raw_df_root = session.run("df -h / 2>/dev/null")
    raw_df_log  = session.run("df -h /var/log 2>/dev/null")

    info.disk_root_pct = _parse_df_pct(raw_df_root)
    info.disk_log_pct  = _parse_df_pct(raw_df_log)
    log.info(
        "Platform: disk root=%s  /var/log=%s",
        info.disk_root_pct, info.disk_log_pct,
    )

    # ----------------------------------------------------------------
    # License summary  (v18 line 825: cplic print | head -20)
    # ----------------------------------------------------------------
    log.info("Platform: collecting license info ...")
    raw_cplic = session.run("cplic print 2>&1 | head -20")
    info.cplic_raw = raw_cplic

    # ----------------------------------------------------------------
    # CPView historical CPU data
    # ----------------------------------------------------------------
    log.info("Platform: collecting CPView historical CPU data ...")
    collect_cpview(session, info)
    if info.cpview_available:
        log.info(
            "Platform: CPView 5m=%.1f%% 15m=%s 1h=%s",
            info.cpview_cpu_5m_idle or 0,
            f"{info.cpview_cpu_15m_idle:.1f}%" if info.cpview_cpu_15m_idle is not None else "n/a",
            f"{info.cpview_cpu_1h_idle:.1f}%" if info.cpview_cpu_1h_idle is not None else "n/a",
        )
    else:
        log.info("Platform: CPView not available")

    log.info("Platform: collection complete")
    return info


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _parse_df_pct(raw: str) -> str:
    """
    Extract the Use% column from df -h output.
    Returns e.g. "34%" or "?" if not parseable.
    Mirrors v18: df -h <path> | awk 'NR==2 {print $5}'
    """
    lines = [l for l in raw.splitlines() if l.strip()]
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 5:
            return parts[4]
    return "?"
