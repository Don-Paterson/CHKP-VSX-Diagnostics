#!/bin/bash
# vsx_diagnostics.sh  v9
# VSX Gateway & Cluster Health Diagnostics with Topology Mapping
# Requires: Expert mode (root), VS0 context
# Usage: ./vsx_diagnostics.sh [-o logfile] [-f]
#
# vsenv uses exec internally which kills the calling shell,
# so all vsenv calls are wrapped in subshells via run_in_vs.

SCRIPT_VERSION="v9"

set -o pipefail

export OLD_FWDIR="${OLD_FWDIR:-}"

# --- Defaults ---
LOGFILE=""
DO_FETCH=false
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
DEFAULT_LOG="/var/log/vsx_diag_${TIMESTAMP}.log"

usage() {
    echo "Usage: $(basename "$0") [-o logfile] [-f]"
    echo "  -o logfile   Write output to logfile (default: $DEFAULT_LOG)"
    echo "  -f           Run 'vsx fetch' before diagnostics"
    echo "  -h           Show this help"
    exit 1
}

while getopts "o:fh" opt; do
    case $opt in
        o) LOGFILE="$OPTARG" ;;
        f) DO_FETCH=true ;;
        h) usage ;;
        *) usage ;;
    esac
done

LOGFILE="${LOGFILE:-$DEFAULT_LOG}"

exec > >(tee -a "$LOGFILE") 2>&1
echo "Logging to: $LOGFILE"
echo "Script version: $SCRIPT_VERSION"

# ==========================================================================
#  Helper functions
# ==========================================================================

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
    local label="$1"; shift
    section "$label"
    if command -v "${1}" &>/dev/null; then
        "$@" 2>&1 || echo "   [command returned non-zero]"
    else
        echo "   [skipped] ${1} not found"
    fi
}

# Run a command inside a VS context via subshell (vsenv-safe).
run_in_vs() {
    local target_vs="$1"; shift
    (
        source /etc/profile.d/CP.sh 2>/dev/null
        source /etc/profile.d/vsenv.sh 2>/dev/null
        vsenv "$target_vs" >/dev/null 2>&1
        "$@"
    )
}

# ==========================================================================
#  Preflight checks
# ==========================================================================

banner "Preflight Checks"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run in Expert mode as root." >&2
    exit 1
fi
echo "OK: Running as root"

for profile in /etc/profile.d/CP.sh /etc/profile.d/vsenv.sh; do
    if [ -f "$profile" ]; then
        . "$profile" 2>/dev/null || true
        echo "OK: Sourced $profile"
    else
        echo "WARNING: Profile not found: $profile"
    fi
done

if [ -z "${FWDIR:-}" ]; then
    echo "ERROR: \$FWDIR is not set." >&2
    exit 1
fi
echo "OK: FWDIR=$FWDIR"

if ! command -v vsx &>/dev/null; then
    echo "ERROR: 'vsx' command not found." >&2
    exit 1
fi
echo "OK: vsx command available"

# ==========================================================================
#  Header
# ==========================================================================

banner "VSX Gateway Health Diagnostics"
echo "Script  : $SCRIPT_VERSION"
echo "Gateway : $(hostname)"
echo "Date    : $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "FWDIR   : $FWDIR"

# Capture version info for summary
CP_VERSION=$(fw ver 2>/dev/null | head -1) || CP_VERSION="unknown"
echo "Version : $CP_VERSION"

# Capture JHF take number
JHF_TAKE=$(cpinfo -y all 2>/dev/null | grep 'HOTFIX_R.*JUMBO_HF_MAIN' \
    | sed -n 's/.*Take:[[:space:]]*\([0-9]*\).*/\1/p' | head -1)

# Capture uptime and load
UPTIME_STR=$(uptime 2>/dev/null) || UPTIME_STR=""
LOAD_AVG=$(echo "$UPTIME_STR" | sed -n 's/.*load average:[[:space:]]*\(.*\)/\1/p')

# ==========================================================================
#  Optional: vsx fetch
# ==========================================================================

if [ "$DO_FETCH" = "true" ]; then
    banner "VSX Configuration Fetch"
    run_in_vs 0 vsx fetch
    rc=$?
    [ $rc -ne 0 ] && echo "WARNING: vsx fetch returned exit code $rc"
fi

# ==========================================================================
#  Cluster topology (from local.vsall)
# ==========================================================================

banner "Cluster Topology"

VSALL_FILE="${FWDIR}/state/local/VSX/local.vsall"
MEMBER_COUNT=0
CLUSTER_MEMBERS=""
MGMT_ADDR=""
CLUSTER_VIP=""
ICN_NET=""
ICN_MASK=""
THIS_HOST=$(hostname)

if [ -f "$VSALL_FILE" ]; then
    CLUSTER_MEMBERS=$(sed -n 's/^\[\([A-Za-z0-9_-]*\):\].*/\1/p' "$VSALL_FILE" | sort -u)
    MEMBER_COUNT=$(echo "$CLUSTER_MEMBERS" | wc -l | tr -d ' ')

    echo "Cluster ($MEMBER_COUNT members)"
    echo ""

    for member in $CLUSTER_MEMBERS; do
        MEMBER_MGMT_IP=$(grep "^\[${member}:\]interface set dev eth0 " "$VSALL_FILE" \
            | sed -n 's/.*address \([0-9][0-9.]*\).*/\1/p' | head -1)
        MEMBER_SYNC_IP=$(grep "^\[${member}:\]interface set dev eth2 " "$VSALL_FILE" \
            | sed -n 's/.*address \([0-9][0-9.]*\).*/\1/p' | head -1)
        # Store for summary
        eval "MEMBER_${member}_MGMT=\"${MEMBER_MGMT_IP:-?}\""
        eval "MEMBER_${member}_SYNC=\"${MEMBER_SYNC_IP:-?}\""
        MARKER=""
        [ "$member" = "$THIS_HOST" ] && MARKER="  <-- THIS GATEWAY"
        echo "  $member  mgmt=${MEMBER_MGMT_IP:-?}  sync=${MEMBER_SYNC_IP:-?}${MARKER}"
    done

    MGMT_ADDR=$(sed -n 's/.*masters_addresses \([0-9][0-9.]*\).*/\1/p' "$VSALL_FILE" | head -1)
    CLUSTER_VIP=$(grep 'interface set dev eth0 ' "$VSALL_FILE" \
        | sed -n 's/.*cluster_ip \([0-9][0-9.]*\).*/\1/p' | grep -v '^0\.0\.0\.0$' | head -1)
    ICN_NET=$(sed -n 's/.*route set funny \([0-9][0-9.]*\).*/\1/p' "$VSALL_FILE" | head -1)
    ICN_MASK=$(grep 'route set funny' "$VSALL_FILE" \
        | sed -n 's/.*netmask \([0-9][0-9.]*\).*/\1/p' | head -1)

    echo ""
    echo "  Cluster VIP: ${CLUSTER_VIP:-unknown}"
    echo "  Management Server: ${MGMT_ADDR:-unknown}"
    [ -n "$ICN_NET" ] && echo "  Internal Comms Network (Funny IPs): ${ICN_NET}/${ICN_MASK}"
