"""Winner selection, reimplemented from `services/crates/winner-selection` (`arbitrator.rs`).

Baselines come only from single-pair solutions, so they are exact. A pair with no
recorded score falls back to its surplus, a lower bound on score (fees >= 0), so the
fallback can only make the filter stricter; `PairComparison.proxied` marks those guesses.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, NamedTuple

from .primitives import MAX_WINNERS, Pair


@dataclass(frozen=True)
class Solution:
    """One bid, valued in native wei. `total` is score or surplus per the caller's mode;
    `pair_values` is always surplus, `pair_scores` always score."""

    solver: str
    """Lowercase hex without `0x`."""

    solution_uid: int
    """`proposed_solutions.uid`, assigned best-to-worst."""

    total: int

    pair_values: dict[Pair, int] = field(default_factory=dict[Pair, int])
    """Keyed by raw directed pair. Empty: `arbitrate` drops the solution."""

    pair_scores: dict[Pair, int] = field(default_factory=dict[Pair, int])
    """Partial: only pairs whose score is known."""

    score_anchor_failed: bool = False
    """Recomputed fees did not match the recorded score, so `pair_scores` was discarded."""

    order_uids: frozenset[str] = frozenset()

    winner_pairs: frozenset[Pair] = frozenset()
    """`pair_values` pairs, `as_erc20`-normalised. JIT orders claim none (services#4902)."""


FilterScale = Literal["score", "surplus"]


@dataclass(frozen=True)
class Ranking:
    """Result of `arbitrate` (`arbitrator.rs:677`)."""

    ranked: tuple[Solution, ...]
    """Winners first, then descending total: the order `compute_reference_scores` walks."""

    filtered_out: tuple[Solution, ...]

    winner_uids: frozenset[int]

    baselines: dict[Pair, int] = field(default_factory=dict[Pair, int])

    proxied_uids: frozenset[int] = frozenset()
    """Solutions whose fairness verdict rested on the surplus fallback (a guess)."""

    @property
    def winners(self) -> tuple[Solution, ...]:
        return tuple(s for s in self.ranked if s.solution_uid in self.winner_uids)

    @property
    def tested(self) -> tuple[Solution, ...]:
        """Kept plus filtered; excludes solutions with no auction order (services#4753)."""
        return self.ranked + self.filtered_out

    @property
    def winning_score(self) -> int:
        return sum(s.total for s in self.winners)


def compute_baseline_scores(solutions: Iterable[Solution]) -> dict[Pair, int]:
    """Best single-pair `total` per directed pair (`arbitrator.rs:650`)."""
    baselines: dict[Pair, int] = {}
    for solution in solutions:
        if len(solution.pair_values) != 1:
            continue
        ((pair, _),) = solution.pair_values.items()
        baselines[pair] = max(baselines.get(pair, 0), solution.total)
    return baselines


class PairComparison(NamedTuple):
    pair: Pair
    value: int
    baseline: int
    exact: bool
    """False: `value` is surplus standing in as a lower bound for score."""

    @property
    def fair(self) -> bool:
        return self.value >= self.baseline

    @property
    def proxied(self) -> bool:
        """An inexact value that fails; one that clears is still a proof of fairness."""
        return not self.exact and not self.fair


def filter_comparisons(
    solution: Solution, baselines: dict[Pair, int]
) -> Iterator[PairComparison]:
    for pair, surplus in solution.pair_values.items():
        score = solution.pair_scores.get(pair)
        baseline = baselines.get(pair, 0)
        if score is not None:
            yield PairComparison(pair, score, baseline, exact=True)
        else:
            yield PairComparison(pair, surplus, baseline, exact=False)


def filter_proxied(solution: Solution, baselines: dict[Pair, int]) -> bool:
    return any(c.proxied for c in filter_comparisons(solution, baselines))


def filter_scale(solution: Solution, baselines: dict[Pair, int]) -> FilterScale:
    return "surplus" if filter_proxied(solution, baselines) else "score"


def is_fair(solution: Solution, baselines: dict[Pair, int]) -> bool:
    """`arbitrator.rs:93`. Single-pair solutions are always kept."""
    if len(solution.pair_values) == 1:
        return True
    return all(c.fair for c in filter_comparisons(solution, baselines))


def filter_guess_could_matter(
    ranking: Ranking, max_winners: int = MAX_WINNERS
) -> bool:
    """Could restoring every guessed (proxied) filter verdict change the winner set?

    False is a proof: if restoring all leaves the winners alone, no subset can move them.
    Reference scores are not covered.
    """
    if not ranking.proxied_uids:
        return False

    filtered = {s.solution_uid for s in ranking.filtered_out}
    restored = [
        s
        for s in ranking.tested
        if s.solution_uid not in filtered or s.solution_uid in ranking.proxied_uids
    ]
    # uid order is the arbitrator's tie-break; `ranked` is winners-first, not score order.
    restored.sort(key=lambda s: (-s.total, s.solution_uid))
    winners = frozenset(
        restored[i].solution_uid for i in pick_winners(restored, max_winners)
    )
    return winners != ranking.winner_uids


def pick_winners(
    solutions: Sequence[Solution], max_winners: int = MAX_WINNERS
) -> set[int]:
    """Indices of winners (`arbitrator.rs:570`); `solutions` must be total-descending."""
    claimed: set[Pair] = set()
    winners: set[int] = set()

    for index, solution in enumerate(solutions):
        # Checked before the disjointness test, as in the Rust.
        if len(winners) >= max_winners:
            return winners
        if solution.winner_pairs.isdisjoint(claimed):
            winners.add(index)
            claimed |= solution.winner_pairs

    return winners


def _rank(kept: Sequence[Solution], max_winners: int) -> tuple[tuple[Solution, ...], frozenset[int]]:
    """Reorder winners-first, as `arbitrate` does: `compute_reference_scores` depends on it."""
    winner_indices = pick_winners(kept, max_winners)
    order = sorted(range(len(kept)), key=lambda i: i not in winner_indices)
    ranked = tuple(kept[i] for i in order)
    winner_uids = frozenset(kept[i].solution_uid for i in winner_indices)
    return ranked, winner_uids


def arbitrate(
    solutions: Iterable[Solution], max_winners: int = MAX_WINNERS
) -> Ranking:
    """Steps 2-5 of the mechanism (`arbitrator.rs:38`); valuation is the caller's.

    Ties in `total` break by input order: feed solutions in uid order, the arbitrator's
    tie-break.
    """
    all_solutions = list(solutions)
    baselines = compute_baseline_scores(all_solutions)

    scored = [s for s in all_solutions if s.pair_values]
    scored.sort(key=lambda s: -s.total)

    kept: list[Solution] = []
    discarded: list[Solution] = []
    proxied: set[int] = set()
    for solution in scored:
        (kept if is_fair(solution, baselines) else discarded).append(solution)
        if len(solution.pair_values) > 1 and filter_proxied(solution, baselines):
            proxied.add(solution.solution_uid)

    ranked, winner_uids = _rank(kept, max_winners)

    return Ranking(
        ranked=ranked,
        filtered_out=tuple(discarded),
        winner_uids=winner_uids,
        baselines=baselines,
        proxied_uids=frozenset(proxied),
    )


class ReferenceScore(NamedTuple):
    score: int
    setters: frozenset[str]
    """Solvers whose solutions make up the score."""


def compute_reference_outcomes(
    ranking: Ranking, max_winners: int = MAX_WINNERS
) -> dict[str, ReferenceScore]:
    """Reference score per winning solver (`arbitrator.rs:607`), plus its setters."""
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
    return {
        solver: ref.score
        for solver, ref in compute_reference_outcomes(ranking, max_winners).items()
    }
