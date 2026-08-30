"""Winner selection, reimplemented from `services/crates/winner-selection`.

Pure: plain dataclasses in, plain dataclasses out, no DB handle and no floats. The
caller supplies each solution's `total` and its per-pair decomposition — see PLAN.md
("Two valuations, deliberately separate") for why the valuation is an input rather than
something this module derives.

The fairness filter compares **one** quantity, the per-pair score, against **one** set of
baselines. Baselines are never approximated: only single-pair solutions set one, and such
a solution's whole value sits on its one pair, so the baseline is its recorded `total`.
A pair whose own score is unrecorded falls back to `pair_values` (user surplus), which is
a *lower bound* on that score since protocol fees are non-negative — so clearing the
baseline on surplus alone still proves the pair fair. Only the other direction is a guess,
and `PairComparison.proxied` marks exactly those.

Everything here mirrors `arbitrator.rs`; deviations are called out in comments.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, NamedTuple

from .primitives import MAX_WINNERS, Pair


@dataclass(frozen=True)
class Solution:
    """One bid, already valued.

    Everything here is in native wei. `total` carries score or user surplus depending on
    the mode the caller ran the valuation in; `pair_values` is always user surplus and
    `pair_scores` always score.
    """

    solver: str
    """Solver submission address, lowercase hex without `0x`."""

    solution_uid: int
    """`proposed_solutions.uid` — a per-auction index, assigned best-to-worst."""

    total: int
    """Solution value. In the Rust this is always the sum of the per-pair values; in
    score mode we take it from `proposed_solutions.score` instead. `pair_scores` sums
    back to it by construction, `pair_values` (surplus) does not — it is short by the
    solution's protocol fees.

    This is also the solution's baseline contribution: a single-pair solution puts its
    whole value on that pair, so `compute_baseline_scores` reads `total` and needs no
    decomposition at all."""

    pair_values: dict[Pair, int] = field(default_factory=dict[Pair, int])
    """Native **user surplus** per **raw** directed token pair, over score-contributing
    orders only. A lower bound on the pair's score, and what the filter falls back to for
    a pair whose score is unrecorded."""

    pair_scores: dict[Pair, int] = field(default_factory=dict[Pair, int])
    """Per-pair **score**, for the pairs where it is known — deliberately partial.

    Score — surplus plus protocol fees — is what the protocol's own filter compares.
    A pair is missing here only when its orders' fees are unrecorded and some other pair's
    are too (`valuation.pair_score_split`), or in surplus mode, where score is not the
    currency at all."""

    score_anchor_failed: bool = False
    """The recomputed fees did not add up against the recorded score, so `pair_scores` was
    discarded wholesale rather than trusted (`valuation.pair_score_split`). Distinct from
    a pair simply being unrecorded: this one says the fee path is wrong here, and callers
    report it separately so a broken fee port cannot read as thin coverage."""

    order_uids: frozenset[str] = frozenset()
    """All orders traded by the solution, contributing to score or not."""

    winner_pairs: frozenset[Pair] = frozenset()
    """Directed pairs claimed in `pick_winners`, `as_erc20`-normalised and covering
    **all** orders — not just the score-contributing ones (`arbitrator.rs:587`)."""


FilterScale = Literal["score", "surplus"]


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

    baselines: dict[Pair, int] = field(default_factory=dict[Pair, int])
    """Per-pair baseline used by the filter. Exact in score mode — every baseline is some
    single-pair solution's recorded score — which is what lets `validate` reuse it for the
    bracket rather than deriving its own."""

    proxied_uids: frozenset[int] = frozenset()
    """Solutions whose fairness verdict rested on the surplus fallback: some pair had no
    recorded score *and* its surplus did not clear the baseline on its own, so the verdict
    is a guess rather than a proof. Recorded here because `arbitrate` is the one place
    that has both the solutions and the baselines, and every caller that reports the
    filter's exactness would otherwise re-derive it."""

    dropped_uids: frozenset[int] = frozenset()
    """Solutions dropped before ranking because their total was 0
    (`arbitrator.rs:72`). These never reach the DB."""

    @property
    def winners(self) -> tuple[Solution, ...]:
        return tuple(s for s in self.ranked if s.solution_uid in self.winner_uids)

    @property
    def tested(self) -> tuple[Solution, ...]:
        """Solutions the fairness filter actually judged.

        Not every solution: zero-value ones are dropped before step 4 and are therefore
        neither kept nor filtered out. They are the right thing to leave out of any
        "how exact was the filter" denominator.
        """
        return self.ranked + self.filtered_out

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

    Reads `total`, so the baseline is **exact in whatever currency the mode ranks in** and
    needs no decomposition: a single-pair solution puts its whole value on that one pair.
    In score mode that is the recorded score, which is why the right-hand side of the
    filter's comparison is never the approximated one — only the left can be dark.
    """
    baselines: dict[Pair, int] = {}
    for solution in solutions:
        if len(solution.pair_values) != 1:
            continue
        ((pair, _),) = solution.pair_values.items()
        baselines[pair] = max(baselines.get(pair, 0), solution.total)
    return baselines


class PairComparison(NamedTuple):
    """One pair's side of the fairness test."""

    pair: Pair
    value: int
    baseline: int
    exact: bool
    """Is `value` the pair's actual score, or only user surplus standing in as a lower
    bound for it?"""

    @property
    def fair(self) -> bool:
        return self.value >= self.baseline

    @property
    def proxied(self) -> bool:
        """Did the fallback actually decide this comparison?

        Only when an inexact value fails to clear the baseline. Surplus is a lower bound
        on the score, so an inexact value that *clears* the baseline proves the pair fair
        outright — including the common case of a pair with no baseline at all. Marking
        those as approximations would report a guess where there is a proof.
        """
        return not self.exact and not self.fair


