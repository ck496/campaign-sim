#!/usr/bin/env bash

# Run a local game: $1 = number of human seats ...default 0 = all bots

set -euo pipefail

HUMANS="${1:-0}"
SOCK="./run/game.sock"
CONFIG="game.json"
NAMES=(ada bo cy dee)
SERVER_PID=""
BOT_PIDS=()

cleanup() {
    for pid in "${BOT_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
    fi
    rm -f "$SOCK"
}
trap cleanup EXIT

mkdir -p ./run

# start server

python3 server.py --config "$CONFIG" --sock "$SOCK" --auth name &
SERVER_PID=$!

# wait for socket to appear
for i in $(seq 1 30); do
    [ -S "$SOCK" ] && break
    sleep 0.1
done
if [ ! -S "$SOCK" ]; then
    echo "server socket did not appear" >&2
    exit 1
fi

# start bot clients for non-human seats
BOT_START=$((HUMANS + 1))
TOTAL=${#NAMES[@]}
for (( i=BOT_START; i<=TOTAL; i++ )); do
    idx=$((i - 1))
    python3 client.py --name "${NAMES[$idx]}" --bot --sock "$SOCK" --config "$CONFIG" &
    BOT_PIDS+=($!)
done

if [ "$HUMANS" -eq 0 ]; then
    # all bots: wait for bot clients to finish, then stop server
    for pid in "${BOT_PIDS[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
else
    echo ""
    echo "Server running. Bots connected."
    echo ""
    echo "Open a new terminal for each human player and run:"
    echo ""
    for (( i=1; i<=HUMANS; i++ )); do
        idx=$((i - 1))
        NAME="${NAMES[$idx]}"
        echo "  python3 client.py --name $NAME --sock $SOCK --config $CONFIG"
    done
    echo ""
    echo "Press Ctrl-C here to stop the server when done."
    wait "$SERVER_PID" 2>/dev/null || true
fi
