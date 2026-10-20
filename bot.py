"""Bot AI and game simulation (PRD section 2, 5)."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from schema import (
    ACTIONS, PUBLIC_POLL_FRAC, ROUNDS, TIME_PER_ROUND,
    Action, CandidateDef, CandidateState, load_config, portion,
)
from world import World, resolve_truces


import random


def bot_plan(
    world: World,
    cand_def: CandidateDef,
    state: CandidateState,
    poll: Dict[str, Dict[int, float]],
    rnd: int,
) -> List[Action]:
    """rul-based bot heuristic. Same logic for every archetype."""
    me = cand_def.seat
    stats = cand_def.stats
    money = state.money
    time = TIME_PER_ROUND
    plan: List[Action] = []

    def can(kind: str) -> bool:
        return (
            time >= ACTIONS[kind]["time"]
            and money >= ACTIONS[kind]["money"]
        )

    def take(kind: str, district: Optional[str] = None,
             target: Optional[int] = None) -> None:
        nonlocal money, time
        plan.append(Action(seat=me, kind=kind,
                           district=district, target=target))
        time -= ACTIONS[kind]["time"]
        money -= ACTIONS[kind]["money"]

    def gap(d_id: str) -> float:
        s = poll[d_id]
        lead = max(s, key=s.get)
        return (s[lead] - s[me]) if lead != me else 1.0

    # swing = closest contested district; home = closest to my stance
    swing = min(world.dist_defs, key=gap)
    leader = max(poll[swing], key=poll[swing].get)
    home = min(
        world.districts,
        key=lambda d: sum(
            abs(d.character[k] - cand_def.stance[k]) for k in range(3)
        ),
    ).id

    # 1. fundraise if low on money

    if money < 25 and can("fundraise"):
        take("fundraise")

    # 2. attack / dig / promo

    if (stats["attack"] >= 1 and leader != me
            and leader in state.dirt_on and can("attack")):
        take("attack", district=swing, target=leader)
    elif (stats["attack"] >= 1 and leader != me
          and leader not in state.dirt_on and can("dig")):
        take("dig", target=leader)
    elif can("promo"):
        take("promo", district=swing)

    # 3. rally at home
    if can("rally"):
        take("rally", district=home)

    # 4. poll on even rounds
    if rnd % 2 == 0 and can("poll"):
        take("poll", district=swing)

    # 5. interview if time left
    if time > 0 and can("interview"):
        take("interview")

    return plan


# --- game simulation ---
# Runs a full bot game for testing

PlanFn = Callable[
    [World, CandidateDef, CandidateState,
     Dict[str, Dict[int, float]], int],
    List[Action],
]


def _take_public_poll(world: World) -> Dict[str, Dict[int, float]]:
    """Poll all districts, return {d_id: {seat: share}}."""
    poll: Dict[str, Dict[int, float]] = {}
    for d in world.districts:
        n = portion(d.citizens, PUBLIC_POLL_FRAC)
        p = world.sample_poll(d.id, n)
        poll[d.id] = p.shares
    return poll


def run_bot_game(
    seed: int,
    strategies: Optional[Dict[int, PlanFn]] = None,
) -> dict:
    """Run a full 6-round bot game. Returns election + final money.

    strategies: optional {seat: plan_fn} overriding bot_plan.
    """
    _, districts, candidates = load_config("game.json")
    rng = random.Random(seed)
    world = World(rng, districts, candidates)
    states = {
        c.seat: CandidateState.fresh(c.seat) for c in candidates
    }
    if strategies is None:
        strategies = {}

    # round-0 public poll
    poll = _take_public_poll(world)

    for rnd in range(1, ROUNDS + 1):
        # plan phase (no RNG)
        plans: Dict[int, List[Action]] = {}
        for c in world.candidates:
            plan_fn = strategies.get(c.seat, bot_plan)
            plans[c.seat] = plan_fn(
                world, c, states[c.seat], poll, rnd,
            )

        # resolve phase: actions in seat order
        for c in world.candidates:
            for action in plans[c.seat]:
                world.apply(action, c, states[c.seat])

        # truces
        resolve_truces(states)

        # round end: decay goodwill, reset candidate time/interviewed
        world.tick()
        for st in states.values():
            st.time = TIME_PER_ROUND
            st.interviewed = False

        # public poll
        poll = _take_public_poll(world)

    return {
        "election": world.election(),
        "money": {seat: st.money for seat, st in states.items()},
        "states": states,
    }
