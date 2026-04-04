"""
parsers/iface_errors.py
Pure function for parsing ip -s link output.

parse_iface_errors(raw, vsid) -> List[IfaceError]

Returns only interfaces with non-zero errors or drops.
Empty list means all interfaces are clean.

ip -s link output format
--------------------------
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ...
    link/ether 00:50:56:...
    RX: bytes  packets  errors  dropped  missed  mcast
    1234567    89012    0       0        0       0
    TX: bytes  packets  errors  dropped  carrier collsns
    9876543    12345    0       0        0       0

State machine (mirrors v18 lines 598-630):
    - Line containing ": <" signals a new interface — extract dev name
    - Line containing "RX: bytes" — next data line has RX stats
    - Line containing "TX: bytes" — next data line has TX stats
    - Data line: fields are [bytes, packets, errors, dropped, ...]

v18 field mapping (awk '{print $N}'):
    $2 = packets
    $3 = errors
    $4 = dropped

Only emit an IfaceError if errors > 0 OR drops > 0.
"""

from __future__ import annotations

import re
import logging
from typing import List

from models.data import IfaceError

log = logging.getLogger(__name__)

# Interface header line: "2: eth0: <FLAGS> ..."
# Also handles: "2: eth0@if3: <FLAGS> ..."
_RE_IFACE_HDR = re.compile(r'^\d+:\s+([^:@]+)[@:]')


def parse_iface_errors(raw: str, vsid: int = 0) -> List[IfaceError]:
    """
    Parse ip -s link output and return IfaceError entries for any
    interface with non-zero errors or drops.

    Parameters
    ----------
    raw  : str   Raw output of 'ip -s link'
    vsid : int   VSID this was collected from (stored on each IfaceError)
    """
    errors: List[IfaceError] = []

    current_dev = ""
    rx_next     = False
    tx_next     = False

    for line in raw.splitlines():
        stripped = line.strip()

        # New interface block
        if ': <' in line or (': ' in line and '<' in line):
            m = _RE_IFACE_HDR.match(line.strip())
            if m:
                current_dev = m.group(1).strip()
                rx_next = False
                tx_next = False
            continue

        # RX header
        if 'RX: bytes' in stripped:
            rx_next = True
            tx_next = False
            continue

        # TX header
        if 'TX: bytes' in stripped:
            tx_next = True
            rx_next = False
            continue

        # Data line — parse if we're expecting RX or TX stats
        if (rx_next or tx_next) and current_dev:
            parts = stripped.split()
            if len(parts) >= 3:
                try:
                    packets = int(parts[1])
                    errs    = int(parts[2])
                    drops   = int(parts[3]) if len(parts) >= 4 else 0
                except (ValueError, IndexError):
                    rx_next = tx_next = False
                    continue

                direction = "rx" if rx_next else "tx"

                if errs > 0 or drops > 0:
                    errors.append(IfaceError(
                        vsid      = vsid,
                        dev       = current_dev,
                        direction = direction,
                        errors    = errs,
                        drops     = drops,
                        packets   = packets,
                    ))
                    log.debug(
                        "iface_errors vsid=%d %s %s: err=%d drop=%d pkts=%d",
                        vsid, current_dev, direction, errs, drops, packets,
                    )

            rx_next = tx_next = False

    if errors:
        log.info(
            "iface_errors vsid=%d: %d interface(s) with errors/drops",
            vsid, len(errors),
        )
    return errors
