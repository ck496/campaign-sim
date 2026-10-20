# Campaign-Sim

A political campaign election game. 4 candidates (Ada, Bo, Cy, Dee)
compete across 5 districts over 6 rounds. Each candidate is their own
process. Communication happens over UNIX domain sockets through a
single communications library (`commlib.py`).

[Straight to running](#quick-start)

## How the game works

Each district has 50 citizens. Every citizen has a stance on three issues
(taxes, water, transit) and a goodwill value toward each candidate. A citizen
votes for whoever is closest in stance and highest in goodwill. The curve
is steep, so small differences matter.

Actions move those numbers:

- **Promo ad** -- half the district sees it. Goodwill goes up temporarily
  and each citizen shifts a little toward your stance permanently.
- **Attack ad** -- requires dirt on a rival. Lowers their goodwill, can backfire.
- **Dig for dirt** -- chance to find something on a rival. Needed before attack ads.
- **Rally** -- supporters near your stance turn out more and shift toward you.
- **Interview** -- small goodwill boost (or gaffe) spread across every district.
- **Fundraise** -- get more money for future rounds.
- **Poll** -- private sample of where a district stands.

Goodwill fades 30% each round, but stance shifts stay. After six rounds,
each district votes. Win the most districts to win the game. Ties go to
total votes, then lower seat number.

**Future work (hopefully):** Each citizen is modeled as a matrix row, so the whole district could later run as a single GPU kernel. The persuasion math could also be replaced with real voting behavior models so citizens react more realistically to ads and events. Another idea would be to let citizens be swapped for LLM-driven voters so candidates speak freely and citizens actually interpret text and shift sentiment on their own.

## Files

- `commlib.py` -- **Communications library** (the single-file IPC library).
  Built on Python asyncio and UNIX stream sockets. API:
  - `serve(path, on_connect, on_message, on_disconnect, mode)` -- start server
  - `connect(path)` / `recv(conn)` -- client connect and receive
  - `send(conn, obj)` -- unicast to one connection
  - `multicast(conns, obj)` -- send to a chosen group
  - `broadcast(obj)` -- send to every live connection
  - `peer_uid(conn)` -- get Linux UID of peer via SO_PEERCRED
- `server.py` -- Game server. Manages the lobby, rounds, and action resolution.
- `client.py` -- Player client. Runs as human (interactive) or bot (`--bot`).
- `bot.py` -- Bot AI logic, used by client.py when `--bot` is passed.
- `schema.py` -- Game config, data structures, and action definitions.
- `protocol.py` -- Message types and validation for client-server JSON messages.
- `world.py` -- Citizens, districts, persuasion math, polls, and elections.
- `game.json` -- Default game configuration (candidates, districts, seed).
- `run_local.sh` -- Launch a game on macOS (name-based auth).
- `setup.sh` -- Create Linux users and directories for peercred mode.
- `run_game.sh` -- Launch a peercred game on Linux via tmux.

## Quick start 

### Quick test with all bots

```
bash run_local.sh 0
```

This starts the server and connects all 4 candidates as bots. They
play automatically and you'll see election results when it's done.

### Playing as a human

**Step 1.** Start the server and bots. This example gives you 2 human
seats (Ada and Bo) and makes the other 2 (Cy and Dee) play as bots:

```
bash run_local.sh 2
```

The script starts the server, connects Cy and Dee as AI bots in the
background, and prints the commands you need to connect.

**Step 2.** Open a new terminal window, `cd` to the project directory,
and connect as Ada:

```
python3 client.py --name ada --sock ./run/game.sock --config game.json
```

**Step 3.** Open another terminal window and connect as Bo:

```
python3 client.py --name bo --sock ./run/game.sock --config game.json
```

Once both humans connect, the game starts. You'll see a welcome message,
the poll table, and a prompt.

**Step 4.** Play. Each round you get 3 time units to spend on actions:

- `promo midtown` -- run a promo ad in a district
- `rally northbank` -- hold a rally
- `fundraise` -- get more money
- `poll midtown` -- buy a private poll
- `attack cy midtown` -- attack ad (need dirt first)
- `dig cy` -- dig for dirt on someone
- `interview` -- do an interview (once per round)
- `say hello` -- chat to everyone
- `whisper bo secret` -- private message
- `end` -- finish your turn

District and candidate names work with prefixes (`mid` for midtown,
`a` for ada, etc).

Both humans have to type `end` each round for the game to advance.
Cy and Dee (the bots) end their turns automatically.

After 6 rounds you get election results.

**Step 5.** When you're done, hit Ctrl-C in the first terminal (the one
running `run_local.sh`) to stop the server.

You can also add `--bot` to any client command to make that player
play automatically instead of waiting for input.

## Auth modes

The server supports `--auth name` (default), `--auth peercred`
(Linux User only), and `--auth auto` (picks peercred if SO_PEERCRED exists
and all game.json users are real Linux users, otherwise falls back to name).

`run_local.sh` uses name mode. `run_game.sh` uses peercred mode.