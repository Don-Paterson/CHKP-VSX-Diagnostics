"""
parsers/vsall.py
Pure functions that parse the content of local.vsall.

parse_vsall(raw, active_member) -> ClusterTopology

No SSH calls, no side effects.  Takes the raw file content as a string,
returns a populated ClusterTopology dataclass.

local.vsall format (relevant lines)
------------------------------------
Member name lines:
    [A-VSX-01:]interface set dev eth0 address 10.1.1.2 ...
    [A-VSX-01:]interface set dev eth2 address 192.168.10.1 ...

The member name is extracted from the bracket prefix.

Global lines (no bracket prefix):
    ... masters_addresses 10.1.1.101 ...
    ... cluster_ip 10.1.1.10 ...          (VIP - may appear multiple times)
    ... route set funny 192.168.20.0 netmask 255.255.255.0 ...  (ICN)

Maps directly to v18 lines 180-205.
"""

from __future__ import annotations

import re
import logging
from typing import Dict, List

from models.data import ClusterMember, ClusterTopology

log = logging.getLogger(__name__)

# Regex constants - compiled once
_RE_MEMBER_NAME  = re.compile(r'^\[([A-Za-z0-9_-]+):\]')
_RE_ADDRESS      = re.compile(r'\baddress\s+([\d.]+)')
_RE_CLUSTER_IP   = re.compile(r'\bcluster_ip\s+([\d.]+)')
_RE_MASTERS      = re.compile(r'\bmasters_addresses\s+([\d.]+)')
_RE_FUNNY_NET    = re.compile(r'\broute set funny\s+([\d.]+)')
_RE_FUNNY_MASK   = re.compile(r'\broute set funny\s+[\d.]+\s+netmask\s+([\d.]+)')


def parse_vsall(raw: str, active_member: str = "") -> ClusterTopology:
    """
    Parse the full content of local.vsall and return a ClusterTopology.

    Parameters
    ----------
    raw           : str   Full text of the vsall file.
    active_member : str   Hostname of the gateway we SSH'd into (for
                          marking which member is "this gateway").
    """
    topology = ClusterTopology(active_member=active_member)

    # --- Pass 1: collect all unique member names from bracket prefixes ---
    member_names: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        m = _RE_MEMBER_NAME.match(line)
        if m:
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                member_names.append(name)

    member_names.sort()
    log.debug("vsall: found %d cluster members: %s", len(member_names), member_names)

    # Build a dict for quick population
    members: Dict[str, ClusterMember] = {
        name: ClusterMember(name=name) for name in member_names
    }

    # --- Pass 2: extract per-member IPs and global fields ---
    cluster_vip    = ""
    mgmt_server    = ""
    icn_net        = ""
    icn_mask       = ""

    for line in raw.splitlines():
        # Per-member lines: [MemberName:]interface set dev ethX address Y
        mb = _RE_MEMBER_NAME.match(line)
        if mb:
            name = mb.group(1)
            if name not in members:
                continue

            # Management IP: eth0 address (member's own IP, not cluster_ip)
            if 'dev eth0 ' in line or 'dev eth0\t' in line:
                am = _RE_ADDRESS.search(line)
                if am and not members[name].mgmt_ip:
                    members[name].mgmt_ip = am.group(1)
                    log.debug("  %s mgmt_ip=%s", name, am.group(1))

            # Sync IP: eth2 address
            if 'dev eth2 ' in line or 'dev eth2\t' in line:
                am = _RE_ADDRESS.search(line)
                if am and not members[name].sync_ip:
                    members[name].sync_ip = am.group(1)
                    log.debug("  %s sync_ip=%s", name, am.group(1))

        else:
            # Global lines (no bracket prefix)

            # Management server address
            if not mgmt_server:
                mm = _RE_MASTERS.search(line)
                if mm:
                    mgmt_server = mm.group(1)
                    log.debug("vsall: mgmt_server=%s", mgmt_server)

            # ICN (funny IP) network
            if not icn_net and 'route set funny' in line:
                fn = _RE_FUNNY_NET.search(line)
                fm = _RE_FUNNY_MASK.search(line)
                if fn:
                    icn_net  = fn.group(1)
                    icn_mask = fm.group(1) if fm else ""
                    log.debug("vsall: icn=%s/%s", icn_net, icn_mask)

    # --- Pass 3: cluster VIP ---
    # cluster_ip appears on [MemberName:]interface set dev eth0 lines.
    # Scan all lines (member-prefixed and global) to find it.
    for line in raw.splitlines():
        if not cluster_vip and ('dev eth0 ' in line or 'dev eth0\t' in line):
            cm = _RE_CLUSTER_IP.search(line)
            if cm and cm.group(1) != '0.0.0.0':
                cluster_vip = cm.group(1)
                log.debug("vsall: cluster_vip=%s", cluster_vip)
                break

    topology.members     = list(members.values())
    topology.cluster_vip = cluster_vip
    topology.mgmt_server = mgmt_server
    topology.icn_net     = icn_net
    topology.icn_mask    = icn_mask

    log.info(
        "Topology: %d members, VIP=%s, mgmt=%s, ICN=%s/%s",
        len(topology.members), cluster_vip or "?",
        mgmt_server or "?", icn_net or "?", icn_mask or "?",
    )
    return topology
