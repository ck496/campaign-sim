"""World model handles things like  citizens, choice, polls, election (PRD section 4)."""

from __future__ import annotations


import math
import random
from typing import Dict, List

from schema import (
    ACTIONS, BACKLASH, BETA, DECAY, DIG_BASE, GAFFE,
    INTERVIEW_FRAC, PERSUADE, PRIVATE_POLL_FRAC, REACH_FRAC,
    SPREAD, SUPPORTER_DIST, T_MAX, T_START,
    Action, CandidateDef, CandidateState, DistrictDef, Poll, Result,
    portion,
)


# --- helpers ---
# Small utilities used by World and action resolution.

def clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    """Clamp v to [lo, hi]."""
    return max(lo, min(hi, v))


class Citizen:
    """One citizen: stance, goodwill per candidate, turnout probability."""

    __slots__ = ("x", "g", "t")

    def __init__(self, x: List[float], seats: List[int]) -> None:
        self.x: List[float] = list(x)
        self.g: Dict[int, float] = {s: 0.0 for s in seats}
        self.t: float = T_START


def nudge(cz: Citizen, stance: List[float], a: float = PERSUADE) -> None:
    """Move a citizen's stance toward stance by fraction a."""

    for k in range(3):
        cz.x[k] = clamp(cz.x[k] + a * (stance[k] - cz.x[k]))


def neighbour(d_id: str, districts: List[DistrictDef]) -> str:
    """Next district in order, or previous if d_id is last."""

    for i, d in enumerate(districts):
        if d.id == d_id:
            if i + 1 < len(districts):
                return districts[i + 1].id
            return districts[i - 1].id

    raise ValueError(f"unknown district '{d_id}'")


# --- world ---
# Holds districts, candidates (read-only defs), and all citizens.

