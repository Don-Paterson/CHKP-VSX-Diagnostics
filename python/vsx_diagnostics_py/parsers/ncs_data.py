"""
parsers/ncs_data.py
Pure functions that parse raw output from: vsx showncs <vsid>

parse_ncs(raw, vsid) -> NCSData

No SSH calls, no side effects.

vsx showncs output format
--------------------------
Each line is a gateway configuration command in NCS (Network Configuration
Script) syntax.  The lines we care about:

  interface set dev <dev> address <ip> netmask <mask> [cluster_ip <cip> cluster_mask <cmask>]
  warp create name_a <wrp> name_b <wrpj> ...
  route set dest <net> netmask <mask> gw <gw>
  route set dest <net> netmask <mask> dev <dev>
  bridge attach name <bridge> dev <dev>

These map directly to the grep/sed chains in v18 lines 356-405.

Key rules carried from v18:
  - Skip interfaces where both address AND cluster_ip are 0.0.0.0 or absent
  - WARP interface names (name_a / name_b) are excluded from the external
    interface list in the diagram — they're internal VSX plumbing
  - cluster_ip of 0.0.0.0 means "no cluster IP on this interface"
"""

from __future__ import annotations

import re
import logging
from typing import List

from models.data import NCSData, NCSInterface, NCSWarpPair, NCSRoute

log = logging.getLogger(__name__)

# Compiled regexes — all used per-line so compile once
_RE_DEV      = re.compile(r'\bdev\s+(\S+)')
_RE_ADDRESS  = re.compile(r'\baddress\s+([\d.]+)')
_RE_NETMASK  = re.compile(r'\bnetmask\s+([\d.]+)')
_RE_CIP      = re.compile(r'\bcluster_ip\s+([\d.]+)')
_RE_CMASK    = re.compile(r'\bcluster_mask\s+([\d.]+)')
_RE_NAME_A   = re.compile(r'\bname_a\s+(\S+)')
_RE_NAME_B   = re.compile(r'\bname_b\s+(\S+)')
_RE_GW       = re.compile(r'\bgw\s+([\d.]+)')
_RE_DEST     = re.compile(r'\bdest\s+([\d.]+)')
_RE_BNAME    = re.compile(r'\bname\s+(\S+)')


def _get(pattern: re.Pattern, line: str) -> str:
    m = pattern.search(line)
    return m.group(1) if m else ""


def parse_ncs(raw: str, vsid: int = 0) -> NCSData:
    """
    Parse raw vsx showncs <vsid> output into an NCSData object.

    Parameters
    ----------
    raw  : str  Raw text from vsx showncs
    vsid : int  VSID this data belongs to (for identification only)
    """
    ncs = NCSData(vsid=vsid, available=bool(raw.strip()))

    if not ncs.available:
        log.debug("NCS vsid=%d: empty output", vsid)
        return ncs

    ncs.raw_output = raw

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        # ----------------------------------------------------------
        # Interface lines
        # ----------------------------------------------------------
        if 'interface set dev' in line:
            dev   = _get(_RE_DEV,     line)
            addr  = _get(_RE_ADDRESS, line)
            mask  = _get(_RE_NETMASK, line)
            cip   = _get(_RE_CIP,     line)
            cmask = _get(_RE_CMASK,   line)

            if not dev:
                continue

            # v18 rule: skip if both addr and cip are absent/zero
            addr_zero = (not addr or addr == '0.0.0.0')
            cip_zero  = (not cip  or cip  == '0.0.0.0')
            if addr_zero and cip_zero:
                continue

            ncs.interfaces.append(NCSInterface(
                dev         = dev,
                local_ip    = addr  if not addr_zero else "",
                local_mask  = mask,
                cluster_ip  = cip   if not cip_zero  else "",
                cluster_mask= cmask,
            ))
            log.debug(
                "NCS vsid=%d iface: dev=%s local=%s cluster=%s",
                vsid, dev, addr, cip,
            )

        # ----------------------------------------------------------
        # WARP pairs
        # ----------------------------------------------------------
        elif 'warp create' in line:
            name_a = _get(_RE_NAME_A, line)
            name_b = _get(_RE_NAME_B, line)
            if not name_a:
                continue

            # cluster_ip for the warp interface is on a separate
            # interface line — we resolve it after the full parse
            ncs.warp_pairs.append(NCSWarpPair(
                name_a = name_a,
                name_b = name_b,
            ))
            log.debug("NCS vsid=%d warp: %s <-> %s", vsid, name_a, name_b)

        # ----------------------------------------------------------
        # Static routes
        # ----------------------------------------------------------
        elif 'route set dest' in line:
            dest = _get(_RE_DEST,    line)
            mask = _get(_RE_NETMASK, line)
            gw   = _get(_RE_GW,      line)
            dev  = _get(_RE_DEV,     line)

            if not dest:
                continue

            ncs.routes.append(NCSRoute(
                dest = dest,
                mask = mask,
                gw   = gw,
                dev  = dev if not gw else "",  # prefer gw over dev
            ))
            log.debug("NCS vsid=%d route: %s/%s via %s", vsid, dest, mask, gw or dev)

        # ----------------------------------------------------------
        # Bridge members
        # ----------------------------------------------------------
        elif 'bridge attach' in line:
            dev   = _get(_RE_DEV,   line)
            bname = _get(_RE_BNAME, line)
            if dev:
                ncs.bridge_members.append(dev)
                log.debug("NCS vsid=%d bridge: %s attached to %s", vsid, dev, bname)

    # ----------------------------------------------------------
    # Post-parse: resolve cluster_ip for WARP name_a interfaces
    # ----------------------------------------------------------
    # Build a lookup: dev -> cluster_ip from parsed interfaces
    cip_by_dev = {
        iface.dev: iface.cluster_ip
        for iface in ncs.interfaces
        if iface.cluster_ip
    }
    for wp in ncs.warp_pairs:
        if wp.name_a in cip_by_dev:
            wp.cluster_ip = cip_by_dev[wp.name_a]

    log.info(
        "NCS vsid=%d: %d interfaces, %d warp pairs, %d routes, %d bridge members",
        vsid,
        len(ncs.interfaces),
        len(ncs.warp_pairs),
        len(ncs.routes),
        len(ncs.bridge_members),
    )
    return ncs
