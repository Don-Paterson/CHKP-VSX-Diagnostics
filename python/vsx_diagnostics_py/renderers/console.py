"""
renderers/console.py
Writes the executive summary to stdout.

render_console(summary) -> None

Prints only the executive summary sections — not the full raw diagnostic
data.  The full output goes to the log file (renderers/logfile.py).

No colour — plain text, works in any Windows terminal including the
basic cmd.exe that students may have on A-GUI.
"""

from __future__ import annotations

import sys

from models.data import HealthSummary
from renderers.text_builder import build_summary_lines


def render_console(summary: HealthSummary) -> None:
    """Print the executive summary to stdout."""
    lines = build_summary_lines(summary)
    output = "\n".join(lines)
    print(output)
    sys.stdout.flush()
