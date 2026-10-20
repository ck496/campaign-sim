"""Game server: lobby, rounds, actions, resolve, chat (PRD section 6)."""
from __future__ import annotations

import asyncio
import dataclasses
import re
from typing import Dict, List, Optional, Set

import commlib
from protocol import build_server_msg, parse_client_msg, strip_control
from schema import (
    ACTIONS, Phase, ROUNDS, TIME_PER_ROUND,
    Action, CandidateState, CandidateDef, load_config, portion,
    PUBLIC_POLL_FRAC,
)
from world import World, resolve_truces


# --- auth resolution ---
# Picks peercred or name based on platform and game.json users.

def _resolve_auth(
    auth_mode: str, candidates: List[CandidateDef],
) -> str:
    """Resolve 'auto' to 'peercred' or 'name'. Returns resolved mode."""
    if auth_mode in ("peercred", "name"):
        print(f"Auth: {auth_mode} (explicit)")
        return auth_mode
    # auto: peercred only if SO_PEERCRED exists and all users are valid
    import socket as _sock
    if not hasattr(_sock, "SO_PEERCRED"):
        print("Auth: name (no SO_PEERCRED on this platform)")
        return "name"
    import pwd
    for c in candidates:
        try:
            pwd.getpwnam(c.user)
        except KeyError:
            print(f"Auth: name (user '{c.user}' not found)")
            return "name"
    print("Auth: peercred (all users exist, SO_PEERCRED available)")
    return "peercred"

# --- name validation ---
# 1-16 chars of [a-z0-9_-], case-folded (C-5).

_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,16}$")


def _valid_name(name: str) -> Optional[str]:
    """Case-fold and validate a join name. Returns clean or None."""
    folded = name.lower()
    return folded if _NAME_RE.match(folded) else None


# --- server ---
# One class, one event loop, sync on_message (C-6).

