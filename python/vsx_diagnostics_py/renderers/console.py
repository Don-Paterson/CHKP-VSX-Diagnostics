"""
renderers/console.py
Writes the executive summary to stdout.

render_console(summary, delta=None) -> None

Prints only the executive summary sections — not the full raw diagnostic
data.  The full output goes to the log file (renderers/logfile.py).

When a DeltaReport is supplied, a compact delta banner is printed before
the main summary showing any flagged changes since the previous run.

No colour — plain text, works in any Windows terminal including the
basic cmd.exe that students may have on A-GUI.
"""

from __future__ import annotations

import sys
from typing import Optional

from models.data import HealthSummary
from models.snapshot import DeltaReport
from renderers.text_builder import build_summary_lines, build_delta_banner_lines


def render_console(summary: HealthSummary, delta: Optional[DeltaReport] = None) -> None:
    """Print the executive summary (and optional delta banner) to stdout."""
    if delta is not None:
        banner = build_delta_banner_lines(delta)
        print("\n".join(banner))

    lines = build_summary_lines(summary)
    output = "\n".join(lines)
    print(output)
    sys.stdout.flush()