else
    echo "WARNING: $VSALL_FILE not found"
fi

# ==========================================================================
#  VSX overview
# ==========================================================================

banner "VSX Overview"
VSX_STAT_V=$(vsx stat -v 2>/dev/null) || VSX_STAT_V=""
echo "$VSX_STAT_V"

# Extract total connections and limit from vsx stat -v
TOTAL_CONN=$(echo "$VSX_STAT_V" | sed -n 's/.*Total connections \[current \/ limit\]:[[:space:]]*\([0-9]*\).*/\1/p')
TOTAL_CONN_LIMIT=$(echo "$VSX_STAT_V" | sed -n 's/.*Total connections \[current \/ limit\]:[[:space:]]*[0-9]* \/ \([0-9]*\).*/\1/p')
VS_LICENSE_COUNT=$(echo "$VSX_STAT_V" | sed -n 's/.*Number of Virtual Systems allowed by license:[[:space:]]*\([0-9]*\).*/\1/p')

# ==========================================================================
#  VSID discovery and classification
# ==========================================================================

banner "Virtual Device Discovery"

VSX_STAT_L=$(vsx stat -l 2>/dev/null) || VSX_STAT_L=""

TMPPARSE=$(mktemp /tmp/vsx_parse.XXXXXX)

awk '
/^VSID:/ { vsid=$2; vtype=""; name=""; policy=""; conn=""; peak=""; limit="" }
/^Type:/ { $1=""; sub(/^[[:space:]]*/,""); vtype=$0 }
/^Name:/ { $1=""; sub(/^[[:space:]]*/,""); name=$0 }
/^Security Policy:/ { sub(/^Security Policy:[[:space:]]*/,""); policy=$0 }
/^Connections number:/ { conn=$NF }
/^Connections peak:/ { peak=$NF }
/^Connections limit:/ { limit=$NF; print vsid "|" vtype "|" name "|" policy "|" conn "|" peak "|" limit }
' <<< "$VSX_STAT_L" > "$TMPPARSE"

if [ ! -s "$TMPPARSE" ]; then
    echo "ERROR: No VSIDs discovered." >&2
    rm -f "$TMPPARSE"
    exit 1
fi

ALL_VSIDS=""
VS_GW_COUNT=0; VS_SW_COUNT=0; VS_RTR_COUNT=0
VS_GW_IDS=""; VS_SW_IDS=""; VS_RTR_IDS=""

while IFS='|' read -r vsid vtype vname vpolicy vconn vpeak vlimit; do
    ALL_VSIDS="$ALL_VSIDS $vsid"
    eval "VS_${vsid}_TYPE=\"$vtype\""
    eval "VS_${vsid}_NAME=\"$vname\""
    eval "VS_${vsid}_POLICY=\"$vpolicy\""
    eval "VS_${vsid}_CONN=\"$vconn\""
    eval "VS_${vsid}_PEAK=\"$vpeak\""
    eval "VS_${vsid}_LIMIT=\"$vlimit\""

    case "$vtype" in
        "Virtual System")   VS_GW_COUNT=$((VS_GW_COUNT+1)); VS_GW_IDS="$VS_GW_IDS $vsid" ;;
        "Virtual Switch")   VS_SW_COUNT=$((VS_SW_COUNT+1)); VS_SW_IDS="$VS_SW_IDS $vsid" ;;
        "Virtual Router")   VS_RTR_COUNT=$((VS_RTR_COUNT+1)); VS_RTR_IDS="$VS_RTR_IDS $vsid" ;;
    esac
done < "$TMPPARSE"

rm -f "$TMPPARSE"
ALL_VSIDS=$(echo $ALL_VSIDS)

echo "Discovered virtual devices:"
echo ""
printf "  %-6s %-22s %-18s %-24s %s\n" \
    "VSID" "Name" "Type" "Policy" "Conn/Peak/Limit"
printf "  %-6s %-22s %-18s %-24s %s\n" \
    "------" "----------------------" "------------------" \
    "------------------------" "---------------"

for vs in $ALL_VSIDS; do
    eval "vtype=\${VS_${vs}_TYPE:-n/a}"
    eval "vname=\${VS_${vs}_NAME:-n/a}"
    eval "vpolicy=\${VS_${vs}_POLICY:-n/a}"
    eval "vconn=\${VS_${vs}_CONN:-?}"
    eval "vpeak=\${VS_${vs}_PEAK:-?}"
    eval "vlimit=\${VS_${vs}_LIMIT:-?}"
    printf "  %-6s %-22s %-18s %-24s %s/%s/%s\n" \
        "$vs" "$vname" "$vtype" "$vpolicy" "$vconn" "$vpeak" "$vlimit"
done

echo ""
echo "  Virtual Systems (firewalls): $VS_GW_COUNT  (VSIDs:${VS_GW_IDS:- none})"
echo "  Virtual Switches:            $VS_SW_COUNT  (VSIDs:${VS_SW_IDS:- none})"
echo "  Virtual Routers:             $VS_RTR_COUNT  (VSIDs:${VS_RTR_IDS:- none})"

# ==========================================================================
#  Cache NCS data for topology and later reuse
# ==========================================================================

