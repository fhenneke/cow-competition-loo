"""Per-auction solver rewards: uncapped exactly, capped as an estimate.

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

**Uncapped is the exact quantity; capped is an estimate.** The real payout is
`0 if the auction is excluded else clamp(uncapped, lower_reward_cap, Σ upper_reward_cap)`.
The lower cap is a per-chain constant and the upper cap is `scaling_factor × realised
protocol fees` of each winning solution — realised, so a batch that never settled has an
upper cap of 0, and a *counterfactual* winner that never won has no cap of its own at
all — see docs/rewards.md#why-the-cap-is-hard-counterfactually. The counterfactual
therefore hands a replacement the cap of the recorded winner(s) whose slot it took
(D4's logic again, and self-consistent with it: a reverted slot has no fees, so its
inherited cap is 0 exactly where its inherited settlement is a revert). `Win.upper_cap
= None` marks a winner whose cap could not even be estimated; the whole solver's
`capped_reward` is then `None` rather than a guess.

Measured over 2026-08-01..04, the caps are not a refinement: 9,809 reward rows sum to
−410 ETH uncapped and 0.75 ETH capped, with 54% of rows clamped at the upper cap. An
uncapped number must never be read as a payout.

Pure module: no DB handle. Integer arithmetic for the uncapped path; the caps are
`Decimal` because the DB's cap is `scaling_factor × fees` and genuinely fractional.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import NamedTuple

# The caps arrive from Postgres `numeric` with ~40 significant digits and are summed
# per solver; Python's default 28-digit context silently rounds those sums, which the
# exact comparison in `validate-rewards` then flags on every capped row. 78 digits
# covers a full uint256, so every sum in this pipeline stays exact.
getcontext().prec = 78


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
    upper_cap: Decimal | None = None
    """This solution's contribution to its solver's upper reward cap. The recorded
    value for a recorded winner, the displaced slot's value for a replacement, and
    `None` when there was nothing to inherit — which poisons the solver's
    `capped_reward` to `None` rather than fabricating a cap."""


@dataclass(frozen=True)
class SolverReward:
    """One solver's reward in one auction, with the quantities that produced it."""

    solver: str
    competition_score: int
    observed_score: int
    reference_score: int
    uncapped_reward: int
    upper_cap: Decimal | None = None
    """Σ of the solver's winning solutions' cap contributions; `None` when any of
    them was unknowable, or when no caps were supplied at all."""
    capped_reward: Decimal | None = None
    """`0` if the auction is excluded, else `clamp(uncapped, lower, upper_cap)`.
    Exact for recorded winners, an estimate wherever `cap_inherited`."""
    cap_inherited: bool = False
    """True when any of the caps behind `upper_cap` came from a displaced slot rather
    than this solution's own record — i.e. `capped_reward` is an estimate."""


def uncapped_rewards(
    wins: Iterable[Win],
    reference_scores: Mapping[str, int],
    *,
    lower_cap: int | None = None,
    excluded: bool = False,
    caps_inherited: frozenset[str] = frozenset(),
) -> dict[str, SolverReward]:
    """Reward per winning solver of one auction — uncapped always, capped when caps
    are supplied.

    `reference_scores` must cover every winning solver; on the recorded side that is
    guaranteed by the DB (reference scores are stored exactly for winners), and on the
    recomputed side by `compute_reference_outcomes`, which produces one entry per
    winning solver.

    The capped side is computed only when `lower_cap` is given: each solver's upper
    cap is the sum of its wins' `upper_cap` contributions (`None` if any contribution
    is `None`), and `caps_inherited` names the solvers whose caps are estimates.
    """
    wins = list(wins)
    winning_score = sum(win.score for win in wins)

    competition: dict[str, int] = {}
    observed: dict[str, int] = {}
    upper: dict[str, Decimal | None] = {}
    for win in wins:
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
        capped: Decimal | None = None
        if lower_cap is not None and upper_cap is not None:
            clamped = min(max(Decimal(uncapped), Decimal(lower_cap)), upper_cap)
            capped = Decimal(0) if excluded else clamped

        rewards[solver] = SolverReward(
            solver=solver,
            competition_score=competition_score,
            observed_score=observed[solver],
            reference_score=reference_score,
            uncapped_reward=uncapped,
            upper_cap=upper_cap,
            capped_reward=capped,
            cap_inherited=solver in caps_inherited,
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

    COMPARED_FIELDS = (
        "competition_score",
        "observed_score",
        "reference_score",
        "uncapped_reward",
        "upper_cap",
        "capped_reward",
    )

    @property
    def differing_fields(self) -> tuple[str, ...]:
        if self.ours is None or self.theirs is None:
            return ("missing",)
        return tuple(
            name
            for name in self.COMPARED_FIELDS
            if getattr(self.ours, name) != getattr(self.theirs, name)
        )


@dataclass
class RewardValidation:
    """Window aggregate of the rewards gate: recorded inputs -> formula -> fct comparison.

    Unlike the competition validation there is no approximation anywhere in this path — the inputs are the
    DB's own winning solutions, settlement flags and reference scores — so the gate is
    absolute: every row must match exactly, and any mismatch is a bug in the formula
    transcription or a data problem to be understood, not a rate to be reported.
    """

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
