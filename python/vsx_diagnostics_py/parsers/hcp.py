"""
parsers/hcp.py
Pure functions that parse raw terminal output from: hcp -r all

parse_hcp_summary(raw)   -> List[HCPResult]
parse_hcp_details(raw)   -> List[HCPTestDetail]
parse_hcp(raw, hostname) -> HCPCollection

R82 hcp output quirks handled here:
1. ANSI colour codes wrap every status word:
       ESC[32mPASSED ESC[0m  ESC[31mERROR ESC[0m
2. Each test line appears TWICE via carriage return:
       [VS 0]   Test...[Working]\r[VS 0]   Test...[PASSED]\n
   We keep only the final LF-terminated line.
"""

from __future__ import annotations

import re
import logging
from typing import List

from models.data import HCPCollection, HCPResult, HCPTestDetail

log = logging.getLogger(__name__)

# ANSI escape code stripper
_RE_ANSI = re.compile(r'\x1b\[[0-9;]*m')

# Summary line after cleaning:
#   [VS 0]   Memory Usage......................................[ERROR]
#   [VS 0]   Memory Usage......................................[ERROR]   0.23247
_RE_SUMMARY = re.compile(
    r'^\[VS\s+(\d+)\]\s+'
    r'(.+?)'
    r'\.+\s*'
    r'\[([A-Z]+)\]'
    r'(?:\s+([\d.]+))?'
    r'\s*$'
)

_RE_DETAIL_HEADER = re.compile(r'^\|\s+([^|]+?)\s+\|\s*$')
_RE_RESULT_LINE   = re.compile(r'^\|\s*(\d+)\s*\|\s*Result:\s*([A-Z]+)')
_RE_FIELD_LINE    = re.compile(r'^\|\s+\|\s+(.*?)\s*\|\s*$')
_RE_FIELD_START   = re.compile(
    r'^\|\s+\|\s+(Description|Finding|Suggested solutions|Summary):\s*(.*?)\s*\|\s*$'
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI colour escape sequences."""
    return _RE_ANSI.sub('', text)


def _clean_hcp_output(raw: str) -> str:
    """
    Strip ANSI codes and remove interim [Working] lines.
    hcp writes each test twice: [Working] via CR then final status via LF.
    After splitting on LF, any segment containing CR is split on CR and
    only the last segment (the final status) is kept.
    """
    lines = raw.split('\n')
    cleaned = []
    for line in lines:
        line = _strip_ansi(line)
        if '\r' in line:
            line = line.split('\r')[-1]
        cleaned.append(line)
    return '\n'.join(cleaned)


def parse_hcp_summary(raw: str) -> List[HCPResult]:
    """Parse [VS N]   Test name......[STATUS] lines."""
    seen: dict = {}

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
            if runtime_str:
                seen[key].runtime_sec = float(runtime_str)
        else:
            result = HCPResult(
                vsid        = vsid,
                test_name   = test_name,
                status      = status,
                runtime_sec = float(runtime_str) if runtime_str else 0.0,
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
    """Parse pipe-table detail blocks for non-PASSED tests."""
    details: List[HCPTestDetail] = []

    current_title   = ""
    current_vsid    = -1
    current_status  = ""
    current_field   = ""
    desc_lines: list  = []
    finding_lines: list = []
    suggested_lines: list = []

    def _flush():
        nonlocal current_vsid, current_status, current_title
        nonlocal desc_lines, finding_lines, suggested_lines, current_field
        if current_vsid >= 0 and current_title:
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
        desc_lines.clear()
        finding_lines.clear()
        suggested_lines.clear()

    for line in raw.splitlines():
        if re.match(r'^\+[=\-]+\+', line):
            continue

        stripped = line.strip()
        if (stripped.startswith('|') and stripped.endswith('|')
                and stripped.count('|') == 2):
            inner = stripped[1:-1].strip()
            if '/' in inner and len(inner) > 5:
                _flush()
                current_title = inner
                continue

        rm = _RE_RESULT_LINE.match(stripped)
        if rm and current_title:
            current_vsid   = int(rm.group(1))
            current_status = rm.group(2).strip()
            current_field  = ""
            continue

        if current_vsid >= 0:
            fs = _RE_FIELD_START.match(stripped)
            if fs:
                field_name = fs.group(1).lower().replace(' ', '_')
                field_name = field_name.replace('suggested_solutions', 'suggested')
                current_field = field_name
                first_content = fs.group(2).strip()
                if first_content:
                    _append_field(current_field, first_content,
                                  desc_lines, finding_lines, suggested_lines)
                continue

            fc = _RE_FIELD_LINE.match(stripped)
            if fc and current_field:
                content = fc.group(1).strip()
                if not re.match(r'^[+\-=|]+$', content):
                    _append_field(current_field, content,
                                  desc_lines, finding_lines, suggested_lines)
                continue

    _flush()
    log.info("hcp details: parsed %d detail blocks", len(details))
    return details


def _append_field(field, content, desc, finding, suggested):
    if field in ('description', 'summary'):
        desc.append(content)
    elif field == 'finding':
        finding.append(content)
    elif field == 'suggested':
        suggested.append(content)


def parse_hcp(raw: str, hostname: str = "") -> HCPCollection:
    """Full parse of hcp -r all terminal output."""
    collection = HCPCollection(hostname=hostname)

    if not raw.strip():
        log.warning("hcp: empty output")
        collection.not_available = True
        return collection

    if 'command not found' in raw.lower() or 'no such file' in raw.lower():
        log.warning("hcp: binary not available on this gateway")
        collection.not_available = True
        return collection

    # Store original raw output (with ANSI codes) for log/HTML display
    collection.raw_summary = raw

    # Clean for parsing: strip ANSI codes and CR Working lines
    cleaned = _clean_hcp_output(raw)
    collection.results = parse_hcp_summary(cleaned)
    collection.details = parse_hcp_details(cleaned)
    collection.ran_ok  = len(collection.results) > 0

    return collection