TMPNCS_DIR=$(mktemp -d /tmp/vsx_ncs.XXXXXX)

for vs in $ALL_VSIDS; do
    [ "$vs" -eq 0 ] && continue
    vsx showncs "$vs" > "${TMPNCS_DIR}/${vs}.ncs" 2>/dev/null || true
done

# ==========================================================================
#  Topology map (built from cached NCS data)
# ==========================================================================

banner "Topology Map"

for vs in $ALL_VSIDS; do
    [ "$vs" -eq 0 ] && continue

    eval "vname=\${VS_${vs}_NAME:-VSID-$vs}"
    eval "vtype=\${VS_${vs}_TYPE:-unknown}"

    NCS_FILE="${TMPNCS_DIR}/${vs}.ncs"
    if [ ! -s "$NCS_FILE" ]; then
        echo "  VSID $vs - $vname: [showncs unavailable]"
        continue
    fi

    echo ""
    echo "  VSID $vs - $vname ($vtype)"
    echo "  ----------------------------------------"

    echo "  Interfaces:"
    grep 'interface set dev' "$NCS_FILE" | while IFS= read -r line; do
        dev=$(echo "$line" | sed -n 's/.*dev \([^ ]*\).*/\1/p')
        addr=$(echo "$line" | sed -n 's/.*address \([0-9][0-9.]*\).*/\1/p')
        mask=$(echo "$line" | sed -n 's/.*netmask \([0-9][0-9.]*\).*/\1/p')
        cip=$(echo "$line" | sed -n 's/.*cluster_ip \([0-9][0-9.]*\).*/\1/p')
        cmask=$(echo "$line" | sed -n 's/.*cluster_mask \([0-9][0-9.]*\).*/\1/p')
        [ -z "$dev" ] && continue
        if [ "$addr" = "0.0.0.0" ] && { [ -z "$cip" ] || [ "$cip" = "0.0.0.0" ]; }; then
            continue
        fi
        if [ -n "$cip" ] && [ "$cip" != "0.0.0.0" ]; then
            echo "    $dev  local=$addr  cluster=$cip/$cmask"
        elif [ "$addr" != "0.0.0.0" ]; then
            echo "    $dev  local=$addr/$mask"
        fi
    done

    if grep -q 'warp create' "$NCS_FILE" 2>/dev/null; then
        echo "  WARP Interconnections:"
        grep 'warp create' "$NCS_FILE" | while IFS= read -r line; do
            wa=$(echo "$line" | sed -n 's/.*name_a \([^ ]*\).*/\1/p')
            wb=$(echo "$line" | sed -n 's/.*name_b \([^ ]*\).*/\1/p')
            [ -n "$wa" ] && echo "    WARP pair: $wa <---> $wb"
        done
    fi

    if grep -q 'route set dest' "$NCS_FILE" 2>/dev/null; then
        echo "  Static Routes:"
        grep 'route set dest' "$NCS_FILE" | while IFS= read -r line; do
            dest=$(echo "$line" | sed -n 's/.*dest \([0-9][0-9.]*\).*/\1/p')
            rmask=$(echo "$line" | sed -n 's/.*netmask \([0-9][0-9.]*\).*/\1/p')
            gw=$(echo "$line" | sed -n 's/.*gw \([0-9][0-9.]*\).*/\1/p')
            rdev=$(echo "$line" | sed -n 's/.*dev \([^ ]*\).*/\1/p')
            [ -z "$dest" ] && continue
            if [ -n "$gw" ]; then
                echo "    $dest/$rmask via $gw"
            elif [ -n "$rdev" ]; then
                echo "    $dest/$rmask dev $rdev"
            fi
        done
    fi

    if grep -q 'bridge attach' "$NCS_FILE" 2>/dev/null; then
        echo "  Bridge Members:"
        grep 'bridge attach' "$NCS_FILE" | while IFS= read -r line; do
            bname=$(echo "$line" | sed -n 's/.*name \([^ ]*\).*/\1/p')
            bdev=$(echo "$line" | sed -n 's/.*dev \([^ ]*\).*/\1/p')
            [ -n "$bdev" ] && echo "    $bdev attached to bridge $bname"
        done
    fi

    echo ""
done

# --- ASCII diagram ---
section "Interconnection Diagram"

echo ""
echo "  Physical Host: VSX Gateway Cluster (${MEMBER_COUNT} members)"
echo "  ==================================="
echo ""

if [ -n "$CLUSTER_MEMBERS" ] && [ -f "$VSALL_FILE" ]; then
    echo "  Cluster Members:"
    for member in $CLUSTER_MEMBERS; do
        MIP=$(grep "^\[${member}:\]interface set dev eth0 " "$VSALL_FILE" \
            | sed -n 's/.*address \([0-9][0-9.]*\).*/\1/p' | head -1)
        SIP=$(grep "^\[${member}:\]interface set dev eth2 " "$VSALL_FILE" \
            | sed -n 's/.*address \([0-9][0-9.]*\).*/\1/p' | head -1)
        echo "    $member  eth0=${MIP:-?} (mgmt)  eth2=${SIP:-?} (sync)"
    done
    echo ""
fi

echo "  Virtual Device Interconnections (on ${THIS_HOST}):"
echo ""

