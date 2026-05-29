#!/usr/bin/env bash
# mailarch.sh — PST Search MCP server launcher
#
# Usage: mailarch.sh [--status|--start|--stop|--restart|--extract|--reindex|--config]
#
# First time:
#   mailarch.sh --extract     # extract PST → .eml (takes a while for large archives)
#   mailarch.sh --start       # start MCP server + index emails
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Default configuration (override via config.yaml or env) ---
: "${PST_DIR:=$APP_DIR/pst}"
: "${PST_EML_DIR:=$APP_DIR/eml}"
: "${PST_DATA_DIR:=$APP_DIR/data}"
: "${PST_PORT:=8766}"
: "${PST_MODEL:=BAAI/bge-small-en-v1.5}"
: "${PST_LOG_LEVEL:=WARNING}"
: "${PST_LOG_FILE:=$APP_DIR/server.log}"
: "${PST_PID_FILE:=$APP_DIR/server.pid}"
: "${PST_IDLE_TIMEOUT:=0}"

# Load config.yaml if present
CONFIG_FILE="$APP_DIR/config.yaml"
if [[ -f "$CONFIG_FILE" ]]; then
    _load_yaml() {
        local key="$1" default="$2"
        local val
        val=$(grep -m1 "^${key}:" "$CONFIG_FILE" 2>/dev/null | sed 's/^[^:]*:[[:space:]]*//' | tr -d '"' || true)
        echo "${val:-$default}"
    }
    _expand() { echo "${1/#\~/$HOME}"; }
    PST_DIR=$(_expand "$(_load_yaml pst_dir "$PST_DIR")")
    PST_EML_DIR=$(_expand "$(_load_yaml eml_dir "$PST_EML_DIR")")
    PST_DATA_DIR=$(_expand "$(_load_yaml data_dir "$PST_DATA_DIR")")
    PST_PORT=$(_load_yaml port "$PST_PORT")
    PST_MODEL=$(_load_yaml model "$PST_MODEL")
    PST_LOG_LEVEL=$(_load_yaml log_level "$PST_LOG_LEVEL")
    PST_IDLE_TIMEOUT=$(_load_yaml idle_timeout "$PST_IDLE_TIMEOUT")
fi

export PST_DIR PST_EML_DIR PST_DATA_DIR PST_PORT PST_MODEL
export PST_LOG_LEVEL PST_LOG_FILE PST_PID_FILE PST_IDLE_TIMEOUT
export PST_APP_DIR="$APP_DIR"

# --- PID helpers ---
_get_pid() {
    [[ -f "$PST_PID_FILE" ]] || return 1
    local pid
    pid=$(cat "$PST_PID_FILE")
    kill -0 "$pid" 2>/dev/null && echo "$pid" || return 1
}

_rotate_log() {
    local log="$PST_LOG_FILE"
    [[ -f "$log" ]] || return 0
    local size
    size=$(stat -c%s "$log" 2>/dev/null || echo 0)
    if [[ "$size" -gt $((20 * 1024 * 1024)) ]]; then
        mv "$log" "${log}.1" 2>/dev/null || true
    fi
}

# --- Commands ---

cmd_status() {
    local pid
    if pid=$(_get_pid 2>/dev/null); then
        local health
        health=$(curl -sf "http://127.0.0.1:${PST_PORT}/health" 2>/dev/null || echo "{}")
        local emails
        emails=$(echo "$health" | grep -oP '"emails":\s*\K[0-9]+' || echo "?")
        local indexing
        indexing=$(echo "$health" | grep -oP '"indexing":\s*\K(true|false)' || echo "?")
        echo "running: PID=$pid, emails=$emails, indexing=$indexing"
    else
        echo "not running"
    fi
}

