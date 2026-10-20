#!/usr/bin/env bash

# run_game.sh -- start a peercred game in tmux.
# Must be run as the moderator user.
# $1 = number of human seats (default 0 = all bots).

set -euo pipefail

WHOAMI="$(whoami)"
if [ "$WHOAMI" != "moderator" ]; then
    echo "error: must run as moderator (you are $WHOAMI)" >&2
    exit 1
fi

HUMANS="${1:-0}"
GAME_DIR="/opt/campaign"
SOCK="/run/campaign/game.sock"
CONFIG="$GAME_DIR/game.json"
NAMES=(cand1 cand2 cand3 cand4)
SESSION="campaign"

# clean up stale socket
rm -f "$SOCK"

# kill any existing tmux session with this name
tmux kill-session -t "$SESSION" 2>/dev/null || true

# start tmux session with the server in the first pane
tmux new-session -d -s "$SESSION" -n game \
    "python3 $GAME_DIR/server.py --auth peercred --config $CONFIG --sock $SOCK; read"

# wait for socket to appear
for i in $(seq 1 30); do
    [ -S "$SOCK" ] && break
    sleep 0.2
done
if [ ! -S "$SOCK" ]; then
    echo "error: server socket did not appear at $SOCK" >&2
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    exit 1
fi

# start player panes
TOTAL=${#NAMES[@]}
for (( i=1; i<=TOTAL; i++ )); do
    idx=$((i - 1))
    USER="${NAMES[$idx]}"
    if [ "$i" -le "$HUMANS" ]; then
        # human player: no --bot flag
        CMD="sudo -u $USER python3 $GAME_DIR/client.py --sock $SOCK --config $CONFIG; read"
    else
        # bot player
        CMD="sudo -u $USER python3 $GAME_DIR/client.py --bot --sock $SOCK --config $CONFIG; read"
    fi
    tmux split-window -t "$SESSION" "$CMD"
    tmux select-layout -t "$SESSION" tiled
done

echo "tmux session '$SESSION' started with server + ${TOTAL} players ($HUMANS human)."
echo "Attach with: tmux attach -t $SESSION"

# attach to the session
tmux attach -t "$SESSION"
