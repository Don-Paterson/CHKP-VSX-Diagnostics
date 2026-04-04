"""
parsers/hcp.py
Pure functions that parse raw terminal output from: hcp -r all

parse_hcp_summary(raw)  -> List[HCPResult]
    Parses the summary table at the top (and the failed-tests repeat table).

parse_hcp_details(raw)  -> List[HCPTestDetail]
    Parses the detailed pipe-table blocks for non-PASSED tests.

parse_hcp(raw, hostname) -> HCPCollection
    Convenience wrapper — calls both and returns a fully populated HCPCollection.

No SSH calls, no side effects.

Summary table format (lines 20-143 of your sample):
    [VS 0]   Memory Usage......................................[ERROR]
    [VS 0]   Bond Health.......................................[ERROR]
    [VS 0]   System stressed...................................[INFO]

Failed-tests repeat table (lines 151-154 of your sample):
    [VS 0]   Memory Usage......................................[ERROR]   0.23247
    [VS 0]   Bond Health.......................................[ERROR]   0.18897

Detail blocks (lines 157+ of your sample):
    +-----+---...---+
    |                   Gaia OS/Memory/Memory Usage         |
    +-----+---...---+
    | 0   | Result: ERROR                                   |
    |     | Description: ...                                |
    |     | Finding: ...                                    |
    |     | Suggested solutions: ...                        |
    +-----+---...---+
"""

from __future__ import annotations

import re
import logging
from typing import List

from models.data import HCPCollection, HCPResult, HCPTestDetail

log = logging.getLogger(__name__)

# Summary line:  [VS 0]   Test name......[STATUS]   optional_runtime
_RE_SUMMARY = re.compile(
    r'^\[VS\s+(\d+)\]\s+'       # [VS 0]
    r'(.+?)'                     # test name (non-greedy, stops at dots)
    r'\.+\s*'                    # dot-padding
    r'\[([A-Z]+)\]'              # [STATUS]
    r'(?:\s+([\d.]+))?'          # optional runtime (sec) in failed table
    r'\s*$'
)

# Detail section header:  |   Gaia OS/Memory/Memory Usage   |
# These appear as full-width title rows in the pipe table
_RE_DETAIL_HEADER = re.compile(r'^\|\s+([^|]+?)\s+\|\s*$')

# Result line inside a detail block:  | 0   | Result: ERROR  |
_RE_RESULT_LINE = re.compile(r'^\|\s*(\d+)\s*\|\s*Result:\s*([A-Z]+)')

# Field lines:  |     | Description: ...  |
# or multi-line continuation:  |     | ...continued...  |
_RE_FIELD_LINE  = re.compile(r'^\|\s+\|\s+(.*?)\s*\|\s*$')
_RE_FIELD_START = re.compile(r'^\|\s+\|\s+(Description|Finding|Suggested solutions|Summary):\s*(.*?)\s*\|\s*$')

# Separator lines we skip
_RE_SEPARATOR   = re.compile(r'^[+|=\-\s]+$')


def parse_hcp_summary(raw: str) -> List[HCPResult]:
    """
    Parse all [VS N]   Test name......[STATUS] lines.
    Deduplicates: the failed-tests repeat table will update runtime_sec
    on already-seen results rather than double-counting.
    """
    # keyed by (vsid, test_name) to deduplicate
    seen: dict[tuple, HCPResult] = {}

    for line in raw.splitlines():
        m = _RE_SUMMARY.match(line.strip())
        if not m:
            continue

        vsid        = int(m.group(1))
        test_name   = m.group(2).strip().rstrip('.')
        status      = m.group(3).strip()
        runtime_str = m.group(4)

        key = (vsid, test_name)
        if key in seen:
            # Update runtime if we now have it (from the failed table)
            if runtime_str:
                seen[key].runtime_sec = float(runtime_str)
        else:
            result = HCPResult(
                vsid       = vsid,
                test_name  = test_name,
                status     = status,
                runtime_sec= float(runtime_str) if runtime_str else 0.0,
            )
            seen[key] = result
            log.debug("hcp: [VS %d] %-45s [%s]", vsid, test_name, status)

    results = list(seen.values())
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info(
        "hcp summary: %d tests — %s",
        len(results),
        "  ".join(f"{s}:{n}" for s, n in sorted(counts.items())),
    )
    return results


