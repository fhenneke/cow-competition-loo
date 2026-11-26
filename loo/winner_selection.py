"""Winner selection, reimplemented from `services/crates/winner-selection` (`arbitrator.rs`).

Baselines come only from single-pair solutions, so they are exact. A pair with no
recorded score falls back to its surplus, a lower bound on score (fees >= 0), so the
fallback can only make the filter stricter; `PairComparison.proxied` marks those guesses.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
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
    """`pair_values` pairs, `as_erc20`-normalised. JIT orders claim none."""


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

    @cached_property
    def winners(self) -> tuple[Solution, ...]:
        return tuple(s for s in self.ranked if s.solution_uid in self.winner_uids)

    @property
    def tested(self) -> tuple[Solution, ...]:
        """Kept plus filtered; excludes solutions with no auction order."""
        return self.ranked + self.filtered_out

    @cached_property
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


def score_order(solutions: Iterable[Solution]) -> list[Solution]:
    """The solutions `arbitrate` judges, total-descending; ties keep input order."""
    scored = [s for s in solutions if s.pair_values]
    scored.sort(key=lambda s: -s.total)
    return scored


def arbitrate(
    solutions: Iterable[Solution], max_winners: int = MAX_WINNERS
) -> Ranking:
    """Steps 2-5 of the mechanism (`arbitrator.rs:38`); valuation is the caller's.

    Ties in `total` break by input order: feed solutions in uid order, the arbitrator's
    tie-break.
    """
    all_solutions = list(solutions)
    baselines = compute_baseline_scores(all_solutions)

    kept: list[Solution] = []
    discarded: list[Solution] = []
    proxied: set[int] = set()
    for solution in score_order(all_solutions):
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
    setter_uids: frozenset[int]
    """The solutions the without-solver pick takes; their totals make up the score."""


def compute_reference_outcomes(
    ranking: Ranking, max_winners: int = MAX_WINNERS
) -> dict[str, ReferenceScore]:
    """Reference score per winning solver (`arbitrator.rs:607`), plus the solutions
    setting it."""
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
            setter_uids=frozenset(without_solver[i].solution_uid for i in winner_indices),
        )

    return outcomes


def compute_reference_scores(
    ranking: Ranking, max_winners: int = MAX_WINNERS
) -> dict[str, int]:
    return {
        solver: ref.score
        for solver, ref in compute_reference_outcomes(ranking, max_winners).items()
    }


class Rearbitrated(NamedTuple):
    ranking: Ranking
    reference_scores: dict[str, int]
    verdicts_moved: bool
    """A fairness verdict changed; if not, the kept set is the baseline's minus the removal."""


