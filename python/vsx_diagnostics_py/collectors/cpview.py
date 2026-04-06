"""
collectors/cpview.py
Collects CPView historical CPU data from the VSX gateway.

collect_cpview(session, platform_info) -> PlatformInfo   (mutates in place)

CPView is Check Point's built-in performance monitor.  It stores
1-minute CPU/memory/connection samples in /var/log/CPView_history/.
This collector reads those history files directly to extract
5-minute, 15-minute, and 1-hour CPU idle averages — giving time-series
context to supplement the 1-second mpstat snapshot.

Why history files rather than `cpview -t`
-----------------------------------------
`cpview -t` (text mode) requires a PTY and produces interactive output
that is hard to parse reliably in a Paramiko exec_command channel.
Reading the raw history files directly is simpler, faster, and more
robust across R81.10 / R82 versions.

History file format (CPView_history/cpu)
-----------------------------------------
Each line is a tab-separated record:
    <unix_timestamp>  <user%>  <sys%>  <idle%>  <iowait%>  <total_used%>

The files are rotated hourly.  We read the most recent file and extract
the last N samples, computing averages.  One sample per minute → last
5 samples = 5-minute average.

Fallback
--------
If the history directory does not exist or is empty (e.g. CPView daemon
not running), we fall back to parsing `cpview -s -t` text output.
If that also fails, cpview_available is set to False and the fields
remain None.  This is non-fatal — the existing mpstat snapshot still
provides a point-in-time CPU reading.

VS0 context only
----------------
CPView data is gateway-wide (system level), not per-VSID.  The data
is collected once in VS0 context and stored on PlatformInfo.
"""

from __future__ import annotations

import logging
import re
import time
from typing import List, Optional, Tuple

from models.data import PlatformInfo
from transport.ssh import ExpertSession

log = logging.getLogger(__name__)

# CPView history directory on Gaia
CPVIEW_HISTORY_DIR = "/var/log/CPView_history"
CPVIEW_CPU_FILE    = f"{CPVIEW_HISTORY_DIR}/cpu"

# How many 1-minute samples to average
SAMPLES_5M  = 5
SAMPLES_15M = 15
SAMPLES_1H  = 60


def collect_cpview(
    session: ExpertSession,
    platform_info: PlatformInfo,
) -> PlatformInfo:
    """
    Attempt to collect CPView historical CPU data.
    Mutates platform_info in place and returns it.
    Never raises — all failures set cpview_available=False.
    """
    log.info("CPView: attempting to collect historical CPU data ...")

    # ── Try history file first (fastest, most reliable) ───────────────
    try:
        success = _collect_from_history_file(session, platform_info)
        if success:
            log.info(
                "CPView: 5m_idle=%.1f%% 15m_idle=%.1f%% 1h_idle=%s",
                platform_info.cpview_cpu_5m_idle or 0,
                platform_info.cpview_cpu_15m_idle or 0,
                f"{platform_info.cpview_cpu_1h_idle:.1f}%"
                if platform_info.cpview_cpu_1h_idle is not None else "n/a",
            )
            return platform_info
    except Exception as exc:
        log.debug("CPView history file read failed: %s", exc)

    # ── Fallback: cpview -s -t text mode ─────────────────────────────
    try:
        success = _collect_from_cpview_cmd(session, platform_info)
        if success:
            log.info(
                "CPView (cmd fallback): 5m_idle=%.1f%%",
                platform_info.cpview_cpu_5m_idle or 0,
            )
            return platform_info
    except Exception as exc:
        log.debug("CPView command fallback failed: %s", exc)

    log.info("CPView: not available on this gateway")
    platform_info.cpview_available = False
    return platform_info


# ---------------------------------------------------------------------------
# History file approach
# ---------------------------------------------------------------------------

def _collect_from_history_file(
    session: ExpertSession,
    platform_info: PlatformInfo,
) -> bool:
    """
    Read the CPView CPU history file directly.
    Returns True if data was successfully extracted.
    """
    # Check the history directory exists
    check = session.run(
        f"[ -d '{CPVIEW_HISTORY_DIR}' ] && echo EXISTS || echo MISSING"
    )
    if "MISSING" in check:
        log.debug("CPView: history directory %s not found", CPVIEW_HISTORY_DIR)
        return False

    # List available cpu history files (may be rotated: cpu, cpu.1, cpu.2 ...)
    ls_out = session.run(
        f"ls -t {CPVIEW_HISTORY_DIR}/cpu* 2>/dev/null | head -3"
    )
    if not ls_out.strip():
        log.debug("CPView: no cpu history files found")
        return False

    # Read the most recent cpu history file
    cpu_file = ls_out.splitlines()[0].strip()
    raw = session.run(f"tail -70 '{cpu_file}' 2>/dev/null")

    if not raw.strip():
        log.debug("CPView: cpu history file empty")
        return False

    platform_info.cpview_raw = raw
    samples = _parse_cpu_history(raw)

    if not samples:
        log.debug("CPView: could not parse any samples from history file")
        return False

    platform_info.cpview_available = True
    platform_info.cpview_cpu_5m_idle  = _avg_idle(samples, SAMPLES_5M)
    platform_info.cpview_cpu_15m_idle = _avg_idle(samples, SAMPLES_15M)
    platform_info.cpview_cpu_1h_idle  = _avg_idle(samples, SAMPLES_1H)
    return True


