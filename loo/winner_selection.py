"""Winner selection, reimplemented from `services/crates/winner-selection`.

Pure: plain dataclasses in, plain dataclasses out, no DB handle and no floats. The
caller supplies each solution's `total` and `pair_values` — see PLAN.md §2 for why the
valuation is an input rather than something this module derives.

Everything here mirrors `arbitrator.rs`; deviations are called out in comments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, NamedTuple, Sequence

from .primitives import MAX_WINNERS, Pair


@dataclass(frozen=True)
class Solution:
    """One bid, already valued.

    `total` and `pair_values` are in native wei and carry either score or user surplus
    depending on the mode the caller ran the valuation in.
    """

    solver: str
    """Solver submission address, lowercase hex without `0x`."""

    solution_uid: int
    """`proposed_solutions.uid` — a per-auction index, assigned best-to-worst."""

    total: int
    """Solution value. In the Rust this is always `sum(pair_values.values())`; in score
    mode we take it from `proposed_solutions.score` instead, so the two can differ."""

    pair_values: dict[Pair, int] = field(default_factory=dict)
    """Value per **raw** directed token pair, over score-contributing orders only."""

    order_uids: frozenset[str] = frozenset()
    """All orders traded by the solution, contributing to score or not."""

    winner_pairs: frozenset[Pair] = frozenset()
    """Directed pairs claimed in `pick_winners`, `as_erc20`-normalised and covering
    **all** orders — not just the score-contributing ones (`arbitrator.rs:587`)."""


@dataclass(frozen=True)
class Ranking:
    """Result of `arbitrate`, mirroring `arbitrator.rs:677`."""

    ranked: tuple[Solution, ...]
    """Solutions that survived the fairness filter, ordered winners-first and then by
    descending total — the exact order `compute_reference_scores` walks."""

    filtered_out: tuple[Solution, ...]
    """Solutions the fairness filter discarded."""

    winner_uids: frozenset[int]
    """`solution_uid` of every winner."""

    baselines: dict[Pair, int] = field(default_factory=dict)
    """Per-pair baseline used by the filter. Kept for diagnostics."""

    dropped_uids: frozenset[int] = frozenset()
    """Solutions dropped before ranking because their total was 0
    (`arbitrator.rs:72`). These never reach the DB."""

    @property
    def winners(self) -> tuple[Solution, ...]:
        return tuple(s for s in self.ranked if s.solution_uid in self.winner_uids)

    @property
    def winning_score(self) -> int:
        return sum(s.total for s in self.winners)


def compute_baseline_scores(solutions: Iterable[Solution]) -> dict[Pair, int]:
    """Best single-pair total per directed pair (`arbitrator.rs:650`).

    Only solutions touching *exactly one* pair are baseline candidates. Note this runs
    over the solutions as they were *scored*, before zero-total ones are dropped —
    `scores_by_solution` is never pruned in the Rust. A zero-total solution can only
    ever install a baseline of 0, which the filter comparison passes trivially, so the
    distinction is behaviourally inert; it is preserved anyway.
    """
    baselines: dict[Pair, int] = {}
    for solution in solutions:
        if len(solution.pair_values) != 1:
            continue
        ((pair, value),) = solution.pair_values.items()
        baselines[pair] = max(baselines.get(pair, 0), value)
    return baselines


def is_fair(solution: Solution, baselines: dict[Pair, int]) -> bool:
    """The fairness test of `arbitrator.rs:93`.

    A solution touching a single pair is always kept — the exemption exists so that
    reference scores do not collapse to 0. Otherwise every pair it touches must be at
    least as valuable as the best solution that trades only that pair.
    """
    if len(solution.pair_values) == 1:
        return True
    return all(
        value >= baselines.get(pair, 0) for pair, value in solution.pair_values.items()
    )


def pick_winners(
    solutions: Sequence[Solution], max_winners: int = MAX_WINNERS
) -> set[int]:
    """Indices of winning solutions (`arbitrator.rs:570`).

    Assumes `solutions` is ordered by descending total. Walks it greedily, taking a
    solution whose directed pairs are disjoint from everything already claimed, which
    is what enforces a uniform *directional* clearing price across winners.
    """
    claimed: set[Pair] = set()
    winners: set[int] = set()

    for index, solution in enumerate(solutions):
        # Checked before the disjointness test, exactly as in the Rust.
        if len(winners) >= max_winners:
            return winners
        if solution.winner_pairs.isdisjoint(claimed):
            winners.add(index)
            claimed |= solution.winner_pairs

    return winners


def _rank(kept: Sequence[Solution], max_winners: int) -> tuple[tuple[Solution, ...], frozenset[int]]:
    """Pick winners on an already total-descending `kept`, then reorder as the Rust does.

    `arbitrate` finishes with `sort_by_key(|s| (Reverse(is_winner), Reverse(score)))`,
    so `ranked` is *not* plain score order: a non-winner may outscore a later winner and
    still sort behind it. `compute_reference_scores` re-runs `pick_winners` on this
    reordered list, so the reordering is load-bearing and must be reproduced.
    """
    winner_indices = pick_winners(kept, max_winners)
    # Stable sort on winner-first alone: `kept` is already total-descending, so sorting
    # by total again would be a no-op. Ordering by index rather than by uid keeps this
    # correct even if a caller hands us solutions with colliding uids.
    order = sorted(range(len(kept)), key=lambda i: i not in winner_indices)
    ranked = tuple(kept[i] for i in order)
    winner_uids = frozenset(kept[i].solution_uid for i in winner_indices)
    return ranked, winner_uids


def arbitrate(
    solutions: Iterable[Solution], max_winners: int = MAX_WINNERS
) -> Ranking:
    """Steps 1-5 of the mechanism (`arbitrator.rs:38`).

    Valuation (step 1) has already happened; this is drop-zero, sort, baselines,
    fairness filter, pick winners.

    Ties in `total` are broken by input order, so feeding solutions in `uid` order
    reproduces the ordering the autopilot recorded (uids are assigned best-to-worst).
    The autopilot itself shuffles before sorting, so the true tie-break is not
    recoverable from any other source.
    """
    all_solutions = list(solutions)
    baselines = compute_baseline_scores(all_solutions)

    dropped = frozenset(s.solution_uid for s in all_solutions if s.total == 0)
    scored = [s for s in all_solutions if s.total != 0]
    scored.sort(key=lambda s: -s.total)

    kept: list[Solution] = []
    discarded: list[Solution] = []
    for solution in scored:
        (kept if is_fair(solution, baselines) else discarded).append(solution)

    ranked, winner_uids = _rank(kept, max_winners)

    return Ranking(
        ranked=ranked,
        filtered_out=tuple(discarded),
        winner_uids=winner_uids,
        baselines=baselines,
        dropped_uids=dropped,
    )


class ReferenceScore(NamedTuple):
    """One winning solver's reference score, and who supplied it."""

    score: int
    setters: frozenset[str]
    """Solvers whose solutions make up the score — i.e. whose removal moves it."""