def filter_comparisons(
    solution: Solution, baselines: dict[Pair, int]
) -> Iterator[PairComparison]:
    """What the filter compares, pair by pair.

    One currency: the pair's score against the best single-pair solution's score. Where
    the pair's own score is unrecorded, its user surplus stands in — a *lower* bound,
    since protocol fees are non-negative, so the comparison can still prove fairness and
    never fabricates it. That asymmetry is the whole design: a dark pair can only ever
    make the filter stricter than the protocol was, never looser.

    Compare with M1's `scaled` and `raw`, which put a fee-free value against a fee-bearing
    baseline *as if it were exact* and landed 30x further from the recorded filter than
    using no fees at all (docs/winner-selection.md).
    """
    for pair, surplus in solution.pair_values.items():
        score = solution.pair_scores.get(pair)
        baseline = baselines.get(pair, 0)
        if score is not None:
            yield PairComparison(pair, score, baseline, exact=True)
        else:
            yield PairComparison(pair, surplus, baseline, exact=False)


def filter_proxied(solution: Solution, baselines: dict[Pair, int]) -> bool:
    """Did any pair's verdict rest on the fallback rather than on a proof?"""
    return any(c.proxied for c in filter_comparisons(solution, baselines))


def filter_scale(solution: Solution, baselines: dict[Pair, int]) -> FilterScale:
    """`filter_proxied` as the label reports render it."""
    return "surplus" if filter_proxied(solution, baselines) else "score"


def is_fair(solution: Solution, baselines: dict[Pair, int]) -> bool:
    """The fairness test of `arbitrator.rs:93`.

    A solution touching a single pair is always kept — the exemption exists so that
    reference scores do not collapse to 0. Otherwise every pair it touches must be at
    least as valuable as the best solution that trades only that pair.
    """
    if len(solution.pair_values) == 1:
        return True
    return all(c.fair for c in filter_comparisons(solution, baselines))


def filter_guess_could_matter(
    ranking: Ranking, max_winners: int = MAX_WINNERS
) -> bool:
    """Could the guessed filter verdicts have changed who wins this auction?

    A guessed verdict is always a *filter* — `proxied` means some pair failed its baseline
    on an inexact value, and a failed pair is what filters a solution — so the opposite
    outcome is always "kept". This restores every guess at once and re-picks.

    **False is a proof, not a sample.** If restoring all of them leaves the winner set
    alone then none of them won, which means each was blocked by a solution that is a
    winner in *every* scenario; restoring only some of them leaves those blockers in
    place, so no subset can move the winners either. True is weaker — it says at least one
    combination could move them, not that the recorded outcome is wrong.

    This is the only handle on the filter's residual uncertainty in the counterfactual,
    where — unlike `validate` — there is no recorded answer to compare against. It matters
    most on the leave-one-out side: removing the solver that set a baseline lowers it,
    which can un-filter solutions, and a dark pair that still fails the *lowered* baseline
    is a solution we may be wrongly keeping out. `auctions_filter_relaxed` counts the
    un-filterings found; this counts the auctions where one could have been missed.

    Reference scores are **not** covered: a guess could move a reward without moving a
    winner, and this does not look for that.
    """
    if not ranking.proxied_uids:
        return False

    filtered = {s.solution_uid for s in ranking.filtered_out}
    restored = [
        s
        for s in ranking.tested
        if s.solution_uid not in filtered or s.solution_uid in ranking.proxied_uids
    ]
    # `uid` is assigned best-to-worst, so it reproduces the autopilot's own tie-break —
    # more faithful here than list order, since `ranked` is winners-first, not score order.
    restored.sort(key=lambda s: (-s.total, s.solution_uid))
    winners = frozenset(
        restored[i].solution_uid for i in pick_winners(restored, max_winners)
    )
    return winners != ranking.winner_uids


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
    proxied: set[int] = set()
    for solution in scored:
        (kept if is_fair(solution, baselines) else discarded).append(solution)
        # Recorded here rather than by the callers: this is the one place holding both
        # the solutions and the baselines they were judged against.
        if len(solution.pair_values) > 1 and filter_proxied(solution, baselines):
            proxied.add(solution.solution_uid)

    ranked, winner_uids = _rank(kept, max_winners)

    return Ranking(
        ranked=ranked,
        filtered_out=tuple(discarded),
        winner_uids=winner_uids,
        baselines=baselines,
        proxied_uids=frozenset(proxied),
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