for vs in $VS_GW_IDS; do
    eval "vname=\${VS_${vs}_NAME:-VS$vs}"
    eval "vpolicy=\${VS_${vs}_POLICY:-none}"
    eval "vconn=\${VS_${vs}_CONN:-?}"
    eval "vpeak=\${VS_${vs}_PEAK:-?}"
    eval "vlimit=\${VS_${vs}_LIMIT:-?}"

    NCS_FILE="${TMPNCS_DIR}/${vs}.ncs"
    wrp_name=""
    wrpj_name=""
    wrp_cip=""

    if [ -s "$NCS_FILE" ]; then
        wrp_name=$(grep 'warp create' "$NCS_FILE" \
            | sed -n 's/.*name_a \([^ ]*\).*/\1/p' | head -1)
        wrpj_name=$(grep 'warp create' "$NCS_FILE" \
            | sed -n 's/.*name_b \([^ ]*\).*/\1/p' | head -1)
        if [ -n "$wrp_name" ]; then
            wrp_cip=$(grep "interface set dev ${wrp_name} " "$NCS_FILE" \
                | sed -n 's/.*cluster_ip \([0-9][0-9.]*\).*/\1/p' | head -1)
        fi
    fi

    # Store WARP info for executive summary
    eval "WARP_${vs}_WRP=\"${wrp_name:-?}\""
    eval "WARP_${vs}_WRPJ=\"${wrpj_name:-?}\""
    eval "WARP_${vs}_CIP=\"${wrp_cip:-?}\""

    ext_ifaces=""
    if [ -s "$NCS_FILE" ]; then
        ext_ifaces=$(grep 'interface set dev' "$NCS_FILE" | while IFS= read -r line; do
            dev=$(echo "$line" | sed -n 's/.*dev \([^ ]*\).*/\1/p')
            addr=$(echo "$line" | sed -n 's/.*address \([0-9][0-9.]*\).*/\1/p')
            cip=$(echo "$line" | sed -n 's/.*cluster_ip \([0-9][0-9.]*\).*/\1/p')
            cmask=$(echo "$line" | sed -n 's/.*cluster_mask \([0-9][0-9.]*\).*/\1/p')
            [ -z "$dev" ] && continue
            [ "$dev" = "${wrp_name:-}" ] && continue
            [ "$dev" = "${wrpj_name:-}" ] && continue
            [ "$addr" = "0.0.0.0" ] && continue
            if [ -n "$cip" ] && [ "$cip" != "0.0.0.0" ]; then
                echo "$dev cluster=$cip/$cmask"
            fi
        done)
    fi

    echo "    +------------------------------------------------+"
    printf "    | VSID %-3s  %-37s|\n" "$vs" "$vname"
    printf "    | Policy: %-39s|\n" "$vpolicy"
    printf "    | Conns: %-5s  Peak: %-5s  Limit: %-13s|\n" "$vconn" "$vpeak" "$vlimit"

    if [ -n "$ext_ifaces" ]; then
        echo "$ext_ifaces" | while IFS= read -r ifline; do
            [ -z "$ifline" ] && continue
            printf "    |   %-45s|\n" "$ifline"
        done
    fi

    echo "    +------------------------------------------------+"
    echo "          |"
    echo "          | WARP: ${wrp_name:-?} (${wrp_cip:-?}) <---> ${wrpj_name:-?}"
    echo "          |"
    echo "          v"
done

for vs in $VS_SW_IDS; do
    eval "vname=\${VS_${vs}_NAME:-VSW}"

    echo "    +------------------------------------------------+"
    printf "    | VSID %-3s  %-37s|\n" "$vs" "$vname (Virtual Switch)"
    echo "    |   Bridge: br1                                  |"
    echo "    |   Physical uplink: eth3                        |"

    for gw_vs in $VS_GW_IDS; do
        GW_NCS="${TMPNCS_DIR}/${gw_vs}.ncs"
        wrpj=""
        if [ -s "$GW_NCS" ]; then
            wrpj=$(grep 'warp create' "$GW_NCS" \
                | sed -n 's/.*name_b \([^ ]*\).*/\1/p' | head -1)
        fi
        eval "gwname=\${VS_${gw_vs}_NAME:-VS$gw_vs}"
        if [ -n "$wrpj" ]; then
            printf "    |   Junction: %-12s from %-18s|\n" "$wrpj" "$gwname"
        fi
    done

    echo "    +------------------------------------------------+"
    echo "          |"
    echo "          | eth3"
    echo "          v"
    echo "    [ External / Physical Network ]"
done

echo ""

# ==========================================================================
#  CoreXL & CPU affinity
# ==========================================================================

banner "CoreXL & CPU Affinity"

section "CoreXL Instance Status"
COREXL_OUTPUT=$(fw ctl multik stat 2>&1) || COREXL_OUTPUT=""
echo "$COREXL_OUTPUT"

COREXL_INSTANCES=$(echo "$COREXL_OUTPUT" | grep -c '| Yes' 2>/dev/null) || COREXL_INSTANCES=0

section "Firewall Kernel Affinity"
fw ctl affinity -l 2>&1 || echo "  [unavailable]"

# ==========================================================================
#  Per-VSID detailed diagnostics (each in a subshell)
# ==========================================================================