def _parse_cpu_history(raw: str) -> List[float]:
    """
    Parse CPView CPU history file lines into a list of idle% values.

    Two known formats:

    Format 1 (R80.x / R81):
        <timestamp>\t<user>\t<sys>\t<idle>\t<iowait>\t<total>
        1712345678\t5.2\t3.1\t91.7\t0.0\t8.3

    Format 2 (R82 — extended columns):
        <timestamp>\t<user>\t<nice>\t<sys>\t<idle>\t<iowait>\t...
        1712345678\t5.2\t0.0\t3.1\t91.7\t0.0\t...

    We detect which format by column count:
    - 6 columns → Format 1, idle is col[3]
    - 7+ columns → Format 2, idle is col[4]

    Lines starting with # are headers — skipped.
    Lines with non-numeric timestamps are skipped.
    """
    idle_values: List[float] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) < 4:
            continue

        # Validate first field is a unix timestamp (10 digits)
        if not re.match(r'^\d{9,11}$', parts[0]):
            continue

        try:
            if len(parts) == 6:
                # Format 1: timestamp user sys idle iowait total
                idle = float(parts[3])
            else:
                # Format 2 (R82): timestamp user nice sys idle ...
                idle = float(parts[4])

            if 0.0 <= idle <= 100.0:
                idle_values.append(idle)
        except (ValueError, IndexError):
            continue

    log.debug("CPView: parsed %d idle samples from history", len(idle_values))
    return idle_values


def _avg_idle(samples: List[float], n: int) -> Optional[float]:
    """Average the last n idle% samples. Returns None if insufficient data."""
    if not samples:
        return None
    tail = samples[-n:] if len(samples) >= n else samples
    return round(sum(tail) / len(tail), 1)


# ---------------------------------------------------------------------------
# cpview command fallback
# ---------------------------------------------------------------------------

def _collect_from_cpview_cmd(
    session: ExpertSession,
    platform_info: PlatformInfo,
) -> bool:
    """
    Fallback: run `cpview -s -t` and parse the CPU section.
    Returns True if any data was extracted.

    `cpview -s -t` produces a snapshot of current stats in text mode,
    including a CPU section with usage percentages.  It does not require
    a PTY and typically exits cleanly in exec_command.

    We only extract idle% from this — no historical averaging possible,
    so only cpu_5m_idle is set (as a point-in-time value, labelled
    appropriately in the output).
    """
    # Check cpview is available
    which = session.run("which cpview 2>/dev/null || echo NOTFOUND")
    if "NOTFOUND" in which or not which.strip():
        return False

    raw = session.run("cpview -s -t 2>/dev/null", timeout=30)
    if not raw.strip():
        return False

    platform_info.cpview_raw = raw

    # Parse idle% from cpview text output
    # Typical line: "CPU:  user 5.2%  sys 3.1%  idle 91.7%  iowait 0.0%"
    idle = _parse_cpview_cmd_idle(raw)
    if idle is None:
        return False

    platform_info.cpview_available = True
    platform_info.cpview_cpu_5m_idle = idle   # point-in-time, not averaged
    return True


def _parse_cpview_cmd_idle(raw: str) -> Optional[float]:
    """
    Extract idle% from cpview -s -t text output.
    Returns None if not found.

    Handles formats:
      "CPU:  user 5.2%  sys 3.1%  idle 91.7%  iowait 0.0%"
      "%user  %nice  %sys  %iowait  %idle\\n5.20   0.00   3.10   0.00    91.70"
      "Idle: 88.5"
    """
    lines = raw.splitlines()

    # Format 2: column header line followed by data line
    for i, line in enumerate(lines):
        if '%idle' in line.lower() and i + 1 < len(lines):
            headers = line.lower().split()
            try:
                idle_col = next(j for j, h in enumerate(headers) if '%idle' in h)
                data_parts = lines[i + 1].split()
                if len(data_parts) > idle_col:
                    val = float(data_parts[idle_col])
                    if 0.0 <= val <= 100.0:
                        return round(val, 1)
            except (StopIteration, ValueError, IndexError):
                pass

    # Format 1 and 3: inline patterns
    patterns = [
        r'idle\s+(\d+(?:\.\d+)?)\s*%',
        r'Idle:\s*(\d+(?:\.\d+)?)',
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if 0.0 <= val <= 100.0:
                    return round(val, 1)
            except ValueError:
                pass
    return None
