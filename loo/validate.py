"""M1 step 5: reproduce the recorded competition and account for every difference.

Three comparisons, all in score mode:

| recomputed | ground truth |
| --- | --- |
| winner set | `proposed_solutions.is_winner` |
| filtered-out set | `proposed_solutions.filtered_out` |
| reference scores | `stg_backend_data__reference_scores` |

Reference scores are checked twice. Once from our own `arbitrate` output, which fails if
anything upstream is wrong, and once from the ranking the DB itself recorded — kept set,
winner flags and `uid` ordering all taken as given. The second isolates
`compute_reference_scores` from the filter and the pick, so a reference-score
disagreement can be attributed rather than merely observed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .extract import AuctionBundle, Bid
from .primitives import MAX_WINNERS, Pair
from .valuation import (
    Mode,
    PairProxy,
    SolutionValuation,
    ValuationError,
    order_surplus,
    pair_values_for_mode,
    value_solution,
)
from .winner_selection import (
    Ranking,
    Solution,
    arbitrate,
    compute_reference_scores,
    pick_winners,
)


@dataclass(frozen=True)
class FilterBracket:
    """What the *true* per-pair score split can and cannot imply about the filter.

    The per-pair split is unknown — scores are stored per solution, not per pair — but
    it is not unconstrained. For each pair `i` of a solution with total score `S` and
    native user surplus `u_i`, the true per-pair score `v_i` satisfies

        u_i  <=  v_i  <=  S - sum(u_j for j != i)

    because protocol fees are non-negative and the `v_j` sum to `S`. Comparing that
    interval against the (exact) baselines decides the filter outright in some cases:

    - `must_keep`   — every pair clears its baseline at its *lower* bound.
    - `must_filter` — some pair misses its baseline even at its *upper* bound.
    - `undetermined` — both filter outcomes are consistent with some valid split.

    Baselines are exact in score mode: only single-pair solutions set them, and for
    those the pair's value is the solution's recorded score.
    """

    verdict: str
    binding_pair: Pair | None = None
    margin: int = 0
    """For `must_filter`, how far the binding pair's upper bound falls short of its
    baseline; for `must_keep`, the smallest lower-bound surplus over a baseline."""


def filter_bracket(
    valuation: SolutionValuation, db_score: int, baselines: dict[Pair, int]
) -> FilterBracket:
    """Bracket the fairness decision for one multi-pair solution."""
    surplus = valuation.pair_surplus
    if len(surplus) <= 1:
        # Single-pair solutions are exempt from the filter entirely.
        return FilterBracket("must_keep")

    total_surplus = sum(surplus.values())

    worst_upper_margin: tuple[int, Pair] | None = None
    worst_lower_margin: tuple[int, Pair] | None = None
    for pair, pair_surplus in surplus.items():
        baseline = baselines.get(pair, 0)
        upper = db_score - (total_surplus - pair_surplus)
        upper_margin = upper - baseline
        lower_margin = pair_surplus - baseline
        if worst_upper_margin is None or upper_margin < worst_upper_margin[0]:
            worst_upper_margin = (upper_margin, pair)
        if worst_lower_margin is None or lower_margin < worst_lower_margin[0]:
            worst_lower_margin = (lower_margin, pair)

    assert worst_upper_margin is not None and worst_lower_margin is not None
    if worst_upper_margin[0] < 0:
        return FilterBracket("must_filter", worst_upper_margin[1], worst_upper_margin[0])
    if worst_lower_margin[0] >= 0:
        return FilterBracket("must_keep", worst_lower_margin[1], worst_lower_margin[0])
    return FilterBracket("undetermined")


@dataclass(frozen=True)
class SolutionCheck:
    uid: int
    solver: str
    db_score: int
    total: int
    n_pairs: int
    basis: str
    db_winner: bool
    our_winner: bool
    db_filtered: bool
    our_filtered: bool
    partially_fillable: bool
    pair_values: dict[Pair, int] = field(default_factory=dict)
    pair_surplus: dict[Pair, int] = field(default_factory=dict)
    bracket: str = "must_keep"
    """`filter_bracket` verdict — what any valid per-pair score split would force."""

    @property
    def winner_differs(self) -> bool:
        return self.db_winner != self.our_winner

    @property
    def filter_differs(self) -> bool:
        return self.db_filtered != self.our_filtered

    @property
    def filter_cause(self) -> str | None:
        """Why our filter decision differs from the DB's.

        A decisive bracket binds *both* sides: the DB's decision came from the true
        per-pair split, and both proxies produce a valid candidate split (each pair value
        is at least its surplus and they sum to at most the score), so both must agree
        with `must_keep` and `must_filter`. A difference is therefore only legitimate
        where the bracket is `undetermined` — that is exactly the approximation PLAN.md §2
        accepts. Anywhere else, one of the two is wrong and it needs fixing before M2.
        """
        if not self.filter_differs:
            return None
        return "proxy" if self.bracket == "undetermined" else "bug"


@dataclass
class AuctionReport:
    auction_id: int
    n_solutions: int
    checks: list[SolutionCheck] = field(default_factory=list)
    valuation_failures: list[tuple[int, str]] = field(default_factory=list)
    reference_recomputed: dict[str, int] = field(default_factory=dict)
    reference_observed: dict[str, int] = field(default_factory=dict)
    reference_db: dict[str, int] = field(default_factory=dict)
    baselines: dict[Pair, int] = field(default_factory=dict)
    observed_pick_uids: frozenset[int] = frozenset()
    """Winners obtained by re-picking on the DB's own kept set — see `observed_pick`."""
    db_winner_uids: frozenset[int] = frozenset()

    @property
    def winners_match(self) -> bool:
        return not any(c.winner_differs for c in self.checks)

    @property
    def pick_matches_observed(self) -> bool:
        """Does step 5 alone reproduce the recorded winners, given the recorded filter?"""
        return self.observed_pick_uids == self.db_winner_uids

    @property
    def filter_matches(self) -> bool:
        return not any(c.filter_differs for c in self.checks)

    @property
    def reference_matches_recomputed(self) -> bool:
        return self.reference_recomputed == self.reference_db

    @property
    def reference_matches_observed(self) -> bool:
        return self.reference_observed == self.reference_db

    @property
    def any_partially_fillable(self) -> bool:
        return any(c.partially_fillable for c in self.checks)

    @property
    def disagreement(self) -> str:
        """Where the reproduction diverged, for triage."""
        if self.valuation_failures:
            return "valuation"
        parts = []
        if not self.filter_matches:
            parts.append("filter")
        if not self.winners_match:
            parts.append("pick")
        if not self.reference_matches_recomputed:
            parts.append("reference")
        return "+".join(parts) if parts else "none"

    @property
    def filter_causes(self) -> dict[str, int]:
        causes: dict[str, int] = {}
        for check in self.checks:
            cause = check.filter_cause
            if cause:
                causes[cause] = causes.get(cause, 0) + 1
        return causes

    @property
    def unexplained(self) -> str | None:
        """The M1 exit criterion, per auction.

        Returns `None` when every difference has a named, verified cause, and otherwise
        names what is still unaccounted for. The per-pair proxy of PLAN.md §2 is an
        accepted cause; anything else is a bug to resolve before M2.

        The two "observed" checks are what make this a real gate rather than a
        restatement: they re-run the pick and the reference scores against the DB's own
        filter decisions, so neither can hide behind a proxy filter difference in the
        same auction.
        """
        if self.valuation_failures:
            return "valuation-failure"
        if any(check.filter_cause == "bug" for check in self.checks):
            return "filter-decision-no-valid-split-explains"
        if not self.pick_matches_observed:
            return "pick-differs-on-observed-kept-set"
        if not self.reference_matches_observed:
            return "reference-scores-differ-on-observed-ranking"
        return None

    @property
    def swapped(self) -> tuple[list[SolutionCheck], list[SolutionCheck]]:
        """(won only in the DB, won only in our reproduction)."""
        return (
            [c for c in self.checks if c.db_winner and not c.our_winner],
            [c for c in self.checks if c.our_winner and not c.db_winner],
        )

    @property
    def score_gap(self) -> int | None:
        """Score difference between the best swapped solution on each side.

        `None` when the winner sets agree, or when one side is empty (the reproduction
        picked more or fewer winners rather than substituting one for another).
        """
        db_only, ours_only = self.swapped
        if not db_only or not ours_only:
            return None
        return max(c.db_score for c in db_only) - max(c.db_score for c in ours_only)