class World:
    """The citizen population and its choices."""

    def __init__(
        self,
        rng: random.Random,
        districts: List[DistrictDef],
        candidates: List[CandidateDef],
    ) -> None:
        self.rng = rng

        # sorted district list (by order) and candidate list (by seat)
        self.districts = sorted(districts, key=lambda d: d.order)
        self.candidates = sorted(candidates, key=lambda c: c.seat)
        self.seats: List[int] = [c.seat for c in self.candidates]

       # lookup dicts
        self.dist_defs: Dict[str, DistrictDef] = {
            d.id: d for d in self.districts
        }
        self.cand_defs: Dict[int, CandidateDef] = {
            c.seat: c for c in self.candidates
        }

        # create citizens: district order, citizen order, issue order
        self.cit: Dict[str, List[Citizen]] = {}

        for d in self.districts:
            citizens = []
            for _ in range(d.citizens):
                x = [
                    clamp(d.character[k] + rng.gauss(0, SPREAD))
                    for k in range(3)
                ]
                citizens.append(Citizen(x, self.seats))
            self.cit[d.id] = citizens

    # --- citizen choice ---
    # Softmax over stance distance + goodwill.

    def stance_dist(self, cz: Citizen, seat: int) -> float:
        """Mean absolute distance between citizen and candidate stance."""
        y = self.cand_defs[seat].stance
        return sum(abs(cz.x[k] - y[k]) for k in range(3)) / 3

    def prefs(self, cz: Citizen) -> Dict[int, float]:
        """Softmax vote probabilities for one citizen """
        u = {
            s: BETA * (-self.stance_dist(cz, s) + cz.g[s])
            for s in self.seats
        }
        # no max-subtraction: must match mock_sim.py exactly
        z = sum(math.exp(v) for v in u.values())

        return {s: math.exp(u[s]) / z for s in self.seats}

    def votes(self, d_id: str) -> Dict[int, float]:
        """expected votes per candidate in a district """
        out = {s: 0.0 for s in self.seats}

        for cz in self.cit[d_id]:
            p = self.prefs(cz)
            for s in self.seats:
                out[s] += cz.t * p[s]
        return out

    def true_share(self, d_id: str) -> Dict[int, float]:
        """normalize
          vote shares in a district."""
        v = self.votes(d_id)
        tot = sum(v.values())
        return {s: v[s] / tot for s in self.seats}

    # --- polls ---

    # Sample citizens; each answers if they plan to vote.

    def sample_poll(self, d_id: str, n: int) -> Poll:
        """Poll n random citizens in a district."""
        counts = {s: 0 for s in self.seats}
        answered = 0
        for cz in self.rng.sample(self.cit[d_id], n):
            if self.rng.random() > cz.t:
                continue
            p = self.prefs(cz)
            pick = self.rng.choices(self.seats, weights=[
                p[s] for s in self.seats
            ])[0]
            counts[pick] += 1
            answered += 1
        if answered == 0:
            shares = {s: 0.0 for s in self.seats}
            return Poll(
                district=d_id, asked=n,
                answered=0, shares=shares, margin=0.0,
            )

        shares = {s: counts[s] / answered for s in self.seats}
        # margin for the leader
        leader_p = max(shares.values())
        margin = 1.96 * math.sqrt(
            leader_p * (1 - leader_p) / answered
        )
        return Poll(
            district=d_id, asked=n,
            answered=answered, shares=shares, margin=margin,
        )

    # ---Actions ---
    # Validate, deduct cost, apply citizen effects, return Result.

    def _reject(
        self, action: Action, state: CandidateState, reason: str,
    ) -> Result:
        """Build a rejection Result (no cost, no citizen effect)."""
        return Result(
            seat=action.seat, kind=action.kind, ok=False,
            cost_time=0, cost_money=0,
            effect=reason, money_after=state.money,
        )

    def apply(
        self,
        action: Action,
        cand_def: CandidateDef,
        state: CandidateState,
        pre_deducted: bool = False,
    ) -> Result:
        """Resolve one queued action. Mutates citizens and state."""
        kind = action.kind
        spec = ACTIONS[kind]
        seat = action.seat

        if not pre_deducted:
            # --- validation ---

            if kind == "interview" and state.interviewed:
                return self._reject(
                    action, state, "one interview per round",
                )

            if kind == "attack" and (
                action.target is None
                or action.target not in state.dirt_on
            ):
                return self._reject(
                    action, state, "no dirt on target",
                )
            if kind in ("attack", "dig") and action.target == seat:
                return self._reject(
                    action, state, "cannot target yourself",
                )

            if (
                state.time < spec["time"]
                or state.money < spec["money"]
            ):
                return self._reject(
                    action, state, "not enough time or money",
                )

            # --- deduct cost ---
            state.time -= spec["time"]
            state.money -= spec["money"]

        # --- dispatch ---
        stance = cand_def.stance
        stats = cand_def.stats
        effect = ""

        if kind == "promo":
            d = self.dist_defs[action.district]
            n = portion(d.citizens, REACH_FRAC)
            for cz in self.rng.sample(self.cit[action.district], n):
                cz.g[seat] += 0.15
                nudge(cz, stance)
            effect = f"{n} of {d.citizens} citizens reached"

        elif kind == "attack":
            d = self.dist_defs[action.district]
            n = portion(d.citizens, REACH_FRAC)
            scale = 1 + 0.5 * stats["attack"]
            backfired = 0
            for cz in self.rng.sample(self.cit[action.district], n):
                if self.rng.random() < BACKLASH:
                    cz.g[seat] -= 0.10
                    backfired += 1
                else:
                    cz.g[action.target] -= 0.15 * scale
            effect = (
                f"x{scale:.1f} strength, {backfired}/{n} backfired"
            )

        elif kind == "fundraise":
            gain = 15 + 5 * stats["funding"]
            state.money += gain
            effect = f"+${gain}"

        elif kind == "rally":
            gain = 0.05 * (1 + 0.25 * stats["speech"])
            hit = [action.district]
            if stats["ground"] >= 1:
                hit.append(neighbour(action.district, self.districts))
            reached = 0
            for d_id in hit:
                for cz in self.cit[d_id]:
                    if self.stance_dist(cz, seat) < SUPPORTER_DIST:
                        cz.t = min(T_MAX, cz.t + gain)
                        cz.g[seat] += 0.05
                        nudge(cz, stance)
                        reached += 1
            effect = f"{reached} supporters, turnout +{gain:.3f}"

        elif kind == "dig":
            chance = DIG_BASE + stats["attack"] / 12
            if self.rng.random() < chance:
                state.dirt_on.add(action.target)
                effect = f"FOUND dirt on seat {action.target}"
            else:
                effect = "found nothing"

        elif kind == "interview":
            state.interviewed = True
            gaffe = self.rng.random() < GAFFE
            for d in self.districts:
                n = portion(d.citizens, INTERVIEW_FRAC)
                for cz in self.rng.sample(self.cit[d.id], n):
                    if gaffe:
                        cz.g[seat] -= 0.10
                    else:
                        cz.g[seat] += 0.08
                        nudge(cz, stance, PERSUADE / 2)
            if gaffe:
                effect = "GAFFE: -0.10 goodwill"
            else:
                effect = "good: +0.08 goodwill"

        elif kind == "poll":
            d = self.dist_defs[action.district]
            n = portion(d.citizens, PRIVATE_POLL_FRAC)
            self._last_poll = self.sample_poll(action.district, n)
            effect = "results below"

        else:
            raise ValueError(f"unknown action kind '{kind}'")

        return Result(
            seat=seat, kind=kind, ok=True,
            cost_time=spec["time"], cost_money=spec["money"],
            effect=effect, money_after=state.money,
        )

    # --- round end ---
    # Decay goodwill; candidate state resets handled by server.

    def tick(self) -> None:
        """End-of-round: multiply every citizen's goodwill by DECAY."""

        for d_id in self.cit:
            for cz in self.cit[d_id]:
                for s in self.seats:
                    cz.g[s] *= DECAY

    # --- election ---
    # Expected votes; no extra randomness.

    def election(self) -> dict:
        """Compute election results .

        Returns {"votes": {d_id: {seat: float}},
                 "won": {seat: int}, "winner": seat}.
        Ties: district goes to lower seat; overall by total votes,
        then lower seat.
        """
        all_votes = {}
        won = {s: 0 for s in self.seats}
        for d in self.districts:
            v = self.votes(d.id)
            all_votes[d.id] = v
            # winner: highest votes, ties to lower seat
            best = max(v.values())
            winner = min(s for s in self.seats if v[s] == best)
            won[winner] += 1
        # overall: most districts, then total votes, then lower seat
        total = {
            s: sum(all_votes[d.id][s] for d in self.districts)
            for s in self.seats
        }
        winner = min(
            self.seats,
            key=lambda s: (-won[s], -total[s], s),
        )
        return {"votes": all_votes, "won": won, "winner": winner}


# --- truces ---
# Mutual truce burns dirt; processed after all actions resolve.

def resolve_truces(
    states: Dict[int, CandidateState],
) -> List[tuple]:
    """Process mutual truces. Returns list of (seat_a, seat_b) pairs."""
    seats = sorted(states)
    done = []
    for i, sa in enumerate(seats):
        for sb in seats[i + 1:]:
            a, b = states[sa], states[sb]
            if (
                sb in a.truce_with
                and sa in b.truce_with
                and sb in a.dirt_on
                and sa in b.dirt_on
            ):
                a.dirt_on.discard(sb)
                b.dirt_on.discard(sa)

                done.append((sa, sb))
    # clear all truce_with sets
    for st in states.values():
        st.truce_with = set()

    return done
