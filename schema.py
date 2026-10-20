"""Data model: enums, records, constants, config loading (PRD section 3)."""
from __future__ import annotations

import dataclasses
import enum
from typing import Dict, List, Optional, Set


# --- enums ---
# Issue and Phase; used everywhere to avoid bare strings.

class Issue(str, enum.Enum):
    TAXES = "taxes"
    WATER = "water"
    TRANSIT = "transit"


class Phase(str, enum.Enum):
    LOBBY = "lobby"
    OPEN = "open"
    RESOLVING = "resolving"
    ELECTION = "election"
    DONE = "done"


# --- constants ---
# Game-wide numbers; imported by world.py, server.py, bot.py.

SPREAD: float = 0.4
BETA: float = 4.0
DECAY: float = 0.7
PERSUADE: float = 0.05
REACH_FRAC: float = 0.5
SUPPORTER_DIST: float = 0.6
T_START: float = 0.5
T_MAX: float = 0.95
PUBLIC_POLL_FRAC: float = 0.6
PRIVATE_POLL_FRAC: float = 0.8
INTERVIEW_FRAC: float = 0.2
GAFFE: float = 0.15
BACKLASH: float = 0.12
DIG_BASE: float = 1 / 3  # +1/12 per Attack point (PRD R-17)
ROUNDS: int = 6
TIME_PER_ROUND: int = 3
START_MONEY: int = 50

ARCHETYPES: dict[str, dict[str, int]] = {
    "Visionary": {"speech": 2, "ground": 1, "funding": 0, "attack": -1},
    "Firebrand": {"speech": 1, "ground": -1, "funding": 0, "attack": 2},
    "Crusader":  {"speech": -1, "ground": 1, "funding": 2, "attack": 0},
    "Veteran":   {"speech": 0, "ground": 2, "funding": 1, "attack": -1},
}

ACTIONS: dict[str, dict] = {
    "promo": {
        "label": "Promo Ad", "time": 1, "money": 20,
        "effect": "50% of one district: +0.15 goodwill, "
                  "stance 5% toward yours",
        "lasts": "goodwill keeps 70%/round; stance permanent",
    },
    "attack": {
        "label": "Attack Ad", "time": 1, "money": 20,
        "effect": "NEEDS DIRT. 50% of one district: "
                  "-0.15 goodwill for rival (x Attack); 12% backfire",
        "lasts": "keeps 70%/round",
    },
    "fundraise": {
        "label": "Fundraise", "time": 1, "money": 0,
        "effect": "+$15, plus $5 per Funding point",
        "lasts": "instant",
    },
    "poll": {
        "label": "Poll", "time": 1, "money": 5,
        "effect": "ask 80% of one district; only you see answers",
        "lasts": "a snapshot",
    },
    "rally": {
        "label": "Rally", "time": 2, "money": 10,
        "effect": "citizens near your stance: +turnout (x Speech),"
                  " +0.05 goodwill, stance 5% toward yours; "
                  "Ground>=1 also hits neighbour",
        "lasts": "turnout and stance permanent",
    },
    "dig": {
        "label": "Dig dirt", "time": 1, "money": 10,
        "effect": "chance to get dirt on a rival (secret)",
        "lasts": "dirt is permanent (until mutual truce)",
    },
    "interview": {
        "label": "Interview", "time": 1, "money": 0,
        "effect": "20% of every district: +0.08 goodwill, "
                  "stance 2.5% toward yours; 15% gaffe; once/round",
        "lasts": "goodwill keeps 70%/round",
    },
}


def portion(size: int, frac: float) -> int:
    """How many citizens a fraction covers (at least 1)."""
    return max(1, round(frac * size))


# --- records ---
# Dataclasses for game state; used by world.py and server.py.

@dataclasses.dataclass
class DistrictDef:
    """A district's fixed config (D-2)."""
    id: str
    name: str
    citizens: int
    character: List[float]
    order: int


@dataclasses.dataclass
class CandidateDef:
    """A candidate's fixed config (D-4)."""
    seat: int
    user: str
    name: str
    archetype: str
    stance: List[float]
    is_bot: bool
    stats: Dict[str, int]


@dataclasses.dataclass
class CandidateState:
    """A candidate's mutable state during a game (D-5)."""
    seat: int
    money: int
    time: int
    interviewed: bool
    ended: bool
    connected: bool
    dirt_on: Set[int]
    truce_with: Set[int]

    @classmethod
    def fresh(cls, seat: int) -> CandidateState:
        """New state at game start."""
        return cls(
            seat=seat, money=START_MONEY, time=TIME_PER_ROUND,
            interviewed=False, ended=False, connected=False,
            dirt_on=set(), truce_with=set(),
        )


@dataclasses.dataclass
class Action:
    """A queued action (D-9)."""
    seat: int
    kind: str
    district: Optional[str]
    target: Optional[int]


@dataclasses.dataclass
class Result:
    """Outcome of one resolved action (D-10)."""
    seat: int
    kind: str
    ok: bool
    cost_time: int
    cost_money: int
    effect: str
    money_after: int


@dataclasses.dataclass
class Poll:
    """A poll snapshot (D-11)."""
    district: str
    asked: int
    answered: int
    shares: Dict[int, float]
    margin: float


# --- config loading ---
# Reads game.json and validates it; called once at server startup.

_TOP_KEYS = ("seed", "rounds", "round_seconds", "districts", "seats")
_DIST_KEYS = ("id", "name", "citizens", "character")
_SEAT_KEYS = ("seat", "user", "name", "archetype", "stance", "is_bot")


def _require(d: dict, keys: tuple, context: str) -> None:
    """Raise ValueError if any key is missing from d."""
    for k in keys:
        if k not in d:
            raise ValueError(f"{context}: missing required key '{k}'")


def load_config(
    path: str,
) -> tuple:
    """Load game.json, validate, return (raw, districts, candidates)."""
    import json
    with open(path) as f:
        raw = json.load(f)

    _require(raw, _TOP_KEYS, "config")

    # districts
    districts: List[DistrictDef] = []
    seen_ids: Set[str] = set()
    for i, d in enumerate(raw["districts"]):
        _require(d, _DIST_KEYS, f"district {i}")
        did = d["id"]
        if did in seen_ids:
            raise ValueError(f"duplicate district id '{did}'")
        seen_ids.add(did)
        ch = d["character"]
        if not isinstance(ch, list) or len(ch) != 3:
            raise ValueError(
                f"district '{did}': character must be [f, f, f]"
            )
        districts.append(DistrictDef(
            id=did, name=d["name"],
            citizens=d["citizens"], character=ch, order=i,
        ))

    # candidates
    candidates: List[CandidateDef] = []
    seen_seats: Set[int] = set()
    for i, s in enumerate(raw["seats"]):
        _require(s, _SEAT_KEYS, f"seat {i}")
        seat = s["seat"]
        if seat in seen_seats:
            raise ValueError(f"duplicate seat {seat}")
        seen_seats.add(seat)
        arch = s["archetype"]
        if arch not in ARCHETYPES:
            raise ValueError(
                f"seat {seat}: unknown archetype '{arch}'"
            )
        st = s["stance"]
        if not isinstance(st, list) or len(st) != 3:
            raise ValueError(
                f"seat {seat}: stance must be [f, f, f]"
            )
        candidates.append(CandidateDef(
            seat=seat, user=s["user"], name=s["name"],
            archetype=arch, stance=st, is_bot=s["is_bot"],
            stats=dict(ARCHETYPES[arch]),
        ))

    candidates.sort(key=lambda c: c.seat)
    return raw, districts, candidates