@dataclass(frozen=True)
class ValuedBid:
    bid: Bid
    solution: Solution
    valuation: SolutionValuation
    basis: str


def build_solutions(
    bundle: AuctionBundle,
    weth: str,
    *,
    mode: Mode = "score",
    pair_proxy: PairProxy = "scaled",
) -> tuple[list[ValuedBid], dict[int, str]]:
    """Value every bid in an auction.

    Returns the valued bids and the per-uid valuation failures. A bid whose valuation
    fails is left out entirely, which is what the Rust does (`arbitrator.rs:155`
    discards the whole solution when any contributing order cannot be scored). Because
    the DB only ever stores solutions whose score computation succeeded, any failure
    here is a reproduction defect and is reported rather than swallowed.
    """
    valued: list[ValuedBid] = []
    failures: dict[int, str] = {}

    for bid in bundle.bids:
        try:
            valuation = value_solution(
                bid.orders, bid.contributes, bundle.native_prices, weth
            )
            values = pair_values_for_mode(valuation, mode, bid.score, pair_proxy)
        except ValuationError as error:
            failures[bid.uid] = str(error)
            continue

        valued.append(
            ValuedBid(
                bid=bid,
                solution=Solution(
                    solver=bid.solver,
                    solution_uid=bid.uid,
                    total=values.total,
                    pair_values=values.values,
                    order_uids=valuation.order_uids,
                    winner_pairs=valuation.winner_pairs,
                ),
                valuation=valuation,
                basis=values.basis,
            )
        )

    return valued, failures


