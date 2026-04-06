# CHKP-VSX-Diagnostics

Health diagnostics for Check Point VSX Gateway clusters running R82.  
Two tools, same job — choose based on where you're running from.

---

## Tools

### `bash/vsx_diagnostics.sh` — Run on the gateway (v18)

Bash script that runs directly on the VSX gateway in Expert mode (VS0 context).  
No external dependencies — uses only standard Gaia CLI tools.

Collects cluster topology, per-VSID diagnostics (blades, CPU, routing, interfaces,
SecureXL, connection tables), cluster health (cphaprob/cpstat), and produces an
executive summary with a HEALTH section and ATTENTION items for any anomalies detected.

```bash
# On the gateway — Expert mode, VS0 context
chmod +x vsx_diagnostics.sh
./vsx_diagnostics.sh -f        # -f runs vsx fetch first (required for topology map on R82)
./vsx_diagnostics.sh -q        # quiet: full log to file, only executive summary to terminal
./vsx_diagnostics.sh -f -q     # both
./vsx_diagnostics.sh -h        # usage
```

Log file is written to the same directory as the script.

---

### `python/vsx_diagnostics.py` — Run from A-GUI (Windows)

Python 3.12 script that connects to the VSX cluster via SSH from the Windows admin
workstation. No tools need to be installed on the gateway.

Produces five output files on each run:
- **Console** — executive summary with delta banner (if a previous run exists)
- **`.log`** — full plain-text diagnostic (all raw command output + delta section + summary)
- **`.html`** — self-contained dark-theme report: cluster state, per-VSID status, HCP findings, delta comparison card, all-member comparison table
- **`.json`** — complete machine-readable export (Power BI / Splunk / Grafana)
- **`.csv`** — flat per-VSID table for pivot tables and time-series joins
- **`.snapshot.json`** — internal state file for delta comparison on the next run

HCP reports (`hcp -r all`) are automatically downloaded from the gateway via SFTP
and archived locally per gateway for historical reference.

#### Requirements

