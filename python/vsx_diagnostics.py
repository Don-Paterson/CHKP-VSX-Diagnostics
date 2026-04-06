"""
vsx_diagnostics.py  (entry point)
VSX Gateway & Cluster Health Diagnostics — Python edition
Runs from A-GUI (Windows), connects to the VSX cluster via SSH.

Usage
-----
python vsx_diagnostics.py --hosts 10.1.1.2 10.1.1.3 10.1.1.4

Full options:
    --hosts     IP addresses to try in order (first reachable wins)
    --username  SSH username (default: admin)
    --password  SSH password (prompted if omitted)
    --expert-password
                Expert mode password (defaults to --password if omitted)
    --fetch     Run 'vsx fetch' before collecting NCS data (recommended on R82)
    --output-dir
                Directory for log and HTML output
                (default: C:\\vsx_diagnostics\\reports)
    --hcp-archive
                Directory for HCP tar.gz archives
                (default: C:\\vsx_diagnostics\\hcp_archive)
    --port      SSH port (default: 22)
    --timeout   SSH connect timeout in seconds (default: 15)
    --log-level DEBUG / INFO / WARNING (default: WARNING)

Output files (written to --output-dir):
    vsx_diag_<hostname>_<timestamp>.log    — full plain-text diagnostic
    vsx_diag_<hostname>_<timestamp>.html   — self-contained HTML report

Collection order
----------------
1.  Connect to first available cluster member
2.  Preflight checks (root, FWDIR, vsx availability)
3.  Platform info (fw ver, JHF take, uptime, disk)
4.  Optional: vsx fetch
5.  Cluster topology (local.vsall)
6.  VSX overview + VSID discovery (vsx stat)
7.  NCS data (vsx showncs per VSID, file-redirect workaround)
8.  Per-VSID diagnostics (vsenv subshells)
9.  CoreXL & affinity (VS0)
10. Cluster health (cphaprob, cpstat)
11. HCP health check (hcp -r all + SFTP download)
12. Health assessment (all threshold rules)
13. Render console, logfile, HTML
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import logging
import os
import sys

# ---------------------------------------------------------------------------
# Imports — packages live inside vsx_diagnostics_py\ subdirectory
# ---------------------------------------------------------------------------
# vsx_diagnostics.py  lives at: C:\vsx_diagnostics\
# packages live at:             C:\vsx_diagnostics\vsx_diagnostics_py\
# We add vsx_diagnostics_py\ to sys.path so bare imports work everywhere.
_HERE    = os.path.dirname(os.path.abspath(__file__))
_PKG_DIR = os.path.join(_HERE, "vsx_diagnostics_py")
for _p in (_HERE, _PKG_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from collectors.cluster_health import collect_cluster_health
from collectors.hcp import collect_hcp
from collectors.ncs import collect_ncs
from collectors.per_vsid import collect_all_vsids
from collectors.platform import collect_platform
from collectors.topology import PreflightError, collect_preflight, collect_topology
from collectors.vsid_discovery import collect_vsid_discovery
from delta.comparator import compare as delta_compare
from delta.serialiser import load_prev_snapshot, save_snapshot, snapshot_from_summary
from health.assessor import assess
from models.data import HealthSummary
from renderers.console import render_console
from renderers.html import render_html
from renderers.logfile import render_logfile
from transport.ssh import SSHError, connect_to_cluster

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_HOSTS     = ["10.1.1.2", "10.1.1.3", "10.1.1.4"]
DEFAULT_USERNAME  = "admin"
DEFAULT_OUTPUT    = r"C:\vsx_diagnostics\reports"
DEFAULT_HCP_ARCH  = r"C:\vsx_diagnostics\hcp_archive"
SCRIPT_VERSION    = "v1.0"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="VSX Gateway Health Diagnostics — connects from A-GUI via SSH",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--hosts", nargs="+", default=DEFAULT_HOSTS, metavar="IP",
        help=f"Cluster member IPs to try in order (default: {' '.join(DEFAULT_HOSTS)})",
    )
    p.add_argument(
        "--username", default=DEFAULT_USERNAME,
        help=f"SSH username (default: {DEFAULT_USERNAME})",
    )
    p.add_argument(
        "--password", default=None,
        help="SSH password (prompted if omitted)",
    )
    p.add_argument(
        "--expert-password", default=None, dest="expert_password",
        help="Expert mode password (defaults to --password if omitted)",
    )
    p.add_argument(
        "--fetch", action="store_true",
        help="Run 'vsx fetch' before NCS collection (recommended on R82 first run)",
    )
    p.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT, dest="output_dir",
        help=f"Directory for log and HTML files (default: {DEFAULT_OUTPUT})",
    )
    p.add_argument(
        "--hcp-archive", default=DEFAULT_HCP_ARCH, dest="hcp_archive",
        help=f"Directory for HCP tar.gz archives (default: {DEFAULT_HCP_ARCH})",
    )
    p.add_argument(
        "--port", type=int, default=22,
        help="SSH port (default: 22)",
    )
    p.add_argument(
        "--timeout", type=int, default=15,
        help="SSH connect timeout in seconds (default: 15)",
    )
    p.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING"],
        dest="log_level",
        help="Logging verbosity (default: WARNING)",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Output path helpers
# ---------------------------------------------------------------------------

def _make_output_paths(output_dir: str, hostname: str, timestamp: str) -> tuple[str, str]:
    """Return (log_path, html_path)."""
    os.makedirs(output_dir, exist_ok=True)
    stem = f"vsx_diag_{hostname}_{timestamp}"
    return (
        os.path.join(output_dir, f"{stem}.log"),
        os.path.join(output_dir, f"{stem}.html"),
    )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    """
    Full diagnostic run.  Returns exit code (0 = success, 1 = error).
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    iso_ts    = datetime.datetime.now().isoformat(timespec="seconds")

    print(f"\nVSX Diagnostics {SCRIPT_VERSION}")
    print(f"Trying cluster members: {', '.join(args.hosts)}")

    # ----------------------------------------------------------------
    # Credentials
    # ----------------------------------------------------------------
    password = args.password
    if not password:
        password = getpass.getpass(f"Password for {args.username}@cluster: ")

    expert_password = args.expert_password or password

    # ----------------------------------------------------------------
    # SSH connection
    # ----------------------------------------------------------------
    try:
        session = connect_to_cluster(
            hosts          = args.hosts,
            username       = args.username,
            password       = password,
            expert_password= expert_password,
            port           = args.port,
            timeout        = args.timeout,
        )
    except SSHError as e:
        print(f"\nERROR: Cannot connect to cluster: {e}", file=sys.stderr)
        return 1

    summary = HealthSummary(
        script_version = SCRIPT_VERSION,
        run_timestamp  = iso_ts,
        do_fetch       = args.fetch,
    )

    with session:
        try:
            _collect_all(session, summary, args)
        except PreflightError as e:
            print(f"\nPREFLIGHT FAILED: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            logging.exception("Unexpected error during collection")
            print(f"\nERROR during collection: {e}", file=sys.stderr)
            # Continue to render whatever was collected
            summary.attention_items.append(
                __import__("models.data", fromlist=["AttentionItem"]).AttentionItem(
                    severity="CRITICAL",
                    category="Collection Error",
                    message=str(e),
                )
            )

    # ----------------------------------------------------------------
    # Assessment
    # ----------------------------------------------------------------
    assess(summary)

    # ----------------------------------------------------------------
    # Delta comparison
    # ----------------------------------------------------------------
    iso_ts_safe = iso_ts.replace(":", "-")   # filesystem-safe timestamp (reserved for future use)
    snapshot     = snapshot_from_summary(summary)
    prev_snapshot = load_prev_snapshot(args.output_dir, iso_ts)

    delta = None
    if prev_snapshot is not None:
        delta = delta_compare(prev_snapshot, snapshot)

    # ----------------------------------------------------------------
    # Output paths
    # ----------------------------------------------------------------
    hostname  = summary.topology.active_member or session.hostname or "unknown"
    stem      = f"vsx_diag_{hostname}_{timestamp}"
    log_path, html_path = _make_output_paths(args.output_dir, hostname, timestamp)

    # Save snapshot alongside the other output files (after paths known)
    save_snapshot(snapshot, args.output_dir, stem)

    # ----------------------------------------------------------------
    # Render
    # ----------------------------------------------------------------
    render_console(summary, delta=delta)
    render_logfile(summary, log_path, delta=delta)
    render_html(summary, html_path, delta=delta)

    return 0


def _collect_all(session, summary: HealthSummary, args: argparse.Namespace) -> None:
    """Run all collectors in order, populating summary in place."""

    # 1. Preflight + partial platform
    print("  Preflight checks ...", end="", flush=True)
    platform_info = collect_preflight(session)
    summary.platform          = platform_info
    summary.topology.fwdir    = getattr(platform_info, "_fwdir", "")
    summary.topology.active_member = platform_info.hostname
    summary.topology.connected_ip  = session.connected_ip
    print(" OK")

    fwdir = summary.topology.fwdir

    # 2. Full platform info
    print("  Platform info ...", end="", flush=True)
    summary.platform = collect_platform(session, platform_info)
    print(" OK")

    # 3. Optional vsx fetch
    if args.fetch:
        print("  vsx fetch ...", end="", flush=True)
        session.run("vsx fetch 2>&1", timeout=60)
        print(" OK")

    # 4. Cluster topology
    print("  Cluster topology ...", end="", flush=True)
    summary.topology = collect_topology(
        session,
        fwdir         = fwdir,
        active_member = platform_info.hostname,
    )
    print(f" OK ({len(summary.topology.members)} members)")

    # 5. VSID discovery
    print("  VSX overview + VSID discovery ...", end="", flush=True)
    summary.vsx_overview, summary.vsids = collect_vsid_discovery(session)
    print(f" OK ({len(summary.vsids)} VSIDs)")

    # 6. NCS data
    print("  NCS topology (vsx showncs) ...", end="", flush=True)
    summary.showncs_available, summary.ncs = collect_ncs(session, summary.vsids)
    status = "OK" if summary.showncs_available else "unavailable (run with --fetch)"
    print(f" {status}")

    # 7. Per-VSID diagnostics
    print("  Per-VSID diagnostics ...")
    for vsid_info in summary.vsids:
        print(f"    VSID {vsid_info.vsid} ({vsid_info.name}) ...", end="", flush=True)
    summary.vsid_diags = collect_all_vsids(session, summary.vsids)
    # Reprint with counts
    print(f"\r  Per-VSID diagnostics ... OK ({len(summary.vsid_diags)} VSIDs)    ")

    # 8. Cluster health
    print("  Cluster health ...", end="", flush=True)
    summary.cluster_health = collect_cluster_health(session)
    print(" OK")

    # 9. HCP
    print("  HCP health check (hcp -r all — may take 1-2 minutes) ...",
          end="", flush=True)
    summary.hcp = collect_hcp(
        session      = session,
        hostname     = platform_info.hostname,
        archive_root = args.hcp_archive,
    )
    hcp_status = (
        f"OK ({len(summary.hcp.errors)} error(s), {len(summary.hcp.infos)} info(s))"
        if summary.hcp.ran_ok else
        "unavailable" if summary.hcp.not_available else
        "timed out" if summary.hcp.timed_out else
        "parse failed (check log)"
    )
    print(f" {hcp_status}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level   = getattr(logging, args.log_level),
        format  = "%(levelname)s:%(name)s:%(message)s",
    )

    sys.exit(run(args))


if __name__ == "__main__":
    main()
