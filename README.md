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

Produces three output files on each run:
- **Console** — executive summary printed to screen
- **`.log`** — full plain-text diagnostic (all raw command output + summary)
- **`.html`** — self-contained dark-theme report with collapsible sections, RAG badges,
  cluster member state table, per-VSID status, and HCP health check findings

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

# Subsequent runs
python C:\vsx_diagnostics\vsx_diagnostics.py

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
| `--output-dir` | `C:\vsx_diagnostics\reports` | Directory for `.log` and `.html` output files |
| `--hcp-archive` | `C:\vsx_diagnostics\hcp_archive` | Directory for HCP tar.gz archives |
| `--port` | `22` | SSH port |
| `--timeout` | `15` | SSH connect timeout (seconds) |
| `--log-level` | `WARNING` | Verbosity: `DEBUG` / `INFO` / `WARNING` |

#### Output files

```
C:\vsx_diagnostics\
├── reports\
│   ├── vsx_diag_<hostname>_<timestamp>.log    # full plain-text diagnostic
│   └── vsx_diag_<hostname>_<timestamp>.html   # self-contained HTML report
└── hcp_archive\
    └── <hostname>\
        └── hcp_report_<hostname>_<timestamp>.tar.gz   # CP HCP report (extract → index.html)
```

---

## Collection sequence

The Python tool runs the following in order on each execution:

1. SSH connect to first available cluster member
2. Preflight checks (root / expert mode, `$FWDIR`, `vsx` availability)
3. Platform info (`fw ver`, `cpinfo` JHF take, `uname`, `uptime`, `df`, `cplic`)
4. Optional: `vsx fetch` (with `--fetch`)
5. Cluster topology (`local.vsall` → member IPs, VIP, management server)
6. VSX overview + VSID discovery (`vsx stat -v` / `-l`)
7. NCS topology (`vsx showncs` per VSID — file-redirect workaround for R82)
8. Per-VSID diagnostics via `vsenv` subshells (blades, CPU, routing, interfaces, SecureXL, connections)
9. CoreXL & affinity (`fw ctl multik stat`, `fw ctl affinity -l`)
10. Cluster health (`cphaprob stat/syncstat`, `cpstat ha -f all`)
11. HCP health check (`hcp -r all` + SFTP download of tar.gz report)
12. Health assessment — 16 threshold rules applied, ATTENTION items generated
13. Render: console summary + log file + HTML report

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
├── install.ps1                   # One-liner deployer for A-GUI
├── requirements.txt              # paramiko==3.5.1
├── vsx_diagnostics.py            # Entry point / CLI
└── vsx_diagnostics_py/
    ├── models/data.py            # All dataclasses (ClusterTopology, VSIDInfo, HealthSummary ...)
    ├── transport/ssh.py          # Paramiko SSH — ExpertSession, connect_to_cluster(), SFTP
    ├── collectors/               # One module per data collection area
    │   ├── topology.py           # Preflight + local.vsall
    │   ├── vsid_discovery.py     # vsx stat
    │   ├── ncs.py                # vsx showncs (file-redirect workaround)
    │   ├── per_vsid.py           # vsenv subshell diagnostics
    │   ├── cluster_health.py     # cphaprob + cpstat
    │   ├── hcp.py                # hcp -r all + SFTP download
    │   └── platform.py           # fw ver, cpinfo, uptime, disk, cplic
    ├── parsers/                  # Pure functions: raw string → dataclass (no SSH)
    │   ├── vsx_stat.py
    │   ├── vsall.py
    │   ├── ncs_data.py
    │   ├── cphaprob.py
    │   ├── cpstat_ha.py
    │   ├── hcp.py
    │   ├── affinity.py
    │   ├── securexl.py
    │   └── iface_errors.py
    ├── health/assessor.py        # 16 threshold rules → AttentionItem list
    └── renderers/
        ├── text_builder.py       # Shared text output engine
        ├── console.py            # stdout executive summary
        ├── logfile.py            # Full plain-text log
        └── html.py               # Self-contained HTML report
```

Parsers are pure functions (no SSH calls) making them independently testable
against captured gateway output without needing a live cluster.

---

## Key technical notes

These lessons from v18 bash development are encoded in the Python tool:

- `vsenv` kills its calling shell — all per-VS commands run in fresh `exec_command` channels
- `vsx showncs` suppresses stdout in subshell capture — output redirected to remote temp file
- `vsx showncs` on R82 requires `vsx fetch` to have run first — use `--fetch` on first run
- `fw ctl affinity -l` on R82 repeats entries per CoreXL instance — deduplicated on parse
- `enabled_blades` in a VSW context returns a verbose error string on R82 — normalised to short label
- R82 adds `READY` as a valid cluster state alongside `ACTIVE / STANDBY / BACKUP / DOWN`
- `cpstat ha -f all` PNOTE parsing scoped strictly to the `Problem Notification table` section only
- SecureXL status: R82 KPPAK pipe-table format — status is field index 3 after pipe-split
- `hcp -r all` output contains ANSI colour codes and `\r` Working lines — stripped before parsing
- Hyper-V LACP bond sync warnings are expected noise — downgraded from WARNING to INFO automatically

---

## License

MIT
