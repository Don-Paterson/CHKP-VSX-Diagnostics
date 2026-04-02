#!/bin/bash
# vsx_diagnostics.sh
# VSX Gateway Health Diagnostics
# Requires: Expert mode, VS0 context
# Usage: ./vsx_diagnostics.sh [-o logfile]

# --- Unbound variable safety (CP profile scripts reference this) ---
export OLD_FWDIR="${OLD_FWDIR:-}"

set -uo pipefail

# --- Defaults ---
LOGFILE=""
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
DEFAULT_LOG="/var/log/vsx_diag_${TIMESTAMP}.log"

# --- Usage ---
usage() {
    echo "Usage: $(basename "$0") [-o logfile]"
    echo "  -o logfile   Write output to logfile (default: $DEFAULT_LOG)"
    echo "  -h           Show this help"
    exit 1
}

# --- Parse arguments ---
while getopts "o:h" opt; do
    case $opt in
        o) LOGFILE="$OPTARG" ;;
        h) usage ;;
        *) usage ;;
    esac
done

# Use default log path if none specified
LOGFILE="${LOGFILE:-$DEFAULT_LOG}"

# Start logging
exec > >(tee -a "$LOGFILE") 2>&1
echo "Logging to: $LOGFILE"

# --- Helper functions ---
banner() {
    echo ""
    echo "============================================================"
    echo "  $1"
    echo "============================================================"
}

section() {
    echo ""
    echo ">> $1"
    echo "--------------------------------------------------------------"
}

run_cmd() {
    local label="$1"
    shift
    section "$label"
    if command -v "${1}" &>/dev/null; then
        "$@" 2>&1 || echo "   [command returned non-zero exit code]"
    else
        echo "   [skipped] ${1} not found in PATH"
    fi
}

# --- Preflight checks ---
banner "Preflight Checks"

# Must be root
if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Must run in Expert mode as root." >&2
    exit 1
fi
echo "OK: Running as root"

# Source CP environment profiles
for profile in /etc/profile.d/CP.sh /etc/profile.d/vsenv.sh; do
    if [[ -f "$profile" ]]; then
        # shellcheck disable=SC1090
        source "$profile" 2>/dev/null || true
        echo "OK: Sourced $profile"
    else
        echo "WARNING: Profile not found: $profile"
    fi
done

# Confirm FWDIR is set
if [[ -z "${FWDIR:-}" ]]; then
    echo "ERROR: \$FWDIR is not set. CP environment may not be loaded." >&2
    exit 1
fi
echo "OK: FWDIR=$FWDIR"

# Confirm vsx command is available
if ! command -v vsx &>/dev/null; then
    echo "ERROR: 'vsx' command not found. Is this a VSX gateway?" >&2
    exit 1
fi
echo "OK: vsx command available"

# --- Script header ---
banner "VSX Gateway Health Diagnostics"
echo "Gateway : $(hostname)"
echo "Date    : $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "FWDIR   : $FWDIR"
echo "Version :"
fw ver 2>/dev/null | head -1 || echo "  [unavailable]"

# --- Global VSX overview ---
banner "VSX Overview"
vsx stat -v 2>&1

# --- VSID discovery ---
banner "VSID Discovery"
VSIDS=$(vsx stat -l 2>/dev/null | awk '/^VSID:/ {print $2}' | sort -n)

if [[ -z "$VSIDS" ]]; then
    echo "ERROR: No VSIDs discovered. Cannot continue." >&2
    exit 1
fi

echo "Discovered VSIDs: $(echo $VSIDS | tr '\n' ' ')"

# --- Summary table initialisation ---
declare -A SUMMARY_CPU
declare -A SUMMARY_MEM
declare -A SUMMARY_CONN
declare -A SUMMARY_SACCEL
declare -A SUMMARY_NAME