class GameServer:
    """The game server: lobby, rounds, resolve, chat, election."""

    def __init__(
        self, config_path: str, auth_mode: str, sock_path: str,
    ) -> None:
        raw, districts, candidates = load_config(config_path)
        self.raw, self.districts, self.candidates = (
            raw, districts, candidates,
        )
        self.auth_mode = _resolve_auth(auth_mode, candidates)
        self.sock_path = sock_path

        import random
        self.world = World(
            random.Random(raw["seed"]), districts, candidates,
        )
        self.seats = [c.seat for c in candidates]
        self.states: Dict[int, CandidateState] = {
            c.seat: CandidateState.fresh(c.seat) for c in candidates
        }
        self.conns: Dict[int, commlib.Conn] = {}
        self.conn_to_seat: Dict[commlib.Conn, int] = {}
        self.phase = Phase.LOBBY
        self.round_num = 0
        self.queue: Dict[int, List[Action]] = {
            s: [] for s in self.seats
        }
        self.truces: Dict[int, Set[int]] = {
            s: set() for s in self.seats
        }
        self.timer_handle: Optional[asyncio.Task] = None
        self.last_poll: dict = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._dispatch = {
            "join": self._handle_join,
            "act": self._handle_act,
            "end": lambda c, m: self._handle_end(c),
            "say": self._handle_say,
            "whisper": self._handle_whisper,
            "group": self._handle_group,
            "threaten": self._handle_threaten,
            "truce": self._handle_truce,
            "me": lambda c, m: self._handle_me(c),
            "map": lambda c, m: self._handle_map(c),
            "help": lambda c, m: self._handle_help(c),
        }

    # --- lifecycle ---

    async def run(self) -> None:
        """Start the UNIX socket listener."""
        # S-1: peercred 0660 (group can connect), name 0600 (owner only)
        sock_mode = 0o660 if self.auth_mode == "peercred" else 0o600
        self._server = await commlib.serve(
            self.sock_path, self._on_connect,
            self._on_message, self._on_disconnect,
            mode=sock_mode,
        )

    def stop(self) -> None:
        """Shut down the server."""
        if self._server:
            self._server.close()
            self._server = None
        if self.timer_handle:
            self.timer_handle.cancel()
            self.timer_handle = None

    # --- callbacks ---

    async def _on_connect(self, conn: commlib.Conn) -> None:
        pass

    async def _on_disconnect(self, conn: commlib.Conn) -> None:
        seat = self.conn_to_seat.pop(conn, None)
        if seat is None:
            return
        self.conns.pop(seat, None)
        self.states[seat].connected = False
        if self.phase == Phase.OPEN and not self.states[seat].ended:
            self.states[seat].ended = True
            self._maybe_close_round()

    def _on_message(self, conn: commlib.Conn, raw: dict) -> None:
        """Dispatch one client message. Sync for C-6."""
        try:
            msg = parse_client_msg(raw)
        except ValueError as e:
            self._err(conn, str(e))
            return
        cmd = msg["cmd"]
        if cmd != "join" and conn not in self.conn_to_seat:
            self._err(conn, "join first")
            return
        handler = self._dispatch.get(cmd)
        if handler:
            handler(conn, msg)

    # --- send helpers ---

    def _send(self, conn: commlib.Conn, obj: dict) -> None:
        asyncio.ensure_future(commlib.send(conn, obj))

    def _broadcast(self, obj: dict) -> None:
        conns = list(self.conns.values())
        asyncio.ensure_future(commlib.multicast(conns, obj))

    def _send_to_seat(self, seat: int, obj: dict) -> None:
        conn = self.conns.get(seat)
        if conn:
            asyncio.ensure_future(commlib.send(conn, obj))

    def _err(self, conn: commlib.Conn, reason: str) -> None:
        self._send(conn, build_server_msg("error", reason=reason))

    def _seat_name(self, seat: int) -> str:
        return self.world.cand_defs[seat].name

    # --- join ---

    def _handle_join(self, conn: commlib.Conn, msg: dict) -> None:
        """Assign seat by name (name mode) or UID (peercred mode)."""
        if conn in self.conn_to_seat:
            return self._err(conn, "already joined")

        if self.auth_mode == "peercred":
            target = self._join_peercred(conn)
        else:
            target = self._join_name(conn, msg)
        if target is None:
            return

        # assign
        self.conns[target] = conn
        self.conn_to_seat[conn] = target
        self.states[target].connected = True
        cdef = self.world.cand_defs[target]
        self._send(conn, build_server_msg(
            "welcome", seat=target, name=cdef.name,
            archetype=cdef.archetype, stats=cdef.stats,
            money=self.states[target].money,
            time=self.states[target].time,
        ))
        if self.phase == Phase.LOBBY and self._all_humans_joined():
            self._start_game()

    def _join_name(
        self, conn: commlib.Conn, msg: dict,
    ) -> Optional[int]:
        """Name mode: match by name/user or first free non-bot."""
        name = _valid_name(msg["name"])
        if name is None:
            self._err(conn, "invalid name")
            return None
        for s, st in self.states.items():
            if st.connected and self._seat_name(s).lower() == name:
                self._err(conn, "name taken")
                return None
        target = self._find_seat(name)
        if target is None:
            self._err(conn, "no free seat")
        return target

    def _join_peercred(self, conn: commlib.Conn) -> Optional[int]:
        """Peercred mode: identify seat by kernel-verified UID (S-2)."""
        uid = commlib.peer_uid(conn)
        if uid is None:
            self._err(conn, "cannot read peer credentials")
            return None
        # map UID to a candidate's user field
        import pwd
        for c in self.candidates:
            try:
                pw = pwd.getpwnam(c.user)
            except KeyError:
                continue
            if pw.pw_uid == uid and not self.states[c.seat].connected:
                return c.seat
        # UID matched no free seat
        self._err(conn, "no seat for your UID")
        return None

    def _find_seat(self, name: str) -> Optional[int]:
        """Find seat by name/user match, or first free non-bot."""
        for c in self.candidates:
            if c.name.lower() == name or c.user.lower() == name:
                if not self.states[c.seat].connected:
                    return c.seat
        for c in self.candidates:
            if not c.is_bot and not self.states[c.seat].connected:
                return c.seat
        return None

    def _all_humans_joined(self) -> bool:
        return all(
            self.states[c.seat].connected
            for c in self.candidates if not c.is_bot
        )

    # --- round lifecycle ---

    def _start_game(self) -> None:
        self._take_public_poll()
        self._start_round(1)

    def _start_round(self, n: int) -> None:
        """Open round n: broadcast round message, start timer."""
        self.round_num, self.phase = n, Phase.OPEN
        for s in self.seats:
            st = self.states[s]
            st.time, st.interviewed, st.ended = (
                TIME_PER_ROUND, False, False,
            )
            self.queue[s], self.truces[s] = [], set()
        for c in self.candidates:
            if c.is_bot and not self.states[c.seat].connected:
                self.states[c.seat].ended = True
        deadline = self.raw.get("round_seconds", 60)
        self._broadcast(build_server_msg(
            "round", round=n, deadline=deadline,
            poll=self.last_poll,
        ))
        self.timer_handle = asyncio.ensure_future(
            self._round_timer(deadline),
        )

    async def _round_timer(self, seconds: int) -> None:
        try:
            await asyncio.sleep(max(0, seconds - 10))
            self._broadcast(build_server_msg("warn", seconds=10))
            await asyncio.sleep(min(seconds, 10))
            self._close_round()
        except asyncio.CancelledError:
            pass

    def _handle_end(self, conn: commlib.Conn) -> None:
        seat = self.conn_to_seat[conn]
        if self.phase != Phase.OPEN:
            return self._err(conn, "not in a round")
        if not self.states[seat].ended:
            self.states[seat].ended = True
            self._maybe_close_round()

    def _maybe_close_round(self) -> None:
        if all(self.states[s].ended for s in self.seats):
            self._close_round()

    def _close_round(self) -> None:
        if self.timer_handle:
            self.timer_handle.cancel()
            self.timer_handle = None
        if self.phase == Phase.OPEN:
            self._resolve()

    # --- resolve ---

    def _resolve(self) -> None:
        """Resolve queued actions, send results, news, next."""
        self.phase = Phase.RESOLVING
        results, polls = self._apply_all()
        truce_pairs = resolve_truces(self.states)
        self._send_results(results, polls)
        self._broadcast_news(truce_pairs)
        self.world.tick()
        self._take_public_poll()
        if self.round_num < self.raw.get("rounds", ROUNDS):
            self._start_round(self.round_num + 1)
        else:
            self._election()

    def _apply_all(self) -> tuple:
        """Apply all queued actions. Returns (results, polls)."""
        results: Dict[int, list] = {s: [] for s in self.seats}
        polls: Dict[int, list] = {}
        for s in self.seats:
            for act in self.queue[s]:
                r = self.world.apply(
                    act, self.world.cand_defs[s],
                    self.states[s], pre_deducted=True,
                )
                results[s].append(r)
                if act.kind == "poll" and r.ok:
                    polls.setdefault(s, []).append(
                        self.world._last_poll,
                    )
        return results, polls

    def _send_results(self, results: dict, polls: dict) -> None:
        """Send private results and polls to each seat."""
        for s in self.seats:
            if results[s]:
                self._send_to_seat(s, build_server_msg(
                    "result",
                    results=[
                        dataclasses.asdict(r) for r in results[s]
                    ],
                ))
            for p in polls.get(s, []):
                self._send_to_seat(s, build_server_msg(
                    "poll", district=p.district, asked=p.asked,
                    answered=p.answered, shares=p.shares,
                    margin=p.margin,
                ))

    def _broadcast_news(self, truce_pairs: list) -> None:
        """Build and broadcast news (no digs, no polls)."""
        lines = []
        for s in self.seats:
            cname = self._seat_name(s)
            for act in self.queue[s]:
                if act.kind in ("dig", "poll"):
                    continue
                label = ACTIONS[act.kind]["label"]
                if act.district:
                    lines.append(
                        f"{cname} used {label} in {act.district}"
                    )
                else:
                    lines.append(f"{cname} used {label}")
        for sa, sb in truce_pairs:
            na, nb = self._seat_name(sa), self._seat_name(sb)
            lines.append(f"{na} and {nb} declared truce")
        self._broadcast(build_server_msg("news", lines=lines))

    def _take_public_poll(self) -> None:
        self.last_poll = {}
        for d in self.districts:
            n = portion(d.citizens, PUBLIC_POLL_FRAC)
            p = self.world.sample_poll(d.id, n)
            self.last_poll[d.id] = p.shares

    def _election(self) -> None:
        r = self.world.election()
        self._broadcast(build_server_msg(
            "election", votes=r["votes"],
            won=r["won"], winner=r["winner"],
        ))
        self.phase = Phase.DONE

    # --- action queueing ---

    def _handle_act(self, conn: commlib.Conn, msg: dict) -> None:
        """Validate and queue an action. Deduct at queue time."""
        seat = self.conn_to_seat[conn]
        state = self.states[seat]
        err = self._validate_act(seat, state, msg)
        if err:
            return self._err(conn, err)
        kind, spec = msg["kind"], ACTIONS[msg["kind"]]
        district, target = msg.get("district"), msg.get("target")
        state.time -= spec["time"]
        state.money -= spec["money"]
        if kind == "interview":
            state.interviewed = True
        self.queue[seat].append(Action(
            seat=seat, kind=kind,
            district=district, target=target,
        ))
        self._send(conn, build_server_msg(
            "queued", kind=kind,
            cost_time=spec["time"], cost_money=spec["money"],
            time_left=state.time, money_left=state.money,
        ))

    def _validate_act(
        self, seat: int, state: CandidateState, msg: dict,
    ) -> Optional[str]:
        """Return error string or None if valid."""
        if self.phase != Phase.OPEN:
            return "not in a round"
        if state.ended:
            return "already ended"
        kind = msg.get("kind")
        if kind not in ACTIONS:
            return f"unknown action '{kind}'"
        spec = ACTIONS[kind]
        district = msg.get("district")
        target = msg.get("target")
        if kind in ("promo", "attack", "poll", "rally"):
            if not district:
                return "missing district"
            if district not in self.world.dist_defs:
                return "unknown district"
        if kind in ("attack", "dig"):
            if target is None:
                return "missing target"
            if target not in self.world.cand_defs:
                return "unknown target"
            if target == seat:
                return "cannot target yourself"
        if kind == "attack" and target not in state.dirt_on:
            return "no dirt on target"
        if kind == "interview" and state.interviewed:
            return "already interviewed"
        if state.time < spec["time"]:
            return "not enough time"
        if state.money < spec["money"]:
            return "not enough money"
        return None

    # --- chat ---

    def _handle_say(self, conn: commlib.Conn, msg: dict) -> None:
        seat = self.conn_to_seat[conn]
        text = strip_control(msg["text"])
        self._broadcast(build_server_msg(
            "chat", scope="all", seat=seat,
            name=self._seat_name(seat), text=text,
        ))

    def _handle_whisper(self, conn: commlib.Conn, msg: dict) -> None:
        seat, target = self.conn_to_seat[conn], msg["to"]
        if target not in self.world.cand_defs:
            return self._err(conn, "unknown target")
        text = strip_control(msg["text"])
        m = build_server_msg(
            "chat", scope="whisper", seat=seat,
            name=self._seat_name(seat), to=target, text=text,
        )
        self._send(conn, m)
        self._send_to_seat(target, m)

    def _handle_group(self, conn: commlib.Conn, msg: dict) -> None:
        seat, targets = self.conn_to_seat[conn], msg["to"]
        text = strip_control(msg["text"])
        m = build_server_msg(
            "chat", scope="group", seat=seat,
            name=self._seat_name(seat), to=targets, text=text,
        )
        self._send(conn, m)
        for t in targets:
            if t in self.conns and t != seat:
                self._send_to_seat(t, m)

    # --- threats and truces ---

    def _handle_threaten(self, conn: commlib.Conn, msg: dict) -> None:
        seat = self.conn_to_seat[conn]
        state = self.states[seat]
        text = strip_control(msg["text"])
        valid = [t for t in msg["to"] if t in state.dirt_on]
        invalid = [t for t in msg["to"] if t not in state.dirt_on]
        if not valid:
            return self._err(conn, "no dirt on any target")
        m = build_server_msg(
            "threat", seat=seat, name=self._seat_name(seat),
            text=text, verified=True,
        )
        for t in valid:
            self._send_to_seat(t, m)
        if invalid:
            self._err(conn, f"no dirt on seats {invalid}")

    def _handle_truce(self, conn: commlib.Conn, msg: dict) -> None:
        seat, target = self.conn_to_seat[conn], msg["to"]
        if target not in self.world.cand_defs:
            return self._err(conn, "unknown target")
        if target == seat:
            return self._err(conn, "cannot truce yourself")
        self.states[seat].truce_with.add(target)
        self._send(conn, build_server_msg(
            "truce", target=target, status="offered",
        ))

    # --- info ---

    def _handle_me(self, conn: commlib.Conn) -> None:
        seat = self.conn_to_seat[conn]
        st = self.states[seat]
        self._send(conn, build_server_msg(
            "result", results=[{
                "seat": seat, "money": st.money, "time": st.time,
                "dirt_count": len(st.dirt_on),
                "queued": len(self.queue[seat]),
            }],
        ))

    def _handle_map(self, conn: commlib.Conn) -> None:
        self._send(conn, build_server_msg(
            "poll", district="all", shares=self.last_poll,
            asked=0, answered=0, margin=0.0,
        ))

    def _handle_help(self, conn: commlib.Conn) -> None:
        cmds = sorted(["act", "end", "say", "whisper", "group",
                        "threaten", "truce", "me", "map", "help"])
        self._send(conn, build_server_msg(
            "chat", scope="system", seat=0, name="server",
            text=f"Commands: {', '.join(cmds)}",
        ))


# --- main ---

def main() -> None:
    """Entry point: parse args, run server."""
    import argparse, os
    parser = argparse.ArgumentParser(description="Game server")
    parser.add_argument(
        "--auth", choices=["name", "peercred", "auto"],
        default="auto",
    )
    parser.add_argument("--sock", default=None)
    parser.add_argument("--config", default="game.json")
    args = parser.parse_args()
    # default socket depends on auth; resolved inside GameServer.__init__
    # but we need a temporary resolve here for the path default
    if args.sock is None:
        args.sock = "./run/game.sock"
    os.makedirs(os.path.dirname(args.sock) or ".", exist_ok=True)
    gs = GameServer(args.config, args.auth, args.sock)

    async def _serve() -> None:
        await gs.run()
        print(f"Server listening on {args.sock} (auth={gs.auth_mode})")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            gs.stop()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
