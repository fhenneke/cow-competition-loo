"""M3: per-auction solver rewards, uncapped.

Transcribed from `fct_solver_rewards_per_auction.sql` (see docs/rewards.md for the
formula reference and the dbt source paths). Everything is per auction, per solver,
summed over that solver's *winning* solutions, in native wei:

    winning_score        = sum of score over all winning solutions in the auction
    competition_score(s) = sum of score over s's winning solutions
    observed_score(s)    = the settled-in-time part of competition_score(s)

    uncapped_reward(s)   = winning_score
                           - competition_score(s)
                           + observed_score(s)
                           - min(winning_score, reference_score(s))

When s settles in time this reduces to `winning_score - min(winning_score,
reference_score)` — the marginal value s added to the auction. When it does not,
`observed_score` is 0 and the reward is a penalty.

**Uncapped only, deliberately.** The real payout is clamped into
`[lower_reward_cap, upper_reward_cap]`, but the upper cap is derived from the realised
protocol fees of the settled batch, and a counterfactual winner that never settled has
none — see docs/rewards.md#why-the-cap-is-hard-counterfactually. PLAN.md §6 names
uncapped as a legitimate stopping point; anything consuming these numbers must say so
too, because a failed settlement's uncapped penalty (`-reference_score`) is orders of
magnitude below the real clamped payout (-0.01 ETH on mainnet).

Pure module: no DB handle, integer arithmetic only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, NamedTuple


class MissingReferenceScoreError(Exception):
    """A winning solver has no reference score.

    Loud rather than defaulted: substituting 0 would add `min(winning_score, 0) = 0`
    where a real subtraction belongs, silently inflating that solver's reward by up to
    the whole winning score.
    """


class Win(NamedTuple):
    """One winning solution, reduced to what the reward formula consumes."""

    solver: str
    score: int
    settled: bool
    """Did this batch land in time? On the counterfactual side this is the outcome
    rule's decision rather than a record — see `counterfactual.OUTCOME_RULE`."""


@dataclass(frozen=True)
class SolverReward:
    """One solver's reward in one auction, with the quantities that produced it."""

    solver: str
    competition_score: int
    observed_score: int
    reference_score: int
    uncapped_reward: int


def uncapped_rewards(
    wins: Iterable[Win], reference_scores: Mapping[str, int]
) -> dict[str, SolverReward]:
    """Uncapped reward per winning solver of one auction.

    `reference_scores` must cover every winning solver; on the recorded side that is
    guaranteed by the DB (reference scores are stored exactly for winners), and on the
    recomputed side by `compute_reference_outcomes`, which produces one entry per
    winning solver.
    """
    wins = list(wins)
    winning_score = sum(win.score for win in wins)

    competition: dict[str, int] = {}
    observed: dict[str, int] = {}
    for win in wins:
        competition[win.solver] = competition.get(win.solver, 0) + win.score
        observed[win.solver] = observed.get(win.solver, 0) + (
            win.score if win.settled else 0
        )

    rewards: dict[str, SolverReward] = {}
    for solver, competition_score in competition.items():
        if solver not in reference_scores:
            raise MissingReferenceScoreError(
                f"solver {solver} won but has no reference score; refusing to "
                f"substitute 0, which would inflate its reward"
            )
        reference_score = reference_scores[solver]
        rewards[solver] = SolverReward(
            solver=solver,
            competition_score=competition_score,
            observed_score=observed[solver],
            reference_score=reference_score,
            uncapped_reward=(
                winning_score
                - competition_score
                + observed[solver]
                - min(winning_score, reference_score)
            ),
        )
    return rewards


@dataclass(frozen=True)
class RewardMismatch:
    """One (auction, solver) where our recomputation disagrees with the DB."""

    auction_id: int
    solver: str
    ours: SolverReward | None
    """None when the fct model has a row we cannot reproduce at all."""
    theirs: SolverReward | None
    """None when we compute a reward for a solver the fct model has no row for."""

    @property
    def differing_fields(self) -> tuple[str, ...]:
        if self.ours is None or self.theirs is None:
            return ("missing",)
        return tuple(
            name
            for name in (
                "competition_score",
                "observed_score",
                "reference_score",
                "uncapped_reward",
            )
            if getattr(self.ours, name) != getattr(self.theirs, name)
        )


@dataclass
class RewardValidation:
    """Window aggregate of the M3 gate: recorded inputs -> formula -> fct comparison.

    Unlike M1 there is no approximation anywhere in this path — the inputs are the
    DB's own winning solutions, settlement flags and reference scores — so the gate is
    absolute: every row must match exactly, and any mismatch is a bug in the formula
    transcription or a data problem to be understood, not a rate to be reported.
    """

    auctions: int = 0
    auctions_with_winners: int = 0
    rows: int = 0
    rows_matched: int = 0
    auctions_missing_from_fct: list[int] = field(default_factory=list)
    mismatches: list[RewardMismatch] = field(default_factory=list)

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
            # The whole auction is absent from the mart — a coverage gap (the model is
            # incremental), not a formula disagreement. Kept apart so the report can
            # say "narrow the window" rather than "you have a bug".
            self.auctions_missing_from_fct.append(auction_id)
            return

        for solver in sorted(set(ours) | set(theirs)):
            self.rows += 1
            mine, its = ours.get(solver), theirs.get(solver)
            if mine == its:
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