- Python 3.12 from [python.org](https://www.python.org/downloads/) — tick **Add to PATH** during install
- One package: `pip install paramiko==3.5.1`

#### Deploy to A-GUI (one-liner)

```powershell
irm https://raw.githubusercontent.com/Don-Paterson/CHKP-VSX-Diagnostics/main/python/install.ps1 | iex
```

The installer downloads the tool, installs paramiko, verifies the install, and creates
the output directory structure. Safe to re-run — preserves the `hcp_archive` folder
across updates.

#### Usage

```powershell
# First run — include --fetch to populate NCS topology data (required on R82)
python C:\vsx_diagnostics\vsx_diagnostics.py --fetch

# Subsequent runs — delta comparison and CPView history collected automatically
python C:\vsx_diagnostics\vsx_diagnostics.py

# Lab/Skillable environment — suppresses Hyper-V noise, uses loose thresholds
python C:\vsx_diagnostics\vsx_diagnostics.py --profile lab

# VMware or cloud-hosted gateways
python C:\vsx_diagnostics\vsx_diagnostics.py --profile virtual

# Query all three cluster members and show per-member differences
python C:\vsx_diagnostics\vsx_diagnostics.py --all-members

# Combine flags
python C:\vsx_diagnostics\vsx_diagnostics.py --profile lab --all-members --fetch

# Verbose output — shows each collector as it runs
python C:\vsx_diagnostics\vsx_diagnostics.py --log-level INFO

# Custom cluster IPs (defaults to 10.1.1.2, 10.1.1.3, 10.1.1.4)
python C:\vsx_diagnostics\vsx_diagnostics.py --hosts 10.0.0.1 10.0.0.2 10.0.0.3

# All options
python C:\vsx_diagnostics\vsx_diagnostics.py --help
```

#### Full options reference

| Option | Default | Description |
|--------|---------|-------------|
| `--hosts` | `10.1.1.2 10.1.1.3 10.1.1.4` | Cluster member IPs — tried in order, first reachable wins |
| `--username` | `admin` | SSH username |
| `--password` | *(prompted)* | SSH password |
| `--expert-password` | *(same as password)* | Expert mode password if different |
| `--fetch` | off | Run `vsx fetch` before NCS collection — required on R82 first run |
| `--profile` | `production` | Threshold profile: `lab` / `virtual` / `production` |
| `--all-members` | off | Connect to all reachable cluster members and show per-member differences |
| `--output-dir` | `C:\vsx_diagnostics\reports` | Directory for all output files |
| `--hcp-archive` | `C:\vsx_diagnostics\hcp_archive` | Directory for HCP tar.gz archives |
| `--port` | `22` | SSH port |
| `--timeout` | `15` | SSH connect timeout (seconds) |
| `--log-level` | `WARNING` | Verbosity: `DEBUG` / `INFO` / `WARNING` |

#### Output files

```
C:\vsx_diagnostics\
├── reports\
│   ├── vsx_diag_<hostname>_<timestamp>.log              # full plain-text diagnostic
│   ├── vsx_diag_<hostname>_<timestamp>.html             # self-contained HTML report
│   ├── vsx_diag_<hostname>_<timestamp>.json             # machine-readable full export
│   ├── vsx_diag_<hostname>_<timestamp>.csv              # flat per-VSID table
│   └── vsx_diag_<hostname>_<timestamp>.snapshot.json    # delta state (internal)
└── hcp_archive\
    └── <hostname>\
        └── hcp_report_<hostname>_<timestamp>.tar.gz     # CP HCP report (extract → index.html)
```

---

## Delta comparison

From the second run onward, each run is automatically compared against the most
recent previous snapshot. Changes are highlighted in the console banner, log file,
and HTML report.

**What is compared:**

| Category | Metrics |
|----------|---------|
| Cluster | Failover count, sync status, sync lost updates, per-member state |
| Platform | CPU idle %, swap used (MB), root disk %, /var/log disk % |
| Connections | Total connection count (global), per-VSID connection % of limit |
| SecureXL | Status per firewall VSID |
| Interfaces | Cumulative RX/TX error and drop counters per VSID |
| PNOTEs | New, resolved, and status-changed entries |
| HCP | Tests that moved between PASSED and ERROR/WARNING/INFO |

Cluster state events (failover, sync loss, member state change) are always flagged.
Resource metrics are only flagged when the change exceeds the active profile's thresholds.
Runs less than the profile's minimum gap apart have resource flags suppressed.

---

## All-member collection (`--all-members`)

When `--all-members` is specified, the tool connects to each reachable cluster member
in turn and collects a targeted health snapshot: version, JHF take, CPU, disk, swap,
sync status, failover count, CoreXL instances, and interface error counters.

Cross-member differences are then compared and surfaced in a dedicated section:

- **Exact-match metrics** (version, JHF take, CoreXL count, sync status, failover count) — any difference is flagged
- **Spread-based metrics** (CPU idle %, disk %, swap) — flagged if spread exceeds the profile threshold
- **Cluster state view disagreements** — flagged as a potential split-brain indicator
- **Per-member interface errors** — flagged per member

The primary session (already open) is reused for the first member; fresh connections
are opened for the remaining members.

---

## Threshold profiles (`--profile`)

Three built-in profiles control all health assessment and delta comparison thresholds:

| Threshold | `lab` | `virtual` | `production` |
|-----------|-------|-----------|--------------|
| CPU idle warn | < 20% | < 35% | < 50% |
| Swap warn | > 500 MB | > 200 MB | > 100 MB |
| Connection warn | ≥ 90% | ≥ 85% | ≥ 80% |
| Disk warn | ≥ 90% | ≥ 85% | ≥ 80% |
| WARP iface errors | INFO (suppressed) | WARNING | WARNING |
| Iface error rate floor | 0% | 0.5% | 0% |
| Delta min gap | 300s | 180s | 120s |
| Delta disk increase | > 10 pp | > 7 pp | > 5 pp |
| Member disk spread | > 20 pp | > 15 pp | > 10 pp |

**`lab`** — designed for Hyper-V/Skillable environments. Suppresses WARP interface
errors (expected noise with no physical LACP partner), uses loose CPU/swap/disk
thresholds, and allows a longer gap between runs before flagging resource changes.

**`virtual`** — for VMware or cloud-hosted gateways that run warmer than bare-metal
but cooler than a lab VM.

**`production`** — matches v18 and CP best practice. This is the default.

---

## CPView historical CPU data

When `/var/log/CPView_history/cpu` is present on the gateway (CPView daemon running),
the tool reads the history file directly to extract 5-minute, 15-minute, and 1-hour
CPU idle averages. These supplement the 1-second `mpstat` snapshot and appear in the
Health Indicators section of the HTML report and in the JSON export under
`platform.cpview_cpu_5m_idle`, `cpview_cpu_15m_idle`, and `cpview_cpu_1h_idle`.

If the history directory is absent, a `cpview -s -t` command fallback is attempted.
If both fail, `cpview_available: false` is recorded and the tool continues without error.

---

## Machine-readable export

Every run produces a `.json` and `.csv` file alongside the HTML report.

**JSON** (`schema_version: "1"`) — complete structured export including run metadata,
platform (with CPView averages), cluster health, per-VSID table, attention items,
HCP results, delta summary, and member comparison summary.

**CSV** — one row per VSID, with run-level metadata duplicated on each row for
easy pivot and time-series join on `run_id + vsid`.

**Ingestion:**
- **Power BI** — Get Data → Text/CSV (use the `.csv`), or JSON connector (use the `.json`)
- **Splunk** — HTTP Event Collector or file monitor; `run.run_id` maps to `_time`
- **Grafana** — JSON datasource plugin or CSV datasource for table panels

---

## Collection sequence

The Python tool runs the following in order on each execution:

1. SSH connect to first available cluster member
2. Preflight checks (root / expert mode, `$FWDIR`, `vsx` availability)
3. Platform info (`fw ver`, `cpinfo` JHF take, `uname`, `uptime`, `df`, `cplic`)
4. CPView historical CPU data (`/var/log/CPView_history/cpu` or `cpview -s -t` fallback)
5. Optional: `vsx fetch` (with `--fetch`)
6. Cluster topology (`local.vsall` → member IPs, VIP, management server)
7. VSX overview + VSID discovery (`vsx stat -v` / `-l`)
8. NCS topology (`vsx showncs` per VSID — file-redirect workaround for R82)
9. Per-VSID diagnostics via `vsenv` subshells (blades, CPU, routing, interfaces, SecureXL, connections)
10. CoreXL & affinity (`fw ctl multik stat`, `fw ctl affinity -l`)
11. Cluster health (`cphaprob stat/syncstat`, `cpstat ha -f all`)
12. HCP health check (`hcp -r all` + SFTP download of tar.gz report)
13. Optional: all-member health collection (with `--all-members`)
14. Health assessment — threshold rules applied using active profile
15. Delta comparison — current run compared against most recent snapshot
16. Snapshot saved to `.snapshot.json`
17. Render: console + log + HTML + JSON + CSV

---

## Lab topology

| Host | IP | Role |
|------|----|------|
| A-VSX-01 | 10.1.1.2 | Cluster member |
| A-VSX-02 | 10.1.1.3 | Cluster member |
| A-VSX-03 | 10.1.1.4 | Cluster member |
| A-SMS | 10.1.1.101 | Management server |
| A-GUI | 10.1.1.201 | Admin workstation (runs Python tool) |

**VSIDs:** 0 — VSX Gateway · 1 — A-VSW (Virtual Switch) · 2 — A-DMZ-GW · 3 — A-INT-GW · 4 — A-Corp-GW  
**Version:** R82 JHF Take 91 · **Cluster mode:** VSLS Primary Up · **Sync:** eth2

---

## Python package structure

```
python/
├── install.ps1                     # One-liner deployer for A-GUI
├── requirements.txt                # paramiko==3.5.1
├── vsx_diagnostics.py              # Entry point / CLI
└── vsx_diagnostics_py/
    ├── models/
    │   ├── data.py                 # Core dataclasses (ClusterTopology, VSIDInfo, HealthSummary ...)
    │   ├── snapshot.py             # RunSnapshot, DeltaItem, DeltaReport
    │   ├── member.py               # MemberSnapshot, MemberComparison
    │   └── thresholds.py           # ThresholdProfile — lab / virtual / production presets
    ├── transport/ssh.py            # Paramiko SSH — ExpertSession, connect_to_cluster(), SFTP
    ├── collectors/
    │   ├── topology.py             # Preflight + local.vsall
    │   ├── vsid_discovery.py       # vsx stat
    │   ├── ncs.py                  # vsx showncs (file-redirect workaround)
    │   ├── per_vsid.py             # vsenv subshell diagnostics
    │   ├── cluster_health.py       # cphaprob + cpstat
    │   ├── hcp.py                  # hcp -r all + SFTP download
    │   ├── platform.py             # fw ver, cpinfo, uptime, disk, cplic, CPView
    │   ├── cpview.py               # CPView history file reader + cmd fallback
    │   ├── member_health.py        # Per-member SSH collection (--all-members)
    │   └── member_comparator.py    # Cross-member diff (pure function, profile-aware)
    ├── parsers/                    # Pure functions: raw string → dataclass (no SSH)
    │   ├── vsx_stat.py
    │   ├── vsall.py
    │   ├── ncs_data.py
    │   ├── cphaprob.py
    │   ├── cpstat_ha.py
    │   ├── hcp.py
    │   ├── affinity.py
    │   ├── securexl.py
    │   └── iface_errors.py
    ├── delta/
    │   ├── comparator.py           # Pure compare(prev, curr, profile) → DeltaReport
    │   └── serialiser.py           # snapshot_from_summary(), save/load snapshot JSON
    ├── health/assessor.py          # Threshold rules → AttentionItem list (profile-aware)
    └── renderers/
        ├── text_builder.py         # Shared text engine (summary + delta + member sections)
        ├── console.py              # stdout summary + delta banner
        ├── logfile.py              # Full plain-text log + delta section
        ├── html.py                 # Self-contained HTML report
        └── export.py               # Machine-readable JSON + CSV export
```

Parsers, the delta comparator, and the member comparator are pure functions
(no SSH calls) — independently testable against captured output without a live cluster.

---

## Key technical notes

- `vsenv` kills its calling shell — all per-VS commands run in fresh `exec_command` channels
- `vsx showncs` suppresses stdout in subshell capture — output redirected to remote temp file
- `vsx showncs` on R82 requires `vsx fetch` to have run first — use `--fetch` on first run
- `fw ctl affinity -l` on R82 repeats entries per CoreXL instance — deduplicated on parse
- `enabled_blades` in a VSW context returns a verbose error string on R82 — normalised to short label
- R82 adds `READY` as a valid cluster state alongside `ACTIVE / STANDBY / BACKUP / DOWN`
- `cpstat ha -f all` PNOTE parsing scoped strictly to the `Problem Notification table` section only
- SecureXL status: R82 KPPAK pipe-table format — status is field index 3 after pipe-split
- `hcp -r all` output contains ANSI colour codes and `\r` Working lines — stripped before parsing
- Hyper-V LACP bond sync warnings downgraded from WARNING to INFO automatically (Bond Health heuristic)
- WARP interface (`wrp*`) errors downgraded to INFO on `--profile lab` — Hyper-V virtual switch noise
- CPView history files use two column formats: 6-column (R80/R81) and 7-column (R82) — both parsed
- Delta comparison is profile-aware — lab profile uses wider thresholds and a 5-minute suppression window
- Snapshot files are ASCII JSON, typically under 5 KB, stored alongside `.log` and `.html` per run

---

## License

MIT