# --- Per-VSID diagnostics ---
for vs in $VSIDS; do
    banner "VSID $vs Diagnostics"

    # Switch VS context
    if ! vsenv "$vs" &>/dev/null; then
        echo "WARNING: Failed to switch to VSID $vs - skipping"
        SUMMARY_NAME[$vs]="[switch failed]"
        continue
    fi

    # Capture VS name from vsx stat -l
    VS_NAME=$(vsx stat -l 2>/dev/null | awk "/^VSID:.*[[:space:]]${vs}$/{found=1} found && /^Name:/{print \$2; exit}")
    SUMMARY_NAME[$vs]="${VS_NAME:-VSID-$vs}"
    echo "Context : VSID $vs - ${SUMMARY_NAME[$vs]}"

    # --- CPU (top processes) ---
    section "Top 5 Processes by CPU"
    ps aux --sort=-%cpu 2>/dev/null | head -6 || echo "  [unavailable]"

    # Capture top CPU process for summary
    TOP_CPU=$(ps aux --sort=-%cpu 2>/dev/null | awk 'NR==2 {print $3"% ("$11")"}')
    SUMMARY_CPU[$vs]="${TOP_CPU:-n/a}"

    # --- Memory ---
    section "Memory Statistics"
    free -m 2>/dev/null || echo "  [unavailable]"

    section "Top 5 Processes by Memory"
    ps aux --sort=-%mem 2>/dev/null | head -6 || echo "  [unavailable]"

    # Capture memory used % for summary
    MEM_SUMMARY=$(free -m 2>/dev/null | awk '/^Mem:/ {printf "%dMB used / %dMB total (%.0f%%)", $3, $2, ($3/$2)*100}')
    SUMMARY_MEM[$vs]="${MEM_SUMMARY:-n/a}"

    # --- CPU performance (mpstat if available) ---
    section "CPU Performance (1-second sample)"
    if command -v mpstat &>/dev/null; then
        mpstat 1 1 2>&1
    else
        echo "  [mpstat not available - install sysstat]"
        # Fallback: /proc/stat snapshot
        echo "  Fallback: top -bn1 CPU line:"
        top -bn1 2>/dev/null | grep "^%Cpu" || echo "  [unavailable]"
    fi

    # --- Routing table ---
    section "Routing Table"
    if command -v ip &>/dev/null; then
        ip route 2>&1
    else
        netstat -rn 2>&1 || echo "  [unavailable]"
    fi

    # --- Network interfaces ---
    section "Interface Addresses"
    if command -v ip &>/dev/null; then
        ip addr 2>&1
    else
        ifconfig -a 2>&1 || echo "  [unavailable]"
    fi

    # --- Interface statistics ---
    section "Interface Statistics (RX/TX errors and drops)"
    if command -v ip &>/dev/null; then
        ip -s link 2>&1
    else
        netstat -i 2>&1 || echo "  [unavailable]"
    fi

    # --- SecureXL ---
    section "SecureXL Acceleration Status"
    if command -v fwaccel &>/dev/null; then
        fwaccel stat 2>&1
        SACCEL=$(fwaccel stat 2>/dev/null | awk '/^Accelerator Status/ {print $NF}')
        SUMMARY_SACCEL[$vs]="${SACCEL:-n/a}"
    else
        echo "  [fwaccel not available in this VS context]"
        SUMMARY_SACCEL[$vs]="n/a"
    fi

    section "SecureXL Template Statistics"
    if command -v fwaccel &>/dev/null; then
        fwaccel stats -s 2>&1
    else
        echo "  [fwaccel not available in this VS context]"
    fi

    # --- Connection tables ---
    section "Connections Table Summary"
    fw tab -t connections -s 2>&1 || echo "  [unavailable]"

    CONN_SUMMARY=$(fw tab -t connections -s 2>/dev/null | awk 'NR>1 {sum+=$4} END {print sum" current connections"}')
    SUMMARY_CONN[$vs]="${CONN_SUMMARY:-n/a}"

    section "NAT Table Summary"
    fw tab -t fwx_alloc -s 2>&1 || echo "  [unavailable]"

    echo ""
    echo "-- End VSID $vs --"
done

# --- Return to VS0 ---
vsenv 0 &>/dev/null
echo ""
echo "Returned to VS0 context"

# --- Cluster diagnostics ---
banner "Cluster Status"
if cphaprob stat &>/dev/null; then
    section "Cluster Member State"
    cphaprob stat 2>&1

    section "Cluster Interfaces"
    cphaprob -a if 2>&1

    section "Cluster Synchronisation"
    cphaprob syncstat 2>&1
else
    echo "  [ClusterXL not active or not a cluster member - skipped]"
fi

# --- Platform information ---
banner "Platform Information"
run_cmd "Kernel build" uname -r
run_cmd "System uptime" uptime
run_cmd "Disk usage" df -h

# --- Summary table ---
banner "Health Summary"
printf "%-6s %-20s %-30s %-35s %-12s %-10s\n" \
    "VSID" "Name" "Memory" "Top CPU Process" "Connections" "SecureXL"
printf "%-6s %-20s %-30s %-35s %-12s %-10s\n" \
    "------" "--------------------" "------------------------------" \
    "-----------------------------------" "------------" "----------"

for vs in $(echo "${!SUMMARY_NAME[@]}" | tr ' ' '\n' | sort -n); do
    printf "%-6s %-20s %-30s %-35s %-12s %-10s\n" \
        "$vs" \
        "${SUMMARY_NAME[$vs]:-n/a}" \
        "${SUMMARY_MEM[$vs]:-n/a}" \
        "${SUMMARY_CPU[$vs]:-n/a}" \
        "${SUMMARY_CONN[$vs]:-n/a}" \
        "${SUMMARY_SACCEL[$vs]:-n/a}"
done

echo ""
echo "============================================================"
echo "  Diagnostics complete - $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  Log saved to: $LOGFILE"
echo "============================================================"
