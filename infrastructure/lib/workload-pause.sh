# Shell interface for the same pause declaration used by the memory checker.
# Source from the checkout or from alongside installed watchdog scripts.

_workload_pause_dir=$(dirname "${BASH_SOURCE[0]}")
if [[ "${_workload_pause_dir##*/}" == lib ]]; then
    _workload_pause_dir=$(dirname "$_workload_pause_dir")
fi

declare -A WORKLOAD_PAUSE_PORTS=()

workload_pause_records() {
    python3 "$_workload_pause_dir/workload_pauses.py" "$@"
}

load_workload_pauses() {
    local payload unit port _provenance _reason
    WORKLOAD_PAUSE_PORTS=()
    payload=$(workload_pause_records "$@") || return 1
    while IFS=$'\t' read -r unit port _provenance _reason; do
        [[ -n "$unit" ]] || continue
        WORKLOAD_PAUSE_PORTS["$unit"]=$port
    done <<< "$payload"
}

workload_is_paused() {
    [[ -n "${WORKLOAD_PAUSE_PORTS[$1]:-}" ]]
}

workload_paused_port() {
    printf '%s\n' "${WORKLOAD_PAUSE_PORTS[$1]:-}"
}

workload_pause_has_listener() {
    # ss -tln puts the local address in column four for TCP.
    awk -v port="$1" '$4 ~ (":" port "$") { found=1 } END { exit !found }' <<< "$2"
}

workload_pause_problems() {
    [ "$#" -ge 1 ] && [ "$#" -le 2 ] || return 2
    local unit=$1 state active load enabled line port listening
    workload_is_paused "$unit" || return 2
    if state=$(systemctl show "$unit" -p ActiveState -p LoadState -p UnitFileState 2>/dev/null); then
        active=""; load=""; enabled=""
        while IFS= read -r line; do
            case "$line" in
                ActiveState=*) active=${line#ActiveState=} ;;
                LoadState=*) load=${line#LoadState=} ;;
                UnitFileState=*) enabled=${line#UnitFileState=} ;;
            esac
        done <<< "$state"
        case "$load:$active:$enabled:$unit" in
            loaded:inactive:disabled:*|masked:inactive:masked:*|loaded:inactive:masked:*|\
            loaded:inactive:static:*.service|loaded:inactive:indirect:*.service) ;;
            *) echo "paused workload unit drift: $unit (load=${load:-unknown}, active=${active:-unknown}, enablement=${enabled:-unknown})" ;;
        esac
    else
        echo "paused workload unit state unreadable: $unit"
    fi

    port=$(workload_paused_port "$unit")
    if [ "$port" != "-" ]; then
        if [ "$#" -ge 2 ]; then
            if workload_pause_has_listener "$port" "$2"; then
                echo "paused workload port listening: $unit ($port)"
            fi
        elif listening=$(ss -H -ltn "sport = :$port" 2>/dev/null); then
            if [ -n "$listening" ]; then
                echo "paused workload port listening: $unit ($port)"
            fi
        else
            echo "paused workload port state unreadable: $unit ($port)"
        fi
    fi
    return 0
}