cmd_extract() {
    echo "Extracting PST files from $PST_DIR → $PST_EML_DIR"
    echo "(This may take several minutes for large archives)"
    mkdir -p "$PST_EML_DIR"
    local found=0
    for pst in "$PST_DIR"/*.pst "$PST_DIR"/*.PST; do
        [[ -f "$pst" ]] || continue
        found=1
        local name
        name=$(basename "$pst" .pst)
        name=$(basename "$name" .PST)
        local _chk_eml
        _chk_eml=$(find "$PST_EML_DIR/$name" -name "*.eml" -maxdepth 4 -print -quit 2>/dev/null || true)
        if [[ -d "$PST_EML_DIR/$name" ]] && [[ -n "$_chk_eml" ]]; then
            local count
            count=$(find "$PST_EML_DIR/$name" -name "*.eml" | wc -l)
            echo "  $pst — already extracted ($count emails). Use --reextract to force."
            continue
        fi
        echo "  Extracting $pst ..."
        mkdir -p "$PST_EML_DIR/$name"
        readpst -e -D -o "$PST_EML_DIR/$name" "$pst"
        local ecount
        ecount=$(find "$PST_EML_DIR/$name" -name "*.eml" | wc -l)
        echo "  Done: $ecount emails extracted → $PST_EML_DIR/$name"
    done
    [[ "$found" -eq 1 ]] || echo "No .pst files found in $PST_DIR"
}

cmd_reextract() {
    echo "Force re-extracting PST files (overwriting existing .eml files)"
    rm -rf "$PST_EML_DIR"
    cmd_extract
}

_wait_for_ready() {
    local health="http://127.0.0.1:${PST_PORT}/health"
    local t_start elapsed resp last_indexing=""
    t_start=$(date +%s)
    while true; do
        elapsed=$(( $(date +%s) - t_start ))
        [[ "$elapsed" -lt 600 ]] || { echo "ERROR: timeout after 600s" >&2; return 1; }
        resp=$(curl -sf "$health" 2>/dev/null) || { sleep 2; continue; }
        local indexing
        indexing=$(echo "$resp" | grep -oP '"indexing":\s*\K(true|false)' || echo "true")
        if [[ "$indexing" == "false" ]]; then
            local emails
            emails=$(echo "$resp" | grep -oP '"emails":\s*\K[0-9]+' || echo "?")
            echo "ready: $emails emails indexed"
            return 0
        fi
        if [[ "$indexing" != "$last_indexing" ]]; then
            local chunks
            chunks=$(echo "$resp" | grep -oP '"chunks":\s*\K[0-9]+' || echo "?")
            echo "  indexing… ($chunks chunks so far)"
            last_indexing="$indexing"
        fi
        sleep 5
    done
}

cmd_start() {
    local pid
    if pid=$(_get_pid 2>/dev/null); then
        echo "already running: PID=$pid"
        exit 0
    fi

    local _first_eml
    _first_eml=$(find "$PST_EML_DIR" -name "*.eml" -maxdepth 5 -print -quit 2>/dev/null || true)
    if [[ ! -d "$PST_EML_DIR" ]] || [[ -z "$_first_eml" ]]; then
        echo "ERROR: No .eml files found in $PST_EML_DIR"
        echo "Run: mailarch.sh --extract"
        exit 1
    fi

    mkdir -p "$PST_DATA_DIR" "$(dirname "$PST_LOG_FILE")"
    _rotate_log

    PYTHONPATH="$APP_DIR/src" \
    setsid "$APP_DIR/.venv/bin/python" -m pst_search.server \
        >>"$PST_LOG_FILE" 2>&1 &

    echo "server process launched — log: $PST_LOG_FILE"

    # Wait for HTTP server to come up
    local health="http://127.0.0.1:${PST_PORT}/health"
    for i in $(seq 1 30); do
        sleep 1
        if curl -sf "$health" >/dev/null 2>&1; then
            local real_pid
            real_pid=$(cat "$PST_PID_FILE" 2>/dev/null || echo "?")
            echo "started: PID=$real_pid"
            _wait_for_ready
            exit 0
        fi
    done
    echo "ERROR: server did not start in 30s — check $PST_LOG_FILE" >&2
    exit 1
}

cmd_stop() {
    local pid
    if ! pid=$(_get_pid 2>/dev/null); then
        echo "not running"
        exit 0
    fi
    kill "$pid" 2>/dev/null || true
    rm -f "$PST_PID_FILE"
    for i in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    echo "stopped: PID=$pid"
}

cmd_restart() {
    cmd_stop || true
    sleep 1
    cmd_start
}

cmd_reindex() {
    cmd_stop || true
    echo "Clearing index ($PST_DATA_DIR)..."
    rm -f "$PST_DATA_DIR/vectors.npy" \
          "$PST_DATA_DIR/row_ids.npy" \
          "$PST_DATA_DIR/metadata.db"
    cmd_start
}

cmd_config() {
    echo "PST Search configuration:"
    printf "  %-20s = %s\n" \
        "PST_DIR"         "$PST_DIR" \
        "PST_EML_DIR"     "$PST_EML_DIR" \
        "PST_DATA_DIR"    "$PST_DATA_DIR" \
        "PST_PORT"        "$PST_PORT" \
        "PST_MODEL"       "$PST_MODEL" \
        "PST_LOG_LEVEL"   "$PST_LOG_LEVEL" \
        "PST_LOG_FILE"    "$PST_LOG_FILE" \
        "PST_IDLE_TIMEOUT" "$PST_IDLE_TIMEOUT"
}

# --- Dispatch ---
case "${1:---status}" in
    --status)     cmd_status ;;
    --start)      cmd_start ;;
    --stop)       cmd_stop ;;
    --restart)    cmd_restart ;;
    --extract)    cmd_extract ;;
    --reextract)  cmd_reextract ;;
    --reindex)    cmd_reindex ;;
    --config)     cmd_config ;;
    *) echo "Usage: mailarch.sh [--status|--start|--stop|--restart|--extract|--reextract|--reindex|--config]" >&2; exit 1 ;;
esac
