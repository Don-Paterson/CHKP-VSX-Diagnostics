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
./vsx_diagnostics.sh -f        # -f runs vsx fetch first (required for topology map)
./vsx_diagnostics.sh -q        # quiet: full log to file, only executive summary to terminal
./vsx_diagnostics.sh -f -q     # both
./vsx_diagnostics.sh -h        # usage
```

Log file is written to the same directory as the script.

---

### `python/vsx_diagnostics.py` — Run from A-GUI (Windows, in development)

Python 3.12 script that connects to the VSX cluster via SSH from the Windows admin
workstation. No tools need to be installed on the gateway.

Produces:
- Console output (full diagnostic detail)
- `.log` file (plain text, same content)
- `.html` file (self-contained report with collapsible sections and RAG status badges)

#### Requirements

- Python 3.12 from [python.org](https://www.python.org/downloads/) — tick **Add to PATH** during install
- One package: `pip install paramiko==3.5.1`

#### Deploy to A-GUI (one-liner)

```powershell
irm https://raw.githubusercontent.com/Don-Paterson/CHKP-VSX-Diagnostics/main/python/install.ps1 | iex
```

#### Usage

```powershell
python C:\vsx_diagnostics\vsx_diagnostics.py --hosts 10.1.1.2 10.1.1.3 10.1.1.4
```

The script tries each cluster member IP in order and connects to the first available.  
Credentials are prompted interactively on first run.

> **Status:** Active development — collectors and renderers being added iteratively.

---

## Lab topology

| Host | IP | Role |
|---|---|---|
| A-VSX-01 | 10.1.1.2 | Cluster member |
| A-VSX-02 | 10.1.1.3 | Cluster member |
| A-VSX-03 | 10.1.1.4 | Cluster member |
| A-SMS | 10.1.1.101 | Management server |
| A-GUI | 10.1.1.201 | Admin workstation (runs Python tool) |

**VSIDs:** 0 — VSX Gateway · 1 — A-VSW (Virtual Switch) · 2 — A-DMZ-GW · 3 — A-INT-GW  
**Version:** R82 JHF Take 91  
**Cluster sync:** eth2 · **Management:** eth0

---

## Python package structure

```
python/
├── install.ps1               # One-liner deployer for A-GUI
├── requirements.txt          # paramiko==3.5.1
├── vsx_diagnostics.py        # Entry point
└── vsx_diagnostics_py/
    ├── models/data.py        # All dataclasses (ClusterTopology, VSIDInfo, HealthSummary, ...)
    ├── transport/ssh.py      # Paramiko SSH wrapper — ExpertSession, connect_to_cluster()
    ├── collectors/           # One module per data collection area
    ├── parsers/              # Pure functions: raw string → dataclass
    ├── health/               # Assessor: collected data → ATTENTION items
    └── renderers/            # console.py · logfile.py · html.py
```

Parsers are pure functions (no SSH calls) making them independently testable
against captured gateway output.

---

## Key technical notes

These hard-won lessons from the bash v18 development carry into the Python version:

- `vsenv` kills its calling shell — all per-VS commands run in a fresh subshell
- `vsx showncs` suppresses stdout in subshell capture — use file redirection
- `vsx showncs` on R82 requires `vsx fetch` to have run first
- `fw ctl affinity -l` on R82 repeats entries per CoreXL instance — deduplicate
- `enabled_blades` in a VSW context returns a verbose error string on R82 — normalise
- R82 adds `READY` as a valid cluster state alongside `ACTIVE/STANDBY/BACKUP/DOWN`
- `cpstat ha -f all` PNOTE parsing must be scoped to the `Problem Notification table` section only
- SecureXL status: R82 KPPAK table format, pipe-delimited, status is field index 3

---

## License

MIT
