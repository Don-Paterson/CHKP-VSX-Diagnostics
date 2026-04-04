"""
renderers/logfile.py
Writes the full diagnostic output to a plain-text .log file.

render_logfile(summary, path) -> None

The log file contains everything: all raw command output, the NCS
topology, per-VSID detail, cluster health, HCP output, and the full
executive summary at the end.  It is the complete record of the run.

The path is determined by main.py:
    C:\vsx_diagnostics\vsx_diag_<hostname>_<timestamp>.log
"""

from __future__ import annotations

import logging
import os

from models.data import HealthSummary
from renderers.text_builder import build_full_lines

log = logging.getLogger(__name__)


def render_logfile(summary: HealthSummary, path: str) -> None:
    """
    Write the full diagnostic report to path.
    Creates parent directories if needed.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)

    lines    = build_full_lines(summary)
    content  = "\n".join(lines)

    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(content)
        f.write("\n")

    size_kb = os.path.getsize(path) / 1024
    log.info("Log file written: %s (%.1f KB)", path, size_kb)
    print(f"\nLog saved to: {path}")
