#!/bin/sh
#
# Dev runserver wrapper that restarts the Django process cleanly on file changes
# and periodically (every MAX_UPTIME seconds) to prevent memory leaks.
#
# Unlike `manage.py runserver` which forks a child (leaking memory into swap
# over time due to copy-on-write pages), this script runs with --noreload and
# uses a simple polling loop to detect .py file changes, then kills and restarts
# the entire process. This ensures full memory deallocation on each restart.
#
# Usage: RUN_CMD='sh common/dev_runserver.sh' in .env

WATCH_DIR="${1:-.}"
POLL_INTERVAL="${2:-2}"
MAX_UPTIME="${3:-21600}"  # restart every 6 hours to prevent memory bloat
SERVER_PID=""
STARTED_AT=""

cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
    exit 0
}

trap cleanup INT TERM

get_checksum() {
    find "$WATCH_DIR" -name '*.py' -newer "$STAMP_FILE" -print 2>/dev/null | head -1
}

start_server() {
    python manage.py runserver 0.0.0.0:8000 --noreload &
    SERVER_PID=$!
    STARTED_AT=$(date +%s)
    echo "[dev_runserver] Started server PID=$SERVER_PID"
}

restart_server() {
    echo "[dev_runserver] Restarting... ($1)"
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
    start_server
    touch "$STAMP_FILE"
}

# Temp file to track last restart time
STAMP_FILE=$(mktemp)
touch "$STAMP_FILE"

start_server

while true; do
    sleep "$POLL_INTERVAL"

    # Check if server is still alive
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        restart_server "crashed"
        continue
    fi

    # Check for .py file changes since last restart
    changed=$(get_checksum)
    if [ -n "$changed" ]; then
        restart_server "file change"
        continue
    fi

    # Periodic restart to prevent memory leaks
    NOW=$(date +%s)
    UPTIME=$((NOW - STARTED_AT))
    if [ "$UPTIME" -ge "$MAX_UPTIME" ]; then
        restart_server "max uptime ${MAX_UPTIME}s reached"
    fi
done
