#!/usr/bin/env bash

# setup.sh -- create Linux users, group, and directories for peercred mode.
# Must be run as root on the Linux VM.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root" >&2
    exit 1
fi

GROUP="campaign"
MODERATOR="moderator"
PLAYERS=(cand1 cand2 cand3 cand4)
GAME_DIR="/opt/campaign"
RUN_DIR="/run/campaign"

# create group
if ! getent group "$GROUP" >/dev/null 2>&1; then
    groupadd "$GROUP"
    echo "Created group $GROUP"
else
    echo "Group $GROUP already exists"
fi

# create moderator user
if ! id "$MODERATOR" >/dev/null 2>&1; then
    useradd -r -g "$GROUP" -s /usr/sbin/nologin "$MODERATOR"
    echo "Created user $MODERATOR"
else
    usermod -aG "$GROUP" "$MODERATOR"
    echo "User $MODERATOR already exists, ensured in group $GROUP"
fi

# create player users
for u in "${PLAYERS[@]}"; do
    if ! id "$u" >/dev/null 2>&1; then
        useradd -r -g "$GROUP" -s /bin/bash "$u"
        echo "Created user $u"
    else
        usermod -aG "$GROUP" "$u"
        echo "User $u already exists, ensured in group $GROUP"
    fi
done

# create game directory and copy files
mkdir -p "$GAME_DIR"
cp server.py client.py commlib.py protocol.py schema.py world.py bot.py game.json "$GAME_DIR/"
cp run_game.sh "$GAME_DIR/"
chown -R "$MODERATOR:$GROUP" "$GAME_DIR"
chmod 0755 "$GAME_DIR"
chmod 0644 "$GAME_DIR"/*.py "$GAME_DIR"/game.json
chmod 0755 "$GAME_DIR"/run_game.sh
echo "Game files copied to $GAME_DIR"

# create runtime directory for the socket
mkdir -p "$RUN_DIR"
chown "$MODERATOR:$GROUP" "$RUN_DIR"
chmod 0750 "$RUN_DIR"
echo "Runtime directory $RUN_DIR ready (0750 $MODERATOR:$GROUP)"

# allow moderator to sudo as each player (for run_game.sh)
SUDOERS="/etc/sudoers.d/campaign"
cat > "$SUDOERS" <<'SUDOEOF'
# campaign game: moderator can run commands as each player without a password
moderator ALL=(cand1) NOPASSWD: ALL
moderator ALL=(cand2) NOPASSWD: ALL
moderator ALL=(cand3) NOPASSWD: ALL
moderator ALL=(cand4) NOPASSWD: ALL
SUDOEOF
chmod 0440 "$SUDOERS"
echo "Sudoers rules written to $SUDOERS"

echo ""
echo "Setup complete. To run the game:"
echo "  sudo -u $MODERATOR bash $GAME_DIR/run_game.sh"
