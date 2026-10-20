"""Game client: human and bot modes (PRD section 7)."""
from __future__ import annotations

import asyncio
import random
import sys
from typing import Dict, List, Optional

import commlib
from bot import bot_plan
from schema import (
    ACTIONS, TIME_PER_ROUND, Action,
    CandidateDef, CandidateState, load_config, portion,
    PUBLIC_POLL_FRAC,
)
from world import World

BAR = "=" * 80


# --- prefix matching ---
# Unique-prefix lookup for district and candidate names.

def prefix_match(prefix: str, choices: dict) -> str:
    """Find unique key in choices starting with prefix (case-fold).

    choices: {key: display_name} where key is lowercase.
    Raises ValueError if ambiguous or no match.
    """
    p = prefix.lower()
    matches = [k for k in choices if k.startswith(p)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0:
        raise ValueError(f"no match for '{prefix}'")
    raise ValueError(
        f"ambiguous '{prefix}': {', '.join(matches)}"
    )


# --- display ---
# Print server messages to stdout, styled like mock_sim_output.txt.

def _print_game_setup(
    candidates: list, districts: list,
) -> None:
    """Print the game setup banner: candidates and action menu."""
    print(BAR)
    print("GAME SETUP")
    print(BAR)
    dnames = ", ".join(
        f"{d.name} {d.citizens}" for d in districts
    )
    print(
        f"Rules: 6 rounds. Each round every candidate gets "
        f"{TIME_PER_ROUND} Time (unused Time is lost)."
    )
    print(
        f"       Money starts at $50 and carries over. "
        f"{len(districts)} districts: {dnames} citizens."
    )
    print()
    print("CANDIDATES")
    print(
        f"  {'Name':<6}{'Archetype':<11}"
        f"{'Speech':>7}{'Ground':>7}{'Funding':>8}{'Attack':>7}"
        f"   Stance (taxes, water, transit)"
    )
    for c in candidates:
        st = c.stats
        stance = (
            f"({c.stance[0]:+.1f}, "
            f"{c.stance[1]:+.1f}, "
            f"{c.stance[2]:+.1f})"
        )
        print(
            f"  {c.name:<6}{c.archetype:<11}"
            f"{st['speech']:>+7d}{st['ground']:>+7d}"
            f"{st['funding']:>+8d}{st['attack']:>+7d}"
            f"   {stance}"
        )
    print()
    print("ACTION MENU")
    print(
        f"  {'Command':<14}{'Time':>5}{'Money':>7}   Effect"
    )
    _menu = [
        ("promo <dist>", "promo"),
        ("attack <n> <d>", "attack"),
        ("fundraise", "fundraise"),
        ("poll <dist>", "poll"),
        ("rally <dist>", "rally"),
        ("dig <name>", "dig"),
        ("interview", "interview"),
    ]
    for cmd_str, key in _menu:
        a = ACTIONS[key]
        money = f"${a['money']}" if a["money"] else "$0"
        print(
            f"  {cmd_str:<14}{a['time']:>5}{money:>7}   {a['effect']}"
        )
    print()
    print("OTHER COMMANDS")
    print("  say <text>             chat to everyone")
    print("  whisper <name> <text>  private message")
    print("  dig <name>             dig for dirt")
    print("  end                    finish your turn")
    print("  me                     show your status")
    print("  map                    show latest poll")
    print(BAR)


def _print_welcome(
    msg: dict, candidates: list, districts: list,
) -> None:
    """Print welcome with full game setup."""
    _print_game_setup(candidates, districts)
    print()
    print(
        f"You are {msg['name']} the {msg['archetype']} "
        f"(seat {msg['seat']})"
    )
    print(f"  Money: ${msg['money']}  Time: {msg['time']}")
    print()


def _print_round(msg: dict, candidates: list) -> None:
    print()
    print(BAR)
    print(
        f"ROUND {msg['round']}  "
        f"(deadline: {msg['deadline']}s)"
    )
    print(BAR)
    poll = msg.get("poll", {})
    if poll:
        _print_poll_table(poll, candidates)


def _print_poll_table(poll: dict, candidates: list) -> None:
    """Print a district x candidate poll table."""
    seats = [c.seat for c in candidates]
    names = [c.name for c in candidates]
    hdr = (
        f"  {'District':<14}"
        + "".join(f"{n:>8}" for n in names)
        + "   Leader"
    )
    print(hdr)
    for d_id, shares in poll.items():
        row = f"  {d_id:<14}"
        best_seat = None
        best_val = -1.0
        for s in seats:
            v = shares.get(s, shares.get(str(s), 0))
            if v > best_val:
                best_val, best_seat = v, s
            row += f"{v:>7.0%} "
        leader = ""
        if best_seat is not None:
            for c in candidates:
                if c.seat == best_seat:
                    leader = c.name
        row += f"   {leader}"
        print(row)
    print()


def _print_queued(msg: dict) -> None:
    print(
        f"  Queued: {msg['kind']}  "
        f"cost T{msg['cost_time']} ${msg['cost_money']}  "
        f"left T{msg['time_left']} ${msg['money_left']}"
    )


def _print_error(msg: dict) -> None:
    print(f"  ERROR: {msg['reason']}")


def _print_news(msg: dict) -> None:
    print()
    print(BAR)
    print("NEWS")
    print(BAR)
    for line in msg.get("lines", []):
        print(f"  {line}")
    print()


def _print_result(msg: dict) -> None:
    print()
    print("YOUR RESULTS")
    for r in msg.get("results", []):
        ok = "OK" if r.get("ok") else "FAIL"
        print(
            f"  {r['kind']}: {ok} -- {r.get('effect', '')}"
            f"  (${r.get('money_after', '?')})"
        )
    print()


def _print_poll_result(msg: dict) -> None:
    print(f"\n  PRIVATE POLL: {msg['district']}")
    shares = msg.get("shares", {})
    for seat, share in shares.items():
        print(f"    Seat {seat}: {share:.1%}")
    print()


def _print_chat(msg: dict) -> None:
    scope = msg.get("scope", "")
    name = msg.get("name", "?")
    text = msg.get("text", "")
    if scope == "whisper":
        print(f"  [whisper from {name}] {text}")
    elif scope == "group":
        print(f"  [group from {name}] {text}")
    elif scope == "system":
        print(f"  [server] {text}")
    else:
        print(f"  <{name}> {text}")


def _print_threat(msg: dict) -> None:
    name = msg.get("name", "?")
    text = msg.get("text", "")
    print(f"  [THREAT from {name}, VERIFIED] {text}")


def _print_election(msg: dict, candidates: list) -> None:
    print()
    print(BAR)
    print("ELECTION RESULTS")
    print(BAR)
    won = msg.get("won", {})
    votes = msg.get("votes", {})
    # print per-district results if available
    if votes:
        seats = [c.seat for c in candidates]
        names = [c.name for c in candidates]
        hdr = (
            f"  {'District':<14}"
            + "".join(f"{n:>8}" for n in names)
            + "   Winner"
        )
        print(hdr)
        for d_id, d_votes in votes.items():
            row = f"  {d_id:<14}"
            best_seat = None
            best_val = -1.0
            for s in seats:
                v = d_votes.get(s, d_votes.get(str(s), 0))
                if v > best_val:
                    best_val, best_seat = v, s
                row += f"{v:>8.1f}"
            winner = ""
            if best_seat is not None:
                for c in candidates:
                    if c.seat == best_seat:
                        winner = c.name
            row += f"   {winner}"
            print(row)
        print()
    # summary
    won_parts = []
    for c in candidates:
        key = c.seat
        count = won.get(key, won.get(str(key), 0))
        won_parts.append(f"{c.name} {count}")
    print(f"  Districts won: {', '.join(won_parts)}")
    winner_seat = msg.get("winner")
    winner_name = ""
    for c in candidates:
        if c.seat == winner_seat:
            winner_name = f"{c.name} ({c.archetype})"
    print(f"  WINNER: {winner_name}")
    print(BAR)


# --- input parsing ---
# Parse human input line into protocol command dict.

def parse_input(
    line: str,
    dist_map: dict,
    cand_map: dict,
    seat_by_name: dict,
) -> Optional[dict]:
    """Parse a user input line into a protocol command dict."""
    parts = line.strip().split(None, 1)
    if not parts:
        return None
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if cmd == "end":
        return {"cmd": "end"}
    if cmd == "me":
        return {"cmd": "me"}
    if cmd == "map":
        return {"cmd": "map"}
    if cmd == "help":
        return {"cmd": "help"}
    if cmd == "fundraise":
        return {"cmd": "act", "kind": "fundraise"}
    if cmd == "interview":
        return {"cmd": "act", "kind": "interview"}

    if cmd == "promo":
        d = prefix_match(rest.strip(), dist_map)
        return {"cmd": "act", "kind": "promo", "district": d}
    if cmd == "poll":
        d = prefix_match(rest.strip(), dist_map)
        return {"cmd": "act", "kind": "poll", "district": d}
    if cmd == "rally":
        d = prefix_match(rest.strip(), dist_map)
        return {"cmd": "act", "kind": "rally", "district": d}

    if cmd == "attack":
        # attack <name> <district>
        ap = rest.strip().split(None, 1)
        if len(ap) < 2:
            raise ValueError("usage: attack <name> <district>")
        t = prefix_match(ap[0], cand_map)
        d = prefix_match(ap[1], dist_map)
        return {
            "cmd": "act", "kind": "attack",
            "target": seat_by_name[t], "district": d,
        }
    if cmd == "dig":
        t = prefix_match(rest.strip(), cand_map)
        return {
            "cmd": "act", "kind": "dig",
            "target": seat_by_name[t],
        }

    if cmd == "say":
        return {"cmd": "say", "text": rest}
    if cmd == "whisper":
        wp = rest.strip().split(None, 1)
        if len(wp) < 2:
            raise ValueError("usage: whisper <name> <text>")
        t = prefix_match(wp[0], cand_map)
        return {"cmd": "whisper", "to": seat_by_name[t], "text": wp[1]}
    if cmd == "group":
        gp = rest.strip().split(None, 1)
        if len(gp) < 2:
            raise ValueError("usage: group <name,name> <text>")
        names = gp[0].split(",")
        seats = [seat_by_name[prefix_match(n, cand_map)] for n in names]
        return {"cmd": "group", "to": seats, "text": gp[1]}

    if cmd == "threaten":
        tp = rest.strip().split(None, 1)
        if len(tp) < 2:
            raise ValueError("usage: threaten <name,name> <text>")
        names = tp[0].split(",")
        seats = [seat_by_name[prefix_match(n, cand_map)] for n in names]
        return {"cmd": "threaten", "to": seats, "text": tp[1]}
    if cmd == "truce":
        t = prefix_match(rest.strip(), cand_map)
        return {"cmd": "truce", "to": seat_by_name[t]}

    raise ValueError(f"unknown command '{cmd}'")


# --- bot session ---
# Runs a bot client: connect, join, play all rounds automatically.

async def run_bot_session(
    sock_path: str, name: str, config_path: str,
) -> dict:
    """Run a bot client. Returns dict with election result."""
    _, districts, candidates = load_config(config_path)
    dist_defs = {d.id: d for d in districts}
    cand_defs = {c.seat: c for c in candidates}

    conn = await commlib.connect(sock_path)
    await commlib.send(conn, {"cmd": "join", "name": name})

    my_seat = None
    my_state = None
    world = None
    rnd = 0
    result_data: dict = {}

    while True:
        msg = await commlib.recv(conn)
        if msg is None:
            break

        t = msg.get("t")
        if t == "welcome":
            my_seat = msg["seat"]
            _print_welcome(msg, candidates, districts)
            # create local world and state for bot_plan
            raw, _, _ = load_config(config_path)
            world = World(
                random.Random(raw["seed"]), districts, candidates,
            )
            my_state = CandidateState.fresh(my_seat)

        elif t == "round":
            rnd = msg["round"]
            # JSON keys are strings; bot_plan needs int seats
            raw_poll = msg.get("poll", {})
            poll = {
                d: {int(s): v for s, v in sh.items()}
                for d, sh in raw_poll.items()
            }
            _print_round(msg, candidates)
            # bot: plan and send actions
            if world and my_state and my_seat:
                cdef = cand_defs[my_seat]
                # reset per-round state
                my_state.time = TIME_PER_ROUND
                my_state.interviewed = False
                actions = bot_plan(
                    world, cdef, my_state, poll, rnd,
                )
                # send all actions then end, no inline drain
                for act in actions:
                    await commlib.send(conn, {
                        "cmd": "act", "kind": act.kind,
                        "district": act.district,
                        "target": act.target,
                    })
                await commlib.send(conn, {"cmd": "end"})

        elif t == "queued":
            _print_queued(msg)
            if my_state:
                my_state.time -= msg["cost_time"]
                my_state.money -= msg["cost_money"]
        elif t == "error":
            _print_error(msg)
        elif t == "warn":
            print(f"  WARNING: {msg.get('seconds', 10)}s left!")
        elif t == "news":
            _print_news(msg)
        elif t == "result":
            _print_result(msg)
            # update local money from results
            for r in msg.get("results", []):
                if my_state and r.get("money_after") is not None:
                    my_state.money = r["money_after"]
        elif t == "poll":
            _print_poll_result(msg)
        elif t == "chat":
            _print_chat(msg)
        elif t == "threat":
            _print_threat(msg)
        elif t == "truce":
            status = msg.get("status", "")
            print(f"  Truce with seat {msg.get('target')}: {status}")
        elif t == "election":
            _print_election(msg, candidates)
            result_data["election"] = msg
            break

    # small delay so peer finishes pending writes before we close
    await asyncio.sleep(0.05)
    conn.close()
    return result_data


# --- human session ---
# Runs a human client with stdin input.

async def run_human_session(
    sock_path: str, name: str, config_path: str,
) -> None:
    """Run a human client with interactive input."""
    _, districts, candidates = load_config(config_path)
    dist_map = {d.id: d.name for d in districts}
    cand_map = {c.name.lower(): c.name for c in candidates}
    seat_by_name = {c.name.lower(): c.seat for c in candidates}

    conn = await commlib.connect(sock_path)
    await commlib.send(conn, {"cmd": "join", "name": name})

    done = False

    async def listener() -> None:
        nonlocal done
        while not done:
            msg = await commlib.recv(conn)
            if msg is None:
                done = True
                break
            t = msg.get("t")
            if t == "welcome":
                _print_welcome(msg, candidates, districts)
                print("> ", end="", flush=True)
            elif t == "round":
                _print_round(msg, candidates)
                print("> ", end="", flush=True)
            elif t == "queued":
                _print_queued(msg)
            elif t == "error":
                _print_error(msg)
            elif t == "warn":
                print(f"\n  WARNING: {msg.get('seconds', 10)}s left!")
                print("> ", end="", flush=True)
            elif t == "news":
                _print_news(msg)
            elif t == "result":
                _print_result(msg)
            elif t == "poll":
                _print_poll_result(msg)
            elif t == "chat":
                _print_chat(msg)
            elif t == "threat":
                _print_threat(msg)
            elif t == "truce":
                status = msg.get("status", "")
                print(
                    f"  Truce with seat {msg.get('target')}: "
                    f"{status}"
                )
            elif t == "election":
                _print_election(msg, candidates)
                done = True
                break

    async def input_reader() -> None:
        nonlocal done
        loop = asyncio.get_running_loop()
        while not done:
            try:
                line = await loop.run_in_executor(
                    None, sys.stdin.readline,
                )
            except EOFError:
                break
            if not line:
                break
            line = line.strip()
            if not line:
                print("> ", end="", flush=True)
                continue
            try:
                cmd = parse_input(
                    line, dist_map, cand_map, seat_by_name,
                )
                if cmd:
                    await commlib.send(conn, cmd)
            except ValueError as e:
                print(f"  Error: {e}")
            if not done:
                print("> ", end="", flush=True)

    listen_task = asyncio.ensure_future(listener())
    input_task = asyncio.ensure_future(input_reader())
    await asyncio.gather(listen_task, input_task)
    conn.close()


# --- main ---

def main() -> None:
    """Entry point."""
    import argparse
    parser = argparse.ArgumentParser(description="Game client")
    parser.add_argument("--name", required=True)
    parser.add_argument("--bot", action="store_true")
    parser.add_argument("--sock", default="./run/game.sock")
    parser.add_argument("--config", default="game.json")
    args = parser.parse_args()

    if args.bot:
        asyncio.run(run_bot_session(args.sock, args.name, args.config))
    else:
        asyncio.run(run_human_session(args.sock, args.name, args.config))


if __name__ == "__main__":
    main()