collect_vs_diag() {
    local vs="$1"
    local vtype="$2"
    local vname="$3"

    echo "Context : VSID $vs - $vname"

    # Enabled blades
    section "Enabled Software Blades"
    if [ "$vtype" = "Virtual Switch" ]; then
        echo "  n/a (Virtual Switch)"
    elif command -v enabled_blades &>/dev/null; then
        enabled_blades 2>/dev/null || echo "  [error]"
    else
        echo "  [enabled_blades not available]"
    fi

    # CPU
    section "Top 5 Processes by CPU"
    ps aux --sort=-%cpu 2>/dev/null | head -6 || echo "  [unavailable]"

    # Memory
    section "Memory Statistics"
    free -m 2>/dev/null || echo "  [unavailable]"

    section "Top 5 Processes by Memory"
    ps aux --sort=-%mem 2>/dev/null | head -6 || echo "  [unavailable]"

    # CPU sample
    section "CPU Performance (1-second sample)"
    if command -v mpstat &>/dev/null; then
        mpstat 1 1 2>&1
    else
        echo "  [mpstat not available]"
        top -bn1 2>/dev/null | grep "^%Cpu" || echo "  [unavailable]"
    fi

    # Routing (skip for switches)
    if [ "$vtype" != "Virtual Switch" ]; then
        section "Routing Table"
        ip route 2>&1

        section "Default Gateway"
        ip route 2>/dev/null | grep '^default' || echo "  [no default route]"
    fi

    # Interfaces
    section "Interface Addresses"
    ip addr 2>&1

    # Interface statistics — also emit error/drop counts for summary
    section "Interface Statistics (errors/drops)"
    local if_stats
    if_stats=$(ip -s link 2>/dev/null) || if_stats=""
    echo "$if_stats"

    # Parse interface errors for attention items
    # Output format: IFACE_ERRORS|vsid|dev|rx_errors|rx_dropped|tx_errors|tx_dropped
    local current_dev=""
    local rx_next=0
    local tx_next=0
    echo "$if_stats" | while IFS= read -r line; do
        case "$line" in
            *": <"*)
                current_dev=$(echo "$line" | sed -n 's/^[0-9]*: \([^:@]*\).*/\1/p')
                rx_next=0; tx_next=0
                ;;
            *"RX: bytes"*)
                rx_next=1
                ;;
            *"TX: bytes"*)
                tx_next=1
                ;;
            *)
                if [ "$rx_next" = "1" ]; then
                    rx_next=0
                    rx_err=$(echo "$line" | awk '{print $3}')
                    rx_drop=$(echo "$line" | awk '{print $4}')
                    if [ "${rx_err:-0}" != "0" ] || [ "${rx_drop:-0}" != "0" ]; then
                        echo "IFACE_ERRORS|${vs}|${current_dev}|rx_err=${rx_err}|rx_drop=${rx_drop}"
                    fi
                elif [ "$tx_next" = "1" ]; then
                    tx_next=0
                    tx_err=$(echo "$line" | awk '{print $3}')
                    tx_drop=$(echo "$line" | awk '{print $4}')
                    if [ "${tx_err:-0}" != "0" ] || [ "${tx_drop:-0}" != "0" ]; then
                        echo "IFACE_ERRORS|${vs}|${current_dev}|tx_err=${tx_err}|tx_drop=${tx_drop}"
                    fi
                fi
                ;;
        esac
    done

    # SecureXL (skip for switches)
    if [ "$vtype" != "Virtual Switch" ]; then
        section "SecureXL Acceleration Status"
        if command -v fwaccel &>/dev/null; then
            fwaccel stat 2>&1
        fi

        section "SecureXL Template Statistics"
        fwaccel stats -s 2>&1 || echo "  [unavailable]"
    fi

    # Connections
    section "Connections Table Summary"
    fw tab -t connections -s 2>&1 || echo "  [unavailable]"

    if [ "$vtype" != "Virtual Switch" ]; then
        section "NAT Table Summary"
        fw tab -t fwx_alloc -s 2>&1 || echo "  [unavailable]"
    fi

    # Virtual Switch specifics
    if [ "$vtype" = "Virtual Switch" ]; then
        section "Bridge Interfaces"
        if command -v brctl &>/dev/null; then
            brctl show 2>&1 || echo "  [unavailable]"
        else
            bridge link 2>&1 || echo "  [unavailable]"
        fi

        section "WARP Interfaces on Bridge"
        ip link show 2>/dev/null | grep -i wrp || echo "  [none found]"
    fi

    # --- Build summary line ---
    local blades="n/a"
    if [ "$vtype" = "Virtual Switch" ]; then
        blades="n/a (vsw)"
    elif command -v enabled_blades &>/dev/null; then
        blades=$(enabled_blades 2>/dev/null) || blades="n/a"
    fi

    local top_cpu
    top_cpu=$(ps aux --sort=-%cpu 2>/dev/null | awk 'NR==2 {print $3"% ("$11")"}') || top_cpu="n/a"

    local mem_pct
    mem_pct=$(free -m 2>/dev/null | awk '/^Mem:/ {printf "%.0f%%", ($3/$2)*100}') || mem_pct="n/a"

    local mem_used_mb
    mem_used_mb=$(free -m 2>/dev/null | awk '/^Mem:/ {print $3}') || mem_used_mb="0"

    local mem_total_mb
    mem_total_mb=$(free -m 2>/dev/null | awk '/^Mem:/ {print $2}') || mem_total_mb="0"

    local swap_used
    swap_used=$(free -m 2>/dev/null | awk '/^Swap:/ {print $3}') || swap_used="0"

    local conn_now
    conn_now=$(fw tab -t connections -s 2>/dev/null | awk 'NR>1 {sum+=$4} END {print sum+0}') || conn_now="0"

    local saccel="n/a"
    if [ "$vtype" != "Virtual Switch" ] && command -v fwaccel &>/dev/null; then
        saccel=$(fwaccel stat 2>/dev/null | awk '/^Accelerator Status/ {print $NF}')
        if [ -z "$saccel" ]; then
            saccel=$(fwaccel stat 2>/dev/null | awk -F'|' '/KPPAK/ {gsub(/[ \t]/, "", $4); print $4}')
        fi
        saccel="${saccel:-n/a}"
    fi

    local idle_pct
    idle_pct=$(mpstat 1 1 2>/dev/null | awk '/^Average:/ {print $NF}') || idle_pct=""

    echo "SUMMARY_DATA|${vs}|${top_cpu:-n/a}|${mem_pct:-n/a}|${conn_now:-0}|${saccel:-n/a}|${blades:-n/a}|${mem_used_mb:-0}|${mem_total_mb:-0}|${swap_used:-0}|${idle_pct:-n/a}"
}

TMPSUMMARY=$(mktemp /tmp/vsx_summary.XXXXXX)
TMPERRORS=$(mktemp /tmp/vsx_errors.XXXXXX)

for vs in $ALL_VSIDS; do
    eval "vtype=\${VS_${vs}_TYPE:-unknown}"
    eval "vname=\${VS_${vs}_NAME:-VSID-$vs}"

    banner "VSID $vs - $vname ($vtype)"

    run_in_vs "$vs" collect_vs_diag "$vs" "$vtype" "$vname" | while IFS= read -r line; do
        case "$line" in
            SUMMARY_DATA\|*)
                echo "$line" >> "$TMPSUMMARY"
                ;;
            IFACE_ERRORS\|*)
                echo "$line" >> "$TMPERRORS"
                ;;
            *)
                echo "$line"
                ;;
        esac
    done

    # NCS config (from cache)
    if [ "$vs" -ne 0 ]; then
        section "NCS Configuration (vsx showncs $vs)"
        NCS_FILE="${TMPNCS_DIR}/${vs}.ncs"
        if [ -s "$NCS_FILE" ]; then
            cat "$NCS_FILE"
        else
            echo "  [unavailable]"
        fi
    fi

    echo ""
    echo "-- End VSID $vs --"
done

# ==========================================================================
#  Cluster health
# ==========================================================================

banner "Cluster Health"

CLUSTER_STATE=""
CLUSTER_MODE=""
SYNC_STATUS=""

if cphaprob stat &>/dev/null 2>&1; then
    section "Cluster Member State"
    CPHAPROB_OUTPUT=$(cphaprob stat 2>&1) || CPHAPROB_OUTPUT=""
    echo "$CPHAPROB_OUTPUT"

    # Parse cluster mode and member states for summary
    CLUSTER_MODE=$(echo "$CPHAPROB_OUTPUT" | sed -n 's/^Cluster Mode:[[:space:]]*\(.*\)/\1/p')

    # Build member state list
    CLUSTER_MEMBER_STATES=""
    echo "$CPHAPROB_OUTPUT" | grep -E '^\s*[0-9]' | while IFS= read -r line; do
        mname=$(echo "$line" | awk '{print $NF}')
        mstate=$(echo "$line" | awk '{for(i=1;i<=NF;i++) if($i ~ /ACTIVE|STANDBY|BACKUP|DOWN/) print $i}')
        echo "MEMBER_STATE|${mname}|${mstate}" >> "$TMPSUMMARY"
    done

    section "Cluster Interfaces"
    cphaprob -a if 2>&1

    section "Cluster Synchronisation"
    SYNC_OUTPUT=$(cphaprob syncstat 2>&1) || SYNC_OUTPUT=""
    echo "$SYNC_OUTPUT"
    SYNC_STATUS=$(echo "$SYNC_OUTPUT" | sed -n 's/^Sync status:[[:space:]]*\(.*\)/\1/p')
    SYNC_LOST=$(echo "$SYNC_OUTPUT" | sed -n 's/.*Lost updates\.*[[:space:]]\([0-9]*\)/\1/p')
    SYNC_LOST="${SYNC_LOST:-0}"

    section "Cluster HA Statistics"
    CPSTAT_HA=$(cpstat ha -f all 2>&1) || CPSTAT_HA=""
    echo "$CPSTAT_HA"

    # Check for any non-OK PNOTEs
    PNOTE_ISSUES=$(echo "$CPSTAT_HA" | awk -F'|' '/\|/ && !/Name/ && !/---/ && !/^$/ {
        gsub(/[ \t]/, "", $3);
        if ($3 != "" && $3 != "OK" && $3 != "Status") print $2 ":" $3
    }')
else
    echo "  [ClusterXL not active or not a cluster member - skipped]"
fi

# ==========================================================================
#  Platform
# ==========================================================================

banner "Platform Information"
run_cmd "Check Point version" fw ver
run_cmd "Hotfix / JHF status" cpinfo -y all
run_cmd "Kernel build" uname -r
run_cmd "System uptime" uptime
run_cmd "Disk usage" df -h

# Capture disk usage for summary
DISK_ROOT_PCT=$(df -h / 2>/dev/null | awk 'NR==2 {print $5}') || DISK_ROOT_PCT="?"
DISK_LOG_PCT=$(df -h /var/log 2>/dev/null | awk 'NR==2 {print $5}') || DISK_LOG_PCT="?"

section "License Summary"
cplic print 2>&1 | head -20 || echo "  [unavailable]"

# ==========================================================================
#  Per-VSID status table
# ==========================================================================

banner "Per-VSID Status Table"

printf "  %-4s %-16s %-5s %-10s %-6s %-12s %-38s %s\n" \
    "VS" "Name" "Type" "SecureXL" "Mem%" "Conns/Limit" "Blades" "Top CPU"
printf "  %-4s %-16s %-5s %-10s %-6s %-12s %-38s %s\n" \
    "----" "----------------" "-----" "----------" "------" \
    "------------" "--------------------------------------" "----------"

for vs in $ALL_VSIDS; do
    eval "vtype=\${VS_${vs}_TYPE:-?}"
    eval "vname=\${VS_${vs}_NAME:-n/a}"
    eval "vlimit=\${VS_${vs}_LIMIT:-?}"

    short_type="$vtype"
    case "$short_type" in
        "VSX Gateway")    short_type="GW" ;;
        "Virtual System") short_type="VS" ;;
        "Virtual Switch") short_type="VSW" ;;
        "Virtual Router") short_type="VR" ;;
    esac

    sum_line=$(grep "^SUMMARY_DATA|${vs}|" "$TMPSUMMARY" 2>/dev/null | tail -1)
    if [ -n "$sum_line" ]; then
        scpu=$(echo "$sum_line" | cut -d'|' -f3)
        smem=$(echo "$sum_line" | cut -d'|' -f4)
        sconn=$(echo "$sum_line" | cut -d'|' -f5)
        ssaccel=$(echo "$sum_line" | cut -d'|' -f6)
        sblades=$(echo "$sum_line" | cut -d'|' -f7)
    else
        scpu="n/a"; smem="n/a"; sconn="0"; ssaccel="n/a"; sblades="n/a"
    fi

    printf "  %-4s %-16s %-5s %-10s %-6s %-12s %-38s %s\n" \
        "$vs" "$vname" "$short_type" "$ssaccel" "$smem" \
        "${sconn}/${vlimit}" "$sblades" "$scpu"
done

# ==========================================================================
#  Executive Summary
# ==========================================================================

banner "Executive Summary"

# --- Environment ---
echo ""
echo "ENVIRONMENT"
VERSION_SHORT=$(echo "$CP_VERSION" | sed -n 's/.*version \(R[0-9.]*\).*/\1/p')
BUILD_SHORT=$(echo "$CP_VERSION" | sed -n 's/.*Build \([0-9]*\).*/\1/p')
echo "  ${VERSION_SHORT:-Check Point} Build ${BUILD_SHORT:-?} + JHF Take ${JHF_TAKE:-?}"
echo "  ${MEMBER_COUNT}-member VSX cluster (${CLUSTER_MODE:-unknown mode})"
echo "  Licensed for ${VS_LICENSE_COUNT:-?} Virtual Systems, ${VS_GW_COUNT} configured"
echo "  Management: ${MGMT_ADDR:-?}  Cluster VIP: ${CLUSTER_VIP:-?}"
echo ""

