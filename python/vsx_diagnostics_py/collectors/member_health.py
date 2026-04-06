"""
collectors/member_health.py
Collects health data from one cluster member via a direct SSH connection.

collect_member_health(session, name, ip) -> MemberSnapshot

This is a targeted, fast collection — not the full diagnostic suite.
It runs only the commands needed for cross-member comparison:
  - cphaprob stat       (member's view of cluster states + failover count)
  - cphaprob syncstat   (sync status from this member's perspective)
  - fw ver              (version — should match; flag if not)
  - mpstat 1 1          (1-second CPU sample in VS0)
  - free -m             (swap)
  - uptime              (load average)
  - df -h / and /var/log (disk — can diverge between members)
  - fw ctl multik stat  (CoreXL instance count — should match)
  - ip -s link          (interface error counters in VS0)

HCP is NOT run per-member (too slow; already collected from primary).
Per-VSID diagnostics are NOT run per-member (connections/SecureXL are
cluster-wide and already collected from the active member).

connect_and_collect(ip, name, username, password, expert_password,
                    port, timeout) -> MemberSnapshot
    Opens its own SSH connection, collects, closes.  Never raises —
    all failures produce a MemberSnapshot with reachable=False.

collect_all_members(primary_session, primary_name, hosts_cfg,
                    topology, username, password, expert_password,
                    port, timeout) -> List[MemberSnapshot]
    Reuses the existing primary session for the first member, then
    opens fresh connections to the remaining reachable members.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from models.member import MemberIfaceError, MemberSnapshot
from parsers.cphaprob import parse_cphaprob_stat, parse_cphaprob_syncstat
from transport.ssh import ExpertSession, SSHError, _connect

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Single-member collection
# ---------------------------------------------------------------------------

def collect_member_health(
    session: ExpertSession,
    name: str,
    ip: str,
) -> MemberSnapshot:
    """
    Collect targeted health data from an already-open ExpertSession.
    Returns a MemberSnapshot.  Never raises.
    """
    snap = MemberSnapshot(name=name, ip=ip, reachable=True)

    try:
        # ── Version ───────────────────────────────────────────────────
        raw_ver = session.run("fw ver 2>/dev/null | head -2")
        snap.cp_version_short = _parse_version_short(raw_ver)
        snap.jhf_take = _parse_jhf_quick(session)

        # ── Cluster state ─────────────────────────────────────────────
        raw_stat = session.run("cphaprob stat 2>&1")
        snap.cphaprob_raw = raw_stat
        parsed = parse_cphaprob_stat(raw_stat)
        snap.member_states  = parsed["member_states"]
        snap.failover_count = parsed["failover_count"]
        # Derive this member's own state from its own view
        snap.own_state = snap.member_states.get(name, "")

        # ── Sync ──────────────────────────────────────────────────────
        raw_sync = session.run("cphaprob syncstat 2>&1")
        snap.syncstat_raw = raw_sync
        parsed_sync = parse_cphaprob_syncstat(raw_sync)
        snap.sync_status       = parsed_sync["sync_status"]
        snap.sync_lost_updates = parsed_sync["sync_lost_updates"]

        # ── Uptime / load ─────────────────────────────────────────────
        raw_uptime = session.run("uptime 2>/dev/null").strip()
        snap.uptime_raw = raw_uptime
        m = re.search(r'load average:\s*(.+)', raw_uptime, re.IGNORECASE)
        if m:
            snap.load_avg = m.group(1).strip()

        # ── Disk ──────────────────────────────────────────────────────
        snap.disk_root_pct = _parse_df_pct(session.run("df -h / 2>/dev/null"))
        snap.disk_log_pct  = _parse_df_pct(session.run("df -h /var/log 2>/dev/null"))

        # ── CPU (VS0 mpstat) ──────────────────────────────────────────
        raw_cpu = session.run("mpstat 1 1 2>/dev/null | tail -2")
        snap.cpu_idle_pct = _parse_cpu_idle(raw_cpu)

        # ── Swap ──────────────────────────────────────────────────────
        raw_free = session.run("free -m 2>/dev/null")
        snap.swap_used_mb = _parse_swap(raw_free)

        # ── CoreXL ────────────────────────────────────────────────────
        raw_multik = session.run("fw ctl multik stat 2>/dev/null")
        snap.corexl_instances = _parse_corexl(raw_multik)

        # ── Interface errors (VS0) ─────────────────────────────────────
        raw_link = session.run("ip -s link 2>/dev/null")
        snap.iface_errors = _parse_iface_errors(raw_link)

    except Exception as exc:
        log.warning("collect_member_health(%s/%s) failed partway: %s", name, ip, exc)
        snap.error_msg = str(exc)

    return snap


# ---------------------------------------------------------------------------
# Connect-and-collect for secondary members
# ---------------------------------------------------------------------------

def connect_and_collect(
    ip: str,
    name: str,
    username: str,
    password: str,
    expert_password: str,
    port: int = 22,
    timeout: int = 15,
) -> MemberSnapshot:
    """
    Open a fresh SSH connection to ip, collect health data, close.
    Never raises — returns MemberSnapshot(reachable=False) on any error.
    """
    snap = MemberSnapshot(name=name, ip=ip, reachable=False)
    session = None
    try:
        log.info("Member health: connecting to %s (%s) ...", name, ip)
        session = _connect(
            host=ip,
            username=username,
            password=password,
            expert_password=expert_password,
            port=port,
            timeout=timeout,
        )
        snap = collect_member_health(session, name=name, ip=ip)
        log.info("Member health: collected from %s (%s)", name, ip)
    except SSHError as e:
        log.warning("Member health: cannot reach %s (%s): %s", name, ip, e)
        snap.error_msg = str(e)
    except Exception as e:
        log.warning("Member health: unexpected error from %s (%s): %s", name, ip, e)
        snap.error_msg = str(e)
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    return snap


def collect_all_members(
    primary_session: ExpertSession,
    primary_name: str,
    topology_members,       # List[ClusterMember]
    username: str,
    password: str,
    expert_password: str,
    port: int = 22,
    timeout: int = 15,
) -> List[MemberSnapshot]:
    """
    Collect MemberSnapshot from every cluster member.

    The primary session (already open, already used for the main collection)
    is reused for the member we are already connected to — no second connect.
    Fresh connections are opened for all other members.

    Parameters
    ----------
    primary_session  : the ExpertSession opened at the start of the run
    primary_name     : hostname of the primary member (topology.active_member)
    topology_members : list of ClusterMember from topology collection
                       (provides name -> mgmt_ip mapping)
    username / password / expert_password : SSH credentials
    port / timeout   : SSH connection parameters

    Returns list of MemberSnapshot, one per cluster member, in member name order.
    """
    # Build name->ip mapping from topology
    ip_by_name: Dict[str, str] = {}
    for m in topology_members:
        if m.name and m.mgmt_ip:
            ip_by_name[m.name] = m.mgmt_ip

    snapshots: List[MemberSnapshot] = []

    for member in sorted(topology_members, key=lambda m: m.name):
        if not member.name:
            continue

        if member.name == primary_name:
            # Reuse existing session — no reconnect needed
            log.info("Member health: collecting from primary member %s (reusing session)", member.name)
            snap = collect_member_health(primary_session, name=member.name, ip=member.mgmt_ip or primary_session.connected_ip)
        else:
            ip = member.mgmt_ip
            if not ip:
                log.warning("Member health: no IP for %s — skipping", member.name)
                snap = MemberSnapshot(name=member.name, ip="", reachable=False,
                                      error_msg="IP not found in topology")
            else:
                snap = connect_and_collect(
                    ip=ip,
                    name=member.name,
                    username=username,
                    password=password,
                    expert_password=expert_password,
                    port=port,
                    timeout=timeout,
                )

        snapshots.append(snap)

    reachable = sum(1 for s in snapshots if s.reachable)
    log.info(
        "Member health: collected from %d/%d members (%s unreachable)",
        reachable, len(snapshots),
        ", ".join(s.name for s in snapshots if not s.reachable) or "none",
    )
    return snapshots


# ---------------------------------------------------------------------------
# Parsing helpers (member_health only — not shared with main collectors)
# ---------------------------------------------------------------------------

def _parse_version_short(raw: str) -> str:
    """Extract 'R82' from fw ver output."""
    m = re.search(r'\bR(\d+(?:\.\d+)?)\b', raw)
    return f"R{m.group(1)}" if m else ""


def _parse_jhf_quick(session: ExpertSession) -> str:
    """
    Fast JHF take extraction using cpinfo -y all output.
    Returns "" if not found (avoids the full 10s wait — uses a grep shortcut).
    """
    raw = session.run(
        "cpinfo -y all 2>/dev/null | grep -i 'JUMBO_HF_MAIN' | tail -1",
        timeout=60,
    )
    m = re.search(r'Take:\s*(\d+)', raw, re.IGNORECASE)
    return m.group(1) if m else ""


def _parse_df_pct(raw: str) -> int:
    """Extract Use% from df -h output. Returns 0 on failure."""
    lines = [l for l in raw.splitlines() if l.strip()]
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 5:
            try:
                return int(parts[4].rstrip("%"))
            except ValueError:
                pass
    return 0


def _parse_cpu_idle(raw: str) -> Optional[float]:
    """
    Extract %idle from mpstat 1 1 output.
    Returns None if not parseable.
    """
    for line in reversed(raw.splitlines()):
        parts = line.split()
        # mpstat output: ... %usr %nice %sys %iowait %irq %soft %steal %guest %gnice %idle
        if len(parts) >= 12 and parts[0] not in ("Linux", "CPU", "Average:"):
            try:
                return round(float(parts[-1]), 1)
            except ValueError:
                pass
        # Handle "Average:" line format
        if parts and parts[0] == "Average:":
            try:
                return round(float(parts[-1]), 1)
            except (ValueError, IndexError):
                pass
    return None


def _parse_swap(raw: str) -> int:
    """Extract swap used MB from free -m output."""
    for line in raw.splitlines():
        if line.startswith("Swap:"):
            parts = line.split()
            try:
                return int(parts[2])
            except (IndexError, ValueError):
                pass
    return 0


def _parse_corexl(raw: str) -> int:
    """Extract CoreXL instance count from fw ctl multik stat."""
    m = re.search(r'(\d+)\s+(?:CoreXL\s+)?(?:firewall\s+)?instance', raw, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Fallback: count "ID" lines
    ids = re.findall(r'^\s*ID\s+\d+', raw, re.MULTILINE)
    return len(ids) if ids else 0


def _parse_iface_errors(raw: str) -> List[MemberIfaceError]:
    """
    Parse 'ip -s link' output and return non-zero error/drop entries.
    Format (per interface):
        2: eth0: <...>
            RX: bytes  packets  errors  dropped ...
                NNN    NNN      NNN     NNN
            TX: bytes  packets  errors  dropped ...
                NNN    NNN      NNN     NNN
    """
    errors: List[MemberIfaceError] = []
    lines = raw.splitlines()
    current_dev = ""
    expect_rx_vals = False
    expect_tx_vals = False
    direction = ""

    for line in lines:
        s = line.strip()

        # Interface name line: "2: eth0: <FLAGS>"
        m = re.match(r'^\d+:\s+(\S+):', line)
        if m:
            current_dev = m.group(1).rstrip(":")
            expect_rx_vals = expect_tx_vals = False
            continue

        if s.startswith("RX:") and "bytes" in s:
            expect_rx_vals = True
            direction = "rx"
            expect_tx_vals = False
            continue
        if s.startswith("TX:") and "bytes" in s:
            expect_tx_vals = True
            direction = "tx"
            expect_rx_vals = False
            continue

        if (expect_rx_vals or expect_tx_vals) and current_dev:
            parts = s.split()
            if len(parts) >= 4:
                try:
                    errs = int(parts[2])
                    drops = int(parts[3])
                    if errs > 0 or drops > 0:
                        errors.append(MemberIfaceError(
                            dev=current_dev,
                            direction=direction,
                            errors=errs,
                            drops=drops,
                        ))
                except (ValueError, IndexError):
                    pass
            expect_rx_vals = expect_tx_vals = False

    return errors
