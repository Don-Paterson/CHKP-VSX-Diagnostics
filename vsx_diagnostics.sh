#!/bin/bash
# VSX Gateway Diagnostics
# Collects health and status data across all virtual systems
# Usage: bash ./vsx_diagnostics.sh [-o logfile] [-f] [-q]

set -uo pipefail

# --- Defaults ---
LOGFILE=""
DO_FETCH=false
QUIET=false

# --- Usage ---
usage() {
    echo "Usage: $(basename "$0") [-o logfile] [-f] [-q]"
    echo "  -o logfile   Tee all output to a timestamped log file"
    echo "  -f           Run 'vsx fetch' before diagnostics (requires Management Server reachability)"
    echo "  -q           Quiet mode - suppress section banners"
    exit 1
}

# --- Parse arguments ---
while getopts "o:fqh" opt; do
    case $opt in
        o) LOGFILE="$OPTARG" ;;
        f) DO_FETCH=true ;;
        q) QUIET=false ;;
        h) usage ;;
        *) usage ;;
    esac
done

# --- Logging setup ---
if [[ -n "$LOGFILE" ]]; then
    exec > >(tee -a "$LOGFILE") 2>&1
    echo "Logging to: $LOGFILE"
fi

# --- Helper functions ---
banner() {
    [[ "$QUIET" == true ]] && return
    echo ""
    echo "=============================="
    echo "$1"
    echo "=============================="
}

section() {
    [[ "$QUIET" == true ]] && return
    echo ""
    echo ">> $1"
}

run_cmd() {
    local label="$1"
    shift
    section "$label"
    if command -v "${1}" &>/dev/null; then
        "$@" 2>&1
    else
        echo "   [skipped] ${1} not found"
    fi
}

# --- Preflight checks ---
if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: This script must run as root (Expert mode)." >&2
    exit 1
fi

for profile in /etc/profile.d/CP.sh /etc/profile.d/vsenv.sh; do
    if [[ -f "$profile" ]]; then
        source "$profile"
    else
        echo "ERROR: Required profile not found: $profile" >&2
        exit 1
    fi
done

# --- Timestamp ---
echo "VSX Diagnostics - $(hostname) - $(date '+%Y-%m-%d %H:%M:%S %Z')"

# --- Optional: fetch VSX configuration from Management ---
if [[ "$DO_FETCH" == true ]]; then
    banner "VSX Configuration Fetch"
    if command -v vsx &>/dev/null; then
        vsx fetch 2>&1
        rc=$?
        if [[ $rc -ne 0 ]]; then
            echo "WARNING: vsx fetch returned exit code $rc"
        fi
    else
        echo "   [skipped] vsx command not available"
    fi
fi

# --- Discover VSIDs dynamically ---
banner "VSID Discovery"
if command -v vsx &>/dev/null && vsx stat -l &>/dev/null; then
    VSIDS=$(vsx stat -l 2>/dev/null | awk '/^[0-9]/ {print $1}' | sort -n)
    echo "Discovered VSIDs: $(echo $VSIDS | tr '\n' ' ')"
else
    VSIDS="0"
    echo "WARNING: Could not enumerate VSIDs, falling back to VS0 only"
fi

# --- Per-VS diagnostics ---
for vs in $VSIDS; do
    banner "VSID $vs"

    if ! vsenv "$vs" &>/dev/null; then
        echo "WARNING: Failed to switch to VSID $vs, skipping"
        continue
    fi

    echo "Context: VSID $vs ($(hostname 2>/dev/null || echo 'unknown'))"

    run_cmd "Top 5 processes by memory" ps aux --sort=-%mem
    # Trim to header + 5 rows handled below? No — ps output is useful in full
    # for diagnostics. Keeping it untruncated.

    run_cmd "Memory statistics" free -m
    run_cmd "CPU statistics (1-second sample)" mpstat 1 1
    run_cmd "Routing table" ip route
    run_cmd "Interface addresses" ip addr
    run_cmd "Interface statistics" ip -s link
    run_cmd "SecureXL status" fwaccel stat
    run_cmd "SecureXL template status" fwaccel stats -s
    run_cmd "Connections table summary" fw tab -t connections -s
    run_cmd "NAT table summary" fw tab -t fwx_alloc -s

    echo "------------------------------"
done

# --- Return to VS0 for global commands ---
vsenv 0 &>/dev/null

# --- Cluster diagnostics (conditional) ---
banner "Cluster Status"
if cphaprob stat &>/dev/null; then
    section "Cluster member state"
    cphaprob stat 2>&1

    section "Cluster interfaces"
    cphaprob -a if 2>&1

    section "Cluster synchronisation"
    cphaprob syncstat 2>&1
else
    echo "   [skipped] Not a cluster member or ClusterXL not active"
fi

# --- Global VSX status ---
banner "Global VSX Status"
run_cmd "VSX overview" vsx stat
run_cmd "VSX policy status (verbose)" fw vsx stat -v

# --- Hardware / platform ---
banner "Platform Information"
run_cmd "Check Point version" cpinfo -y all
run_cmd "Kernel build" uname -r
run_cmd "Uptime" uptime
run_cmd "Disk usage" df -h

echo ""
echo "=============================="
echo "Diagnostics complete - $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=============================="