# --- Cluster members with state ---
echo "CLUSTER MEMBERS"
for member in $CLUSTER_MEMBERS; do
    eval "m_mgmt=\${MEMBER_${member}_MGMT:-?}"
    m_state=$(grep "^MEMBER_STATE|${member}|" "$TMPSUMMARY" 2>/dev/null \
        | sed -n 's/.*|\([A-Z]*\)$/\1/p' | head -1)
    m_state="${m_state:-?}"
    marker=""
    [ "$member" = "$THIS_HOST" ] && marker=" (this gateway)"
    echo "  $member ($m_mgmt) - ${m_state}${marker}"
done
echo ""

# --- Virtual devices topology narrative ---
echo "VIRTUAL DEVICES"
for vs in $ALL_VSIDS; do
    eval "vtype=\${VS_${vs}_TYPE:-?}"
    eval "vname=\${VS_${vs}_NAME:-?}"
    eval "vpolicy=\${VS_${vs}_POLICY:-?}"

    NCS_FILE="${TMPNCS_DIR}/${vs}.ncs"

    case "$vtype" in
        "VSX Gateway")
            echo "  VS0  $vname (VSX Gateway)"
            echo "        eth0 -> Management (10.1.1.0/24)"
            echo "        eth2 -> Cluster Sync (192.168.10.0/24)"
            echo "        bond0 -> Trunk (VLAN carrier for VS interfaces)"
            ;;
        "Virtual Switch")
            echo "  VS${vs}  $vname (Virtual Switch)"
            echo "        br1[eth3] -> Physical network uplink"
            # List WARP junctions
            for gw_vs in $VS_GW_IDS; do
                GW_NCS="${TMPNCS_DIR}/${gw_vs}.ncs"
                if [ -s "$GW_NCS" ]; then
                    wrpj=$(grep 'warp create' "$GW_NCS" \
                        | sed -n 's/.*name_b \([^ ]*\).*/\1/p' | head -1)
                    eval "gwname=\${VS_${gw_vs}_NAME:-VS$gw_vs}"
                    [ -n "$wrpj" ] && echo "        br1[$wrpj] -> Junction from $gwname"
                fi
            done
            ;;
        "Virtual System")
            echo "  VS${vs}  $vname (Firewall) - Policy: $vpolicy"
            # List external interfaces from NCS
            if [ -s "$NCS_FILE" ]; then
                grep 'interface set dev' "$NCS_FILE" | while IFS= read -r line; do
                    dev=$(echo "$line" | sed -n 's/.*dev \([^ ]*\).*/\1/p')
                    cip=$(echo "$line" | sed -n 's/.*cluster_ip \([0-9][0-9.]*\).*/\1/p')
                    cmask=$(echo "$line" | sed -n 's/.*cluster_mask \([0-9][0-9.]*\).*/\1/p')
                    [ -z "$dev" ] && continue
                    [ -z "$cip" ] && continue
                    [ "$cip" = "0.0.0.0" ] && continue
                    # Determine network name from subnet
                    echo "        $dev -> $cip/$cmask"
                done

                # WARP connection
                wrp=$(grep 'warp create' "$NCS_FILE" | sed -n 's/.*name_a \([^ ]*\).*/\1/p' | head -1)
                wrpj=$(grep 'warp create' "$NCS_FILE" | sed -n 's/.*name_b \([^ ]*\).*/\1/p' | head -1)
                wrp_cip=""
                if [ -n "$wrp" ]; then
                    wrp_cip=$(grep "interface set dev ${wrp} " "$NCS_FILE" \
                        | sed -n 's/.*cluster_ip \([0-9][0-9.]*\).*/\1/p' | head -1)
                fi
                if [ -n "$wrp" ]; then
                    # Find which VS the junction connects to
                    for sw_vs in $VS_SW_IDS; do
                        eval "swname=\${VS_${sw_vs}_NAME:-VSW}"
                        echo "        $wrp ($wrp_cip) --WARP[$wrpj]--> $swname"
                    done
                fi
            fi
            ;;
        "Virtual Router")
            echo "  VS${vs}  $vname (Virtual Router) - Policy: $vpolicy"
            ;;
    esac
done
echo ""

# --- Traffic flow ---
echo "TRAFFIC FLOW"

# Build flow dynamically
FLOW_PARTS=""
for vs in $VS_GW_IDS; do
    eval "vname=\${VS_${vs}_NAME:-VS$vs}"
    NCS_FILE="${TMPNCS_DIR}/${vs}.ncs"
    wrp=""
    wrpj=""
    if [ -s "$NCS_FILE" ]; then
        wrp=$(grep 'warp create' "$NCS_FILE" | sed -n 's/.*name_a \([^ ]*\).*/\1/p' | head -1)
        wrpj=$(grep 'warp create' "$NCS_FILE" | sed -n 's/.*name_b \([^ ]*\).*/\1/p' | head -1)
    fi
    if [ -n "$FLOW_PARTS" ]; then
        FLOW_PARTS="${FLOW_PARTS}  <--VSW-->  "
    fi
    FLOW_PARTS="${FLOW_PARTS}${vname}[${wrp:-?}/${wrpj:-?}]"
done

if [ -n "$FLOW_PARTS" ]; then
    for sw_vs in $VS_SW_IDS; do
        eval "swname=\${VS_${sw_vs}_NAME:-VSW}"
    done
    echo "  $FLOW_PARTS"
    echo ""
    echo "  Both firewalls connect to ${swname:-VSW} via WARP interface pairs."
    echo "  ${swname:-VSW} bridges WARP junctions and eth3 to the physical network."
    echo "  Inter-VS traffic transits the virtual switch at layer 2."
fi
echo ""

# --- Health assessment ---
echo "HEALTH"

# Overall status tracking
HEALTH_OK=true
ATTENTION_ITEMS=""

add_attention() {
    ATTENTION_ITEMS="${ATTENTION_ITEMS}  - $1"$'\n'
    HEALTH_OK=false
}

