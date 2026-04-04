"""
collectors/hcp.py
Runs hcp -r all on the active cluster member, downloads the report tar.gz
to the local A-GUI archive folder, and returns a parsed HCPCollection.

collect_hcp(session, hostname, archive_root, timeout) -> HCPCollection

Archive folder structure created on A-GUI:
    <archive_root>/<hostname>/hcp_report_<hostname>_<timestamp>.tar.gz

e.g.:
    C:/vsx_diagnostics/hcp_archive/A-VSX-01/hcp_report_A-VSX-01_04_04_26_18_22.tar.gz

The tar.gz is Check Point's own HTML report -- extract and open index.html
in a browser for the full interactive view.  Our tool parses the terminal
output for ATTENTION items; the tar.gz is kept for historical reference.
"""

from __future__ import annotations

import logging
import os

from models.data import HCPCollection
from parsers.hcp import parse_hcp
from transport.ssh import ExpertSession

log = logging.getLogger(__name__)

# Remote directory where hcp writes its reports
HCP_REPORT_DIR = "/var/log/hcp/last"

# How long to wait for hcp -r all to complete.
# Typical runtime: 60-120s.  Allow 3 minutes before treating as timeout.
HCP_TIMEOUT_SECONDS = 180


def collect_hcp(
    session: ExpertSession,
    hostname: str,
    archive_root: str,
    timeout: int = HCP_TIMEOUT_SECONDS,
) -> HCPCollection:
    """
    Run hcp -r all, parse results, download the tar.gz report.

    Parameters
    ----------
    session      : active ExpertSession (expert mode, VS0)
    hostname     : gateway hostname (e.g. "A-VSX-01") — used for archive path
    archive_root : local base folder for hcp archives on A-GUI
                   e.g. r"C:\vsx_diagnosticshcp_archive"
    timeout      : seconds to wait for hcp to complete (default 180)

    Returns a populated HCPCollection.  Never raises — all failures
    are captured as flags on the collection (timed_out, not_available).
    """
    collection = HCPCollection(hostname=hostname)

    # ----------------------------------------------------------------
    # Step 1 — Check hcp is available
    # ----------------------------------------------------------------
    log.info("HCP: checking availability ...")
    which = session.run("command -v hcp 2>/dev/null").strip()
    if not which:
        log.warning("HCP: 'hcp' command not found on %s — skipping", hostname)
        collection.not_available = True
        return collection
    log.info("HCP: found at %s", which)

    # ----------------------------------------------------------------
    # Step 2 — Run hcp -r all
    # ----------------------------------------------------------------
    log.info("HCP: running 'hcp -r all' (timeout=%ds) — this may take 1-2 minutes ...", timeout)
    try:
        raw = session.run("hcp -r all 2>&1", timeout=timeout)
        log.info("HCP: run complete (%d chars output)", len(raw))
    except Exception as e:
        log.warning("HCP: timed out or failed during run: %s", e)
        collection.timed_out = True
        return collection

    if not raw.strip():
        log.warning("HCP: empty output after run")
        collection.timed_out = True
        return collection

    # ----------------------------------------------------------------
    # Step 3 — Parse terminal output
    # ----------------------------------------------------------------
    log.info("HCP: parsing output ...")
    collection = parse_hcp(raw, hostname=hostname)

    if collection.errors:
        log.warning(
            "HCP: %d ERROR(s): %s",
            len(collection.errors),
            ", ".join(r.test_name for r in collection.errors),
        )
    if collection.infos:
        log.info(
            "HCP: %d INFO(s): %s",
            len(collection.infos),
            ", ".join(r.test_name for r in collection.infos),
        )

    # ----------------------------------------------------------------
    # Step 4 — Find and download the latest tar.gz report
    # ----------------------------------------------------------------
    log.info("HCP: looking for report tar.gz in %s ...", HCP_REPORT_DIR)
    remote_files = session.list_remote_dir(HCP_REPORT_DIR)

    # Filter to tar.gz files for this hostname
    tarballs = [
        (fname, mtime)
        for fname, mtime in remote_files
        if fname.endswith(".tar.gz") and hostname in fname
    ]

    if not tarballs:
        # Fall back to any tar.gz in the directory
        tarballs = [
            (fname, mtime)
            for fname, mtime in remote_files
            if fname.endswith(".tar.gz")
        ]

    if not tarballs:
        log.warning("HCP: no tar.gz report found in %s", HCP_REPORT_DIR)
    else:
        # Newest first (list_remote_dir already sorts this way)
        report_filename = tarballs[0][0]
        remote_path     = f"{HCP_REPORT_DIR}/{report_filename}"
        local_dir       = os.path.join(archive_root, hostname)
        local_path      = os.path.join(local_dir, report_filename)

        log.info("HCP: downloading %s -> %s ...", report_filename, local_path)
        if session.download_file(remote_path, local_path):
            collection.local_archive_path = local_path
            log.info("HCP: report archived at %s", local_path)
        else:
            log.warning("HCP: download failed — report not archived")

    return collection