def observed_ranking(bundle: AuctionBundle, solutions: Iterable[Solution]) -> Ranking:
    """Rebuild the `Ranking` the autopilot recorded, taking the DB at its word.

    `uid` is assigned best-to-worst, so ordering the kept solutions by `uid` reproduces
    `arbitrate`'s output ordering exactly — winners first by descending score, then
    non-winners, with filtered-out solutions after both. That includes the tie-breaks
    from the autopilot's pre-sort shuffle, which are not recoverable any other way.
    """
    by_uid = {s.solution_uid: s for s in solutions}
    winner_uids = frozenset(b.uid for b in bundle.bids if b.is_winner and b.uid in by_uid)
    ranked = tuple(
        by_uid[b.uid]
        for b in sorted(bundle.bids, key=lambda b: b.uid)
        if not b.filtered_out and b.uid in by_uid
    )
    filtered = tuple(
        by_uid[b.uid] for b in bundle.bids if b.filtered_out and b.uid in by_uid
    )
    return Ranking(ranked=ranked, filtered_out=filtered, winner_uids=winner_uids)


def observed_pick(
    bundle: AuctionBundle, solutions: Iterable[Solution], max_winners: int = MAX_WINNERS
) -> frozenset[int]:
    """Re-pick winners on the kept set the DB recorded, isolating step 5.

    Takes `filtered_out` as given and re-runs `pick_winners` over the remaining
    solutions in descending score order, with `uid` breaking ties. In score mode every
    quantity involved is exact — scores come from the DB and token pairs are read off
    the trades — so this tests `pick_winners` and the `winner_pairs` extraction with no
    proxy anywhere in the path.

    Without this, a pick-level bug in an auction that *also* has a proxy filter
    difference would be attributed to the proxy and never looked at.
    """
    by_uid = {s.solution_uid: s for s in solutions}
    kept = [
        by_uid[bid.uid]
        for bid in sorted(bundle.bids, key=lambda b: b.uid)
        if not bid.filtered_out and bid.uid in by_uid
    ]
    kept.sort(key=lambda s: -s.total)
    return frozenset(kept[i].solution_uid for i in pick_winners(kept, max_winners))


def check_auction(
    bundle: AuctionBundle,
    weth: str,
    *,
    mode: Mode = "score",
    pair_proxy: PairProxy = "scaled",
    max_winners: int = MAX_WINNERS,
) -> AuctionReport:
    """Reproduce one auction and diff it against the DB."""
    valued, failures = build_solutions(bundle, weth, mode=mode, pair_proxy=pair_proxy)
    solutions = [v.solution for v in valued]
    ranking = arbitrate(solutions, max_winners)

    our_filtered = {s.solution_uid for s in ranking.filtered_out}
    # A solution we failed to value never reaches the ranking, so it shows up as
    # neither a winner nor filtered out; `valuation_failures` is what flags it.
    report = AuctionReport(
        auction_id=bundle.auction_id,
        n_solutions=len(bundle.bids),
        valuation_failures=sorted(failures.items()),
        reference_recomputed=compute_reference_scores(ranking, max_winners),
        reference_observed=compute_reference_scores(
            observed_ranking(bundle, solutions), max_winners
        ),
        reference_db=dict(bundle.reference_scores),
        baselines=ranking.baselines,
        observed_pick_uids=observed_pick(bundle, solutions, max_winners),
        db_winner_uids=frozenset(b.uid for b in bundle.bids if b.is_winner),
    )

    by_uid = {v.bid.uid: v for v in valued}
    for bid in bundle.bids:
        entry = by_uid.get(bid.uid)
        bracket = (
            filter_bracket(entry.valuation, bid.score, ranking.baselines)
            if entry
            else FilterBracket("undetermined")
        )
        report.checks.append(
            SolutionCheck(
                uid=bid.uid,
                solver=bid.solver,
                db_score=bid.score,
                total=entry.solution.total if entry else 0,
                n_pairs=len(entry.solution.pair_values) if entry else 0,
                basis=entry.basis if entry else "unvalued",
                db_winner=bid.is_winner,
                our_winner=bid.uid in ranking.winner_uids,
                db_filtered=bid.filtered_out,
                our_filtered=bid.uid in our_filtered,
                partially_fillable=any(o.partially_fillable for o in bid.orders),
                pair_values=dict(entry.solution.pair_values) if entry else {},
                pair_surplus=dict(entry.valuation.pair_surplus) if entry else {},
                bracket=bracket.verdict,
            )
        )

    return report