def parse_hcp_details(raw: str) -> List[HCPTestDetail]:
    """
    Parse the pipe-table detail blocks for non-PASSED tests.

    The structure is:
        +---header separator---+
        |   Section Title      |      ← full-width title (test category)
        +---+---...---+
        | 0 | Result: ERROR    |      ← vsid + result
        |   | Description: ... |      ← field content (may be multi-line)
        |   | ...              |
        |   | Finding:         |
        |   | ...              |
        |   | Suggested solutions: |
        |   | ...              |
        +---+---...---+

    We extract Description, Finding, and Suggested solutions as plain text,
    stripping the pipe-table framing.  ASCII sub-tables within Finding are
    kept as-is — they're useful in the HTML report.
    """
    details: List[HCPTestDetail] = []

    # State machine
    current_title   = ""
    current_vsid    = -1
    current_status  = ""
    current_field   = ""    # which field we're accumulating: desc/finding/suggested/summary
    desc_lines: list[str]       = []
    finding_lines: list[str]    = []
    suggested_lines: list[str]  = []

    def _flush():
        nonlocal current_vsid, current_status, current_title
        nonlocal desc_lines, finding_lines, suggested_lines, current_field
        if current_vsid >= 0 and current_title:
            # Extract just the test name from "Gaia OS/Category/Test Name"
            parts = current_title.split('/')
            test_name = parts[-1].strip() if parts else current_title.strip()
            details.append(HCPTestDetail(
                vsid        = current_vsid,
                test_name   = test_name,
                status      = current_status,
                description = "\n".join(desc_lines).strip(),
                finding     = "\n".join(finding_lines).strip(),
                suggested   = "\n".join(suggested_lines).strip(),
            ))
            log.debug("hcp detail: [VS %d] %s [%s]", current_vsid, test_name, current_status)
        current_vsid    = -1
        current_status  = ""
        current_title   = ""
        current_field   = ""
        desc_lines      = []
        finding_lines   = []
        suggested_lines = []

    for line in raw.splitlines():
        # Top-level separator row — signals start of a new block
        if re.match(r'^\+[=\-]+\+', line):
            # Flush any previous block before starting fresh title hunt
            continue

        # Full-width title row  |   Gaia OS/Memory/Memory Usage   |
        # Characteristics: exactly one pipe on each end, content has no inner pipes
        # or has only one segment (the title)
        stripped = line.strip()
        if (stripped.startswith('|') and stripped.endswith('|')
                and stripped.count('|') == 2):
            inner = stripped[1:-1].strip()
            # Looks like a section title if it contains '/' or is all caps-ish and long
            if '/' in inner and len(inner) > 5:
                _flush()
                current_title = inner
                continue

        # Result line:  | 0   | Result: ERROR  |
        rm = _RE_RESULT_LINE.match(stripped)
        if rm and current_title:
            current_vsid   = int(rm.group(1))
            current_status = rm.group(2).strip()
            current_field  = ""
            continue

        # Field start line:  |     | Description: text |
        if current_vsid >= 0:
            fs = _RE_FIELD_START.match(stripped)
            if fs:
                field_name = fs.group(1).lower().replace(' ', '_')
                field_name = field_name.replace('suggested_solutions', 'suggested')
                current_field = field_name
                first_content = fs.group(2).strip()
                if first_content:
                    _append_to_field(current_field, first_content,
                                     desc_lines, finding_lines, suggested_lines)
                continue

            # Continuation line:  |     | ...content... |
            fc = _RE_FIELD_LINE.match(stripped)
            if fc and current_field:
                content = fc.group(1).strip()
                # Skip pure separator lines within tables
                if not re.match(r'^[+\-=|]+$', content):
                    _append_to_field(current_field, content,
                                     desc_lines, finding_lines, suggested_lines)
                continue

    _flush()  # flush last block
    log.info("hcp details: parsed %d detail blocks", len(details))
    return details


def _append_to_field(
    field: str,
    content: str,
    desc: list,
    finding: list,
    suggested: list,
) -> None:
    if field in ('description', 'summary'):
        desc.append(content)
    elif field == 'finding':
        finding.append(content)
    elif field == 'suggested':
        suggested.append(content)


def parse_hcp(raw: str, hostname: str = "") -> HCPCollection:
    """
    Full parse of hcp -r all terminal output.
    Returns a populated HCPCollection.
    Sets ran_ok=True if any results were found.
    """
    collection = HCPCollection(hostname=hostname)

    if not raw.strip():
        log.warning("hcp: empty output — hcp may not be available or timed out")
        collection.not_available = True
        return collection

    # Check for hcp not found
    if 'command not found' in raw.lower() or 'no such file' in raw.lower():
        log.warning("hcp: binary not available on this gateway")
        collection.not_available = True
        return collection

    collection.raw_summary = raw
    collection.results      = parse_hcp_summary(raw)
    collection.details      = parse_hcp_details(raw)
    collection.ran_ok       = len(collection.results) > 0

    return collection