# Cluster sync
if [ -n "$SYNC_STATUS" ]; then
    echo "  Cluster sync: $SYNC_STATUS (interface: eth2, delta sync)"
    if [ "$SYNC_STATUS" != "OK" ]; then
        add_attention "Cluster sync status: $SYNC_STATUS"
    fi
    if [ "${SYNC_LOST:-0}" != "0" ]; then
        add_attention "Lost sync updates: $SYNC_LOST"
    fi
else
    echo "  Cluster sync: [not available]"
fi

# PNOTEs
if [ -n "${PNOTE_ISSUES:-}" ]; then
    echo "  PNOTEs: ISSUES DETECTED"
    add_attention "PNOTE issues: $PNOTE_ISSUES"
else
    echo "  PNOTEs: All OK"
fi

# SecureXL per VS
SXLALL="OK"
for vs in $ALL_VSIDS; do
    eval "vtype=\${VS_${vs}_TYPE:-?}"
    [ "$vtype" = "Virtual Switch" ] && continue
    sum_line=$(grep "^SUMMARY_DATA|${vs}|" "$TMPSUMMARY" 2>/dev/null | tail -1)
    ssaccel=$(echo "$sum_line" | cut -d'|' -f6)
    if [ -n "$ssaccel" ] && [ "$ssaccel" != "enabled" ] && [ "$ssaccel" != "n/a" ]; then
        SXLALL="ISSUE"
        eval "vname=\${VS_${vs}_NAME:-VS$vs}"
        add_attention "SecureXL not enabled on VSID $vs ($vname): $ssaccel"
    fi
done
echo "  SecureXL: $SXLALL (across all firewall VSIDs)"

# CPU
sum_line_0=$(grep "^SUMMARY_DATA|0|" "$TMPSUMMARY" 2>/dev/null | tail -1)
idle_pct=$(echo "$sum_line_0" | cut -d'|' -f11)
echo "  CPU: ${idle_pct:-?}% idle, load avg ${LOAD_AVG:-?}, ${COREXL_INSTANCES} CoreXL instances"
# Flag if idle < 50%
if [ -n "$idle_pct" ] && echo "$idle_pct" | grep -q '^[0-9]'; then
    idle_int=$(echo "$idle_pct" | cut -d'.' -f1)
    if [ "${idle_int:-100}" -lt 50 ]; then
        add_attention "CPU idle below 50%: ${idle_pct}%"
    fi
fi

# Memory
mem_used=$(echo "$sum_line_0" | cut -d'|' -f8)
mem_total=$(echo "$sum_line_0" | cut -d'|' -f9)
swap_used=$(echo "$sum_line_0" | cut -d'|' -f10)
mem_pct=$(echo "$sum_line_0" | cut -d'|' -f4)
echo "  Memory: ${mem_pct:-?} used (${mem_used:-?}/${mem_total:-?} MB), swap: ${swap_used:-0} MB used"
if [ -n "$swap_used" ] && [ "${swap_used:-0}" != "0" ]; then
    add_attention "Swap in use: ${swap_used} MB"
fi

# Connections
echo "  Connections: ${TOTAL_CONN:-?}/${TOTAL_CONN_LIMIT:-?} total"
if [ -n "$TOTAL_CONN" ] && [ -n "$TOTAL_CONN_LIMIT" ] && [ "${TOTAL_CONN_LIMIT:-0}" -gt 0 ] 2>/dev/null; then
    conn_pct=$(( (TOTAL_CONN * 100) / TOTAL_CONN_LIMIT ))
    if [ "$conn_pct" -ge 80 ]; then
        add_attention "Total connection usage at ${conn_pct}% of cluster limit!"
    fi
fi

# Per-VS connection check
for vs in $ALL_VSIDS; do
    eval "vlimit=\${VS_${vs}_LIMIT:-0}"
    sum_line=$(grep "^SUMMARY_DATA|${vs}|" "$TMPSUMMARY" 2>/dev/null | tail -1)
    sconn=$(echo "$sum_line" | cut -d'|' -f5)
    sconn="${sconn:-0}"
    if echo "$sconn" | grep -q '^[0-9]*$' && echo "$vlimit" | grep -q '^[0-9]*$'; then
        if [ "${vlimit:-0}" -gt 0 ] 2>/dev/null; then
            pct=$(( (sconn * 100) / vlimit ))
            if [ "$pct" -ge 80 ]; then
                eval "vname=\${VS_${vs}_NAME:-?}"
                add_attention "VSID $vs ($vname) at ${pct}% connection capacity (${sconn}/${vlimit})"
            fi
        fi
    fi
done

# Disk
echo "  Disk: root ${DISK_ROOT_PCT:-?}, /var/log ${DISK_LOG_PCT:-?}"
DISK_ROOT_INT=$(echo "$DISK_ROOT_PCT" | tr -d '%')
DISK_LOG_INT=$(echo "$DISK_LOG_PCT" | tr -d '%')
if echo "$DISK_ROOT_INT" | grep -q '^[0-9]' && [ "${DISK_ROOT_INT:-0}" -ge 80 ] 2>/dev/null; then
    add_attention "Root filesystem at ${DISK_ROOT_PCT}"
fi
if echo "$DISK_LOG_INT" | grep -q '^[0-9]' && [ "${DISK_LOG_INT:-0}" -ge 80 ] 2>/dev/null; then
    add_attention "Log filesystem at ${DISK_LOG_PCT}"
fi

# Interface errors
if [ -s "$TMPERRORS" ]; then
    while IFS='|' read -r tag vs dev detail1 detail2; do
        eval "vname=\${VS_${vs}_NAME:-VS$vs}"
        add_attention "VSID $vs ($vname) $dev: $detail1 $detail2"
    done < "$TMPERRORS"
fi

echo ""
# --- Attention items ---
if [ "$HEALTH_OK" = "true" ]; then
    echo "ATTENTION"
    echo "  No issues detected."
else
    echo "ATTENTION"
    echo -n "$ATTENTION_ITEMS"
fi

# Cleanup
rm -f "$TMPSUMMARY" "$TMPERRORS"
rm -rf "$TMPNCS_DIR"

echo ""
echo "============================================================"
echo "  Diagnostics complete - $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  Log saved to: $LOGFILE"
echo "============================================================"