@dataclass
class Summary:
    """Aggregate over a window."""

    auctions: int = 0
    auctions_winners_match: int = 0
    auctions_filter_match: int = 0
    auctions_reference_match_recomputed: int = 0
    auctions_reference_match_observed: int = 0
    auctions_pick_match_observed: int = 0
    solutions: int = 0
    multi_pair_solutions: int = 0
    multi_pair_filter_mismatch: int = 0
    """The honest cost of the per-pair proxy: single-pair solutions are exempt from the
    filter and their pair value is exact, so only multi-pair solutions can be wrong."""
    valuation_failures: int = 0
    basis_counts: dict[str, int] = field(default_factory=dict)
    bracket_counts: dict[str, int] = field(default_factory=dict)
    filter_causes: dict[str, int] = field(default_factory=dict)
    mismatched: list[AuctionReport] = field(default_factory=list)
    unexplained: list[tuple[int, str]] = field(default_factory=list)
    """Auctions where a difference is still unaccounted for — the M1 exit criterion is
    that this list is empty."""

    @property
    def proxy_error_rate(self) -> float:
        if not self.multi_pair_solutions:
            return 0.0
        return self.multi_pair_filter_mismatch / self.multi_pair_solutions

    def add(self, report: AuctionReport) -> None:
        self.auctions += 1
        self.solutions += report.n_solutions
        self.valuation_failures += len(report.valuation_failures)
        self.auctions_winners_match += report.winners_match
        self.auctions_filter_match += report.filter_matches
        self.auctions_reference_match_recomputed += report.reference_matches_recomputed
        self.auctions_reference_match_observed += report.reference_matches_observed
        self.auctions_pick_match_observed += report.pick_matches_observed

        for check in report.checks:
            self.basis_counts[check.basis] = self.basis_counts.get(check.basis, 0) + 1
            if check.n_pairs > 1:
                self.multi_pair_solutions += 1
                self.bracket_counts[check.bracket] = (
                    self.bracket_counts.get(check.bracket, 0) + 1
                )
                if check.filter_differs:
                    self.multi_pair_filter_mismatch += 1
            cause = check.filter_cause
            if cause:
                self.filter_causes[cause] = self.filter_causes.get(cause, 0) + 1

        if report.disagreement != "none":
            self.mismatched.append(report)
        reason = report.unexplained
        if reason:
            self.unexplained.append((report.auction_id, reason))


@dataclass
class SurplusCrossCheck:
    """Result of diffing `valuation.order_surplus` against the intermediate dbt model."""

    compared: int = 0
    skipped: int = 0
    """Orders present in the model that we could not value at all. Counted separately so
    "N/N agree" cannot be read as coverage it does not have."""
    mismatches: list[tuple[int, int, str, int, int]] = field(default_factory=list)
    """(auction, solution uid, order uid, ours, theirs)."""

    @property
    def agreed(self) -> int:
        return self.compared - len(self.mismatches)

    def merge(self, other: "SurplusCrossCheck") -> None:
        self.compared += other.compared
        self.skipped += other.skipped
        self.mismatches.extend(other.mismatches)


def check_surplus_against_db(
    bundle: AuctionBundle, db_surplus: dict[tuple[int, int, str], int]
) -> SurplusCrossCheck:
    """Cross-check `valuation.order_surplus` against the intermediate dbt model.

    The dbt model computes the same quantity by a different route, so agreement is
    independent evidence that the surplus formulas are right. Note the model rounds
    where the Rust does not — see
    [analytics-db.md](../docs/analytics-db.md#order_surplus_atoms_in_surplus_token-rounds)
    — so a scatter of one-atom differences in our favour is expected, not a defect.
    """
    result = SurplusCrossCheck()
    for bid in bundle.bids:
        for order in bid.orders:
            key = (bundle.auction_id, bid.uid, order.uid)
            if key not in db_surplus:
                continue
            try:
                ours = order_surplus(order)
            except ValuationError:
                result.skipped += 1
                continue
            result.compared += 1
            if ours != db_surplus[key]:
                result.mismatches.append(
                    (bundle.auction_id, bid.uid, order.uid, ours, db_surplus[key])
                )
    return result