class Arbitration:
    """`arbitrate` of one auction's solutions, kept to re-run cheaply without some solvers.

    `without(removed)` equals `arbitrate` of the other solvers' solutions, and its
    reference scores `compute_reference_scores` of that ranking. Two facts make it cheap:

    - A fairness verdict depends only on the solution and its own pairs' baselines, so a
      verdict stands unless the removal lowers one of those baselines.
    - The greedy pick only ever skips a solution it does not take, so dropping solutions
      it skipped leaves the pick as it was. With the verdicts standing and no removed
      solution among the winners, the winners and the ranking are the baseline's minus
      the removed solutions, and a reference pick that took no removed solution stands.
    """

    def __init__(self, solutions: Iterable[Solution], max_winners: int = MAX_WINNERS) -> None:
        self.solutions = list(solutions)
        self.max_winners = max_winners
        self.ranking = arbitrate(self.solutions, max_winners)
        self._scored = score_order(self.solutions)
        self._filtered = frozenset(s.solution_uid for s in self.ranking.filtered_out)
        self._kept = [s for s in self._scored if s.solution_uid not in self._filtered]
        self._solver_of = {s.solution_uid: s.solver for s in self.solutions}
        # Single-pair solutions, which alone set baselines: their totals per pair, and
        # the pairs each solver bids on alone.
        self._single_pair: dict[Pair, list[tuple[str, int]]] = {}
        self._single_pairs_of: dict[str, set[Pair]] = {}
        for s in self.solutions:
            if len(s.pair_values) == 1:
                (pair,) = s.pair_values
                self._single_pair.setdefault(pair, []).append((s.solver, s.total))
                self._single_pairs_of.setdefault(s.solver, set()).add(pair)
        self._winning_solvers = frozenset(s.solver for s in self.ranking.winners)
        # A positive baseline falls exactly when every solver holding it is removed.
        self._holders: dict[Pair, frozenset[str]] = {
            pair: frozenset(solver for solver, total in bids if total == value)
            for pair, bids in self._single_pair.items()
            if (value := self.ranking.baselines[pair]) > 0
        }

    @cached_property
    def references(self) -> dict[str, ReferenceScore]:
        return compute_reference_outcomes(self.ranking, self.max_winners)

    @cached_property
    def reference_scores(self) -> dict[str, int]:
        return {solver: ref.score for solver, ref in self.references.items()}

    @cached_property
    def _setting_solvers(self) -> frozenset[str]:
        """Solvers with a solution in some reference pick."""
        return frozenset(
            self._solver_of[uid] for ref in self.references.values() for uid in ref.setter_uids
        )

    def _touched(self, removed: frozenset[str]) -> set[Pair]:
        """Pairs whose baseline a removed solver's single-pair solution may set."""
        touched = [self._single_pairs_of[s] for s in removed if s in self._single_pairs_of]
        return set[Pair]().union(*touched)

    def _rest_baseline(self, pair: Pair, removed: frozenset[str]) -> int | None:
        """`pair`'s baseline without `removed`; None when no single-pair solution is left."""
        rest = [total for solver, total in self._single_pair[pair] if solver not in removed]
        return max(0, *rest) if rest else None

    def unmoved_by(self, removed: frozenset[str]) -> bool:
        """Does removing `removed` move nothing? True when no removed solution wins, sets
        a reference score or sets a baseline: `without` then keeps every verdict, the
        winners and every reference pick, and only drops the removed solutions."""
        if not (
            removed.isdisjoint(self._winning_solvers)
            and removed.isdisjoint(self._setting_solvers)
        ):
            return False
        holders = self._holders
        return not any(
            pair in holders and holders[pair] <= removed
            for solver in removed
            for pair in self._single_pairs_of.get(solver, ())
        )

    def without(self, removed: frozenset[str]) -> Rearbitrated:
        """The ranking without every solution of `removed`, and its reference scores."""
        baseline = self.ranking
        baselines = baseline.baselines
        lowered: set[Pair] = set()
        touched = self._touched(removed)
        if touched:
            # `compute_baseline_scores` of the rest, recomputed only where it can differ.
            baselines = dict(baselines)
            for pair in touched:
                rest = self._rest_baseline(pair, removed)
                if rest is None:
                    del baselines[pair]
                else:
                    baselines[pair] = rest
                if baselines.get(pair, 0) != baseline.baselines[pair]:
                    lowered.add(pair)

        verdicts_moved = False
        if lowered:
            kept: list[Solution] = []
            discarded: list[Solution] = []
            proxied: set[int] = set()
            for solution in self._scored:
                if solution.solver in removed:
                    continue
                uid = solution.solution_uid
                if len(solution.pair_values) > 1 and not lowered.isdisjoint(solution.pair_values):
                    fair = is_fair(solution, baselines)
                    guessed = filter_proxied(solution, baselines)
                    verdicts_moved = verdicts_moved or fair == (uid in self._filtered)
                else:
                    fair = uid not in self._filtered
                    guessed = uid in baseline.proxied_uids
                (kept if fair else discarded).append(solution)
                if guessed:
                    proxied.add(uid)
            filtered_out = tuple(discarded)
            proxied_uids = frozenset(proxied)
        else:
            kept = [s for s in self._kept if s.solver not in removed]
            filtered_out = tuple(s for s in baseline.filtered_out if s.solver not in removed)
            proxied_uids = (
                frozenset(u for u in baseline.proxied_uids if self._solver_of[u] not in removed)
                if baseline.proxied_uids
                else baseline.proxied_uids
            )

        unchanged = not verdicts_moved and removed.isdisjoint(self._winning_solvers)
        if unchanged:
            ranked = tuple(s for s in baseline.ranked if s.solver not in removed)
            winner_uids = baseline.winner_uids
        else:
            ranked, winner_uids = _rank(kept, self.max_winners)
        ranking = Ranking(
            ranked=ranked,
            filtered_out=filtered_out,
            winner_uids=winner_uids,
            baselines=baselines,
            proxied_uids=proxied_uids,
        )
        if not unchanged:
            return Rearbitrated(
                ranking, compute_reference_scores(ranking, self.max_winners), verdicts_moved
            )

        if removed.isdisjoint(self._setting_solvers):
            return Rearbitrated(ranking, self.reference_scores, False)
        # Same winners in the same order, so the same solvers get a reference score.
        scores: dict[str, int] = {}
        for solver, reference in self.references.items():
            if any(self._solver_of[uid] in removed for uid in reference.setter_uids):
                without_solver = [s for s in ranked if s.solver != solver]
                picked = pick_winners(without_solver, self.max_winners)
                scores[solver] = sum(without_solver[i].total for i in picked)
            else:
                scores[solver] = reference.score
        return Rearbitrated(ranking, scores, verdicts_moved)
