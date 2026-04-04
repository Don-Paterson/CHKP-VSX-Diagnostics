"""
collectors/cluster_health.py
Collects cluster health data from cphaprob and cpstat commands.

collect_cluster_health(session) -> ClusterHealth

Equivalent to v18's "Cluster Health" section (lines 741-807).

Commands run (all in VS0 expert context via session.run()):
    cphaprob stat       — member states, failover events, cluster mode
    cphaprob -a if      — monitored interfaces (for failover annotation)
    cphaprob syncstat   — sync status and lost updates counter
    cpstat ha -f all    — PNOTE table (scoped parsing)

All commands are run via the persistent interactive shell (session.run())
because they operate in VS0 context and do not require vsenv.

Non-fatal: if cphaprob stat fails (ClusterXL not active, standalone GW),
the collector returns a ClusterHealth with empty fields and logs a warning.
This mirrors v18's else branch at line 805.
"""

from __future__ import annotations

import logging

from models.data import ClusterHealth
from parsers.cphaprob import (
    parse_cphaprob_stat,
    parse_cphaprob_if,
    parse_cphaprob_syncstat,
)
from parsers.cpstat_ha import parse_cpstat_ha
from transport.ssh import ExpertSession

log = logging.getLogger(__name__)


def collect_cluster_health(session: ExpertSession) -> ClusterHealth:
    """
    Collect and parse all cluster health data.

    Returns a populated ClusterHealth dataclass.
    Never raises — all failures captured as empty fields with log warnings.
    """
    health = ClusterHealth()

    # ----------------------------------------------------------------
    # Step 1 — Check ClusterXL is active (mirrors v18 line 747)
    # ----------------------------------------------------------------
    log.info("Cluster health: checking ClusterXL availability ...")
    probe = session.run("cphaprob stat 2>&1 | head -3")

    if not probe.strip() or any(
        phrase in probe.lower() for phrase in (
            'not supported', 'not a cluster', 'clusterxl is not',
            'command not found', 'no such',
        )
    ):
        log.warning(
            "Cluster health: ClusterXL not active or not a cluster member — "
            "cluster health section skipped"
        )
        return health

    # ----------------------------------------------------------------
    # Step 2 — cphaprob stat
    # ----------------------------------------------------------------
    log.info("Cluster health: running cphaprob stat ...")
    raw_stat = session.run("cphaprob stat 2>&1")
    health.cphaprob_raw = raw_stat

    parsed = parse_cphaprob_stat(raw_stat)
    health.cluster_mode           = parsed['cluster_mode']
    health.member_states          = parsed['member_states']
    health.failover_count         = parsed['failover_count']
    health.failover_transition    = parsed['failover_transition']
    health.failover_time          = parsed['failover_time']
    health.last_state_change      = parsed['last_state_change']
    health.last_state_change_time = parsed['last_state_change_time']

    log.info(
        "Cluster health: mode=%r  members=%d  failovers=%d",
        health.cluster_mode,
        len(health.member_states),
        health.failover_count,
    )

    # ----------------------------------------------------------------
    # Step 3 — cphaprob -a if  (monitored interfaces)
    # ----------------------------------------------------------------
    log.info("Cluster health: running cphaprob -a if ...")
    raw_if = session.run("cphaprob -a if 2>&1")
    health.cphaprob_if_raw   = raw_if
    health.monitored_ifaces  = parse_cphaprob_if(raw_if)
    log.info(
        "Cluster health: monitored interfaces: %s",
        health.monitored_ifaces or "(none)",
    )

    # ----------------------------------------------------------------
    # Step 4 — cphaprob syncstat
    # ----------------------------------------------------------------
    log.info("Cluster health: running cphaprob syncstat ...")
    raw_sync = session.run("cphaprob syncstat 2>&1")
    health.syncstat_raw = raw_sync

    parsed_sync = parse_cphaprob_syncstat(raw_sync)
    health.sync_status        = parsed_sync['sync_status']
    health.sync_lost_updates  = parsed_sync['sync_lost_updates']

    log.info(
        "Cluster health: sync_status=%r  lost_updates=%d",
        health.sync_status, health.sync_lost_updates,
    )

    # ----------------------------------------------------------------
    # Step 5 — cpstat ha -f all  (PNOTE table)
    # ----------------------------------------------------------------
    log.info("Cluster health: running cpstat ha -f all ...")
    raw_cpstat = session.run("cpstat ha -f all 2>&1")
    health.cpstat_ha_raw  = raw_cpstat
    health.pnote_entries  = parse_cpstat_ha(raw_cpstat)

    issues = health.pnote_issues
    if issues:
        log.warning(
            "Cluster health: %d PNOTE issue(s): %s",
            len(issues),
            ", ".join(f"{p.name}:{p.status}" for p in issues),
        )
    else:
        log.info("Cluster health: all PNOTEs OK")

    return health
