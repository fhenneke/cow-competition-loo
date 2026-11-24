"""Per-auction solver rewards, in native wei, from `fct_solver_rewards_per_auction.sql`.

    uncapped(s) = winning_score - competition_score(s) + observed_score(s)
                  - min(winning_score, reference_score(s))
    capped(s)   = 0 if excluded else clamp(uncapped, lower_cap, Σ upper_cap)

Uncapped is exact; never read it as a payout. Capped is an estimate wherever a
counterfactual winner inherits the displaced slot's caps
(docs/rewards.md#why-the-cap-is-hard-counterfactually).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import NamedTuple

# Caps are ~40-digit numerics; the default 28-digit context rounds their sums.
getcontext().prec = 78


class MissingReferenceScoreError(Exception):
    """A winning solver has no reference score; 0 would inflate its reward."""


class Win(NamedTuple):
    solver: str
    score: int
    settled: bool
    upper_cap: Decimal | None = None
    """None = unknown; makes the solver's `capped_reward` None."""
    penalty_cap: Decimal | None = None
    """CIP-87. None = no cap (flat floor applies), not unknown; 0 is a real cap."""


@dataclass(frozen=True)
class SolverReward:
    solver: str
    competition_score: int
    observed_score: int
    reference_score: int
    uncapped_reward: int
    upper_cap: Decimal | None = None
    lower_cap: Decimal | None = None
    capped_reward: Decimal | None = None


def lower_reward_cap(wins: Iterable[Win], flat: int) -> Decimal:
    """Solver's reward floor: −Σ penalty_cap of unsettled wins if every win has a cap,
    else `flat` (the solution_data column, which is not the CIP-87 floor)."""
    if flat == 0:
        return Decimal(0)
    floor = Decimal(0)
    for win in wins:
        if win.penalty_cap is None:
            return Decimal(flat)
        if not win.settled:
            floor -= win.penalty_cap
    return floor


def uncapped_rewards(
    wins: Iterable[Win],
    reference_scores: Mapping[str, int],
    *,
    lower_cap: int | None = None,
    excluded: bool = False,
) -> dict[str, SolverReward]:
    """Reward per winning solver; capped fields only when `lower_cap` (flat floor) is given."""
    wins = list(wins)
    winning_score = sum(win.score for win in wins)

    by_solver: dict[str, list[Win]] = {}
    competition: dict[str, int] = {}
    observed: dict[str, int] = {}
    upper: dict[str, Decimal | None] = {}
    for win in wins:
        by_solver.setdefault(win.solver, []).append(win)
        competition[win.solver] = competition.get(win.solver, 0) + win.score
        observed[win.solver] = observed.get(win.solver, 0) + (
            win.score if win.settled else 0
        )
        if win.solver not in upper:
            upper[win.solver] = Decimal(0)
        current = upper[win.solver]
        if win.upper_cap is None:
            upper[win.solver] = None
        elif current is not None:
            upper[win.solver] = current + win.upper_cap

    rewards: dict[str, SolverReward] = {}
    for solver, competition_score in competition.items():
        if solver not in reference_scores:
            raise MissingReferenceScoreError(
                f"solver {solver} won but has no reference score; refusing to "
                f"substitute 0, which would inflate its reward"
            )
        reference_score = reference_scores[solver]
        uncapped = (
            winning_score
            - competition_score
            + observed[solver]
            - min(winning_score, reference_score)
        )

        upper_cap = upper[solver] if lower_cap is not None else None
        floor = (
            lower_reward_cap(by_solver[solver], lower_cap)
            if lower_cap is not None
            else None
        )
        capped: Decimal | None = None
        if floor is not None and upper_cap is not None:
            clamped = min(max(Decimal(uncapped), floor), upper_cap)
            capped = Decimal(0) if excluded else clamped

        rewards[solver] = SolverReward(
            solver=solver,
            competition_score=competition_score,
            observed_score=observed[solver],
            reference_score=reference_score,
            uncapped_reward=uncapped,
            upper_cap=upper_cap,
            lower_cap=floor,
            capped_reward=capped,
        )
    return rewards


@dataclass(frozen=True)
class RewardMismatch:
    auction_id: int
    solver: str
    ours: SolverReward | None
    theirs: SolverReward | None

    COMPARED_FIELDS = (
        "competition_score",
        "observed_score",
        "reference_score",
        "uncapped_reward",
        "upper_cap",
        "lower_cap",
        "capped_reward",
    )

    @property
    def differing_fields(self) -> tuple[str, ...]:
        return differing_fields(self.ours, self.theirs)


def differing_fields(
    ours: SolverReward | None, theirs: SolverReward | None
) -> tuple[str, ...]:
    if ours is None or theirs is None:
        return ("missing",)
    return tuple(
        name
        for name in RewardMismatch.COMPARED_FIELDS
        if getattr(ours, name) != getattr(theirs, name)
    )


@dataclass
class RewardValidation:
    """Rewards gate over recorded inputs; exact, so any mismatch is a bug."""

    auctions: int = 0
    auctions_with_winners: int = 0
    rows: int = 0
    rows_matched: int = 0
    auctions_missing_from_fct: list[int] = field(default_factory=list[int])
    mismatches: list[RewardMismatch] = field(default_factory=list[RewardMismatch])

    def check_auction(
        self,
        auction_id: int,
        ours: Mapping[str, SolverReward],
        theirs: Mapping[str, SolverReward],
    ) -> None:
        self.auctions += 1
        if not ours and not theirs:
            return
        self.auctions_with_winners += 1

        if ours and not theirs:
            # Mart coverage gap (incremental model), not a formula mismatch.
            self.auctions_missing_from_fct.append(auction_id)
            return

        for solver in sorted(set(ours) | set(theirs)):
            self.rows += 1
            mine, its = ours.get(solver), theirs.get(solver)
            if not differing_fields(mine, its):
                self.rows_matched += 1
            else:
                self.mismatches.append(
                    RewardMismatch(
                        auction_id=auction_id, solver=solver, ours=mine, theirs=its
                    )
                )

    @property
    def gate_met(self) -> bool:
        return not self.mismatches and not self.auctions_missing_from_fct