def compute_reference_outcomes(
    ranking: Ranking, max_winners: int = MAX_WINNERS
) -> dict[str, ReferenceScore]:
    """Reference score per winning solver (`arbitrator.rs:607`), with its setters.

    For each winning solver, re-pick winners over `ranking.ranked` with **all** of that
    solver's solutions removed; the reference score is the sum of those winners' totals.
    Walks `ranked` in its stored order — see `_rank` for why that order matters.

    The Rust returns only the scores. The setters ride along because the counterfactual
    needs "whose reward moves when a solver disappears", and answering it separately
    would mean a second copy of this walk re-doing every pick.
    """
    outcomes: dict[str, ReferenceScore] = {}

    for solution in ranking.ranked:
        solver = solution.solver
        if len(outcomes) >= max_winners:
            return outcomes
        if solver in outcomes:
            continue
        if solution.solution_uid not in ranking.winner_uids:
            continue

        without_solver = [s for s in ranking.ranked if s.solver != solver]
        winner_indices = pick_winners(without_solver, max_winners)
        outcomes[solver] = ReferenceScore(
            score=sum(without_solver[i].total for i in winner_indices),
            setters=frozenset(without_solver[i].solver for i in winner_indices),
        )

    return outcomes


def compute_reference_scores(
    ranking: Ranking, max_winners: int = MAX_WINNERS
) -> dict[str, int]:
    """Just the scores of `compute_reference_outcomes` — the shape the Rust returns."""
    return {
        solver: ref.score
        for solver, ref in compute_reference_outcomes(ranking, max_winners).items()
    }
