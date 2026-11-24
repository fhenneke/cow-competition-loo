"""Reproduce the recorded competition (winners, filtered-out set, reference scores) and
account for every difference."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .extract import AuctionBundle
from .primitives import MAX_WINNERS, Pair
from .valuation import (
    Mode,
    SolutionValuation,
    ValuationError,
    ValuedBid,
    build_solutions,
    order_surplus,
)
from .winner_selection import (
    FilterScale,
    Ranking,
    Solution,
    arbitrate,
    compute_reference_scores,
    filter_guess_could_matter,
    pick_winners,
)


def filter_bracket(
    valuation: SolutionValuation, db_score: int, baselines: dict[Pair, int]
) -> str:
    """What any valid per-pair split forces: each pair's score `v_i >= u_i` (surplus) and
    `sum(v_i) == db_score`, so a keeping split exists iff
    `sum(max(baseline_i, u_i)) <= db_score`.

    `baselines` must be exact score baselines (`score_baselines`), not surplus ones.
    """
    surplus = valuation.pair_surplus
    if len(surplus) <= 1:
        return "must_keep"
    if sum(max(baselines.get(pair, 0), u) for pair, u in surplus.items()) > db_score:
        return "must_filter"
    if all(u >= baselines.get(pair, 0) for pair, u in surplus.items()):
        return "must_keep"
    return "undetermined"


@dataclass(frozen=True)
class SolutionCheck:
    uid: int
    solver: str
    db_score: int
    total: int
    n_pairs: int
    db_winner: bool
    our_winner: bool
    db_filtered: bool
    our_filtered: bool
    partially_fillable: bool
    pair_values: dict[Pair, int] = field(default_factory=dict[Pair, int])
    pair_surplus: dict[Pair, int] = field(default_factory=dict[Pair, int])
    bracket: str = "must_keep"
    filter_scale: FilterScale = "surplus"
    """`surplus`: some pair's verdict was a guess on the fallback."""
    score_anchor_failed: bool = False

    @property
    def winner_differs(self) -> bool:
        return self.db_winner != self.our_winner

    @property
    def filter_differs(self) -> bool:
        return self.db_filtered != self.our_filtered

    @property
    def filter_cause(self) -> str | None:
        """Score scale is a proof, so any difference is a `bug`. On the surplus fallback:
        `undetermined` is `proxy`; keeping what the DB filtered under `must_filter` is
        `model` (surplus baselines <= score baselines); anything else is `bug`.
        """
        if not self.filter_differs:
            return None
        if self.filter_scale == "score":
            return "bug"
        if self.bracket == "undetermined":
            return "proxy"
        if self.db_filtered and not self.our_filtered and self.bracket == "must_filter":
            return "model"
        return "bug"


@dataclass
class AuctionReport:
    auction_id: int
    n_solutions: int
    checks: list[SolutionCheck] = field(default_factory=list[SolutionCheck])
    valuation_failures: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    reference_recomputed: dict[str, int] = field(default_factory=dict[str, int])
    reference_observed: dict[str, int] = field(default_factory=dict[str, int])
    reference_db: dict[str, int] = field(default_factory=dict[str, int])
    baselines: dict[Pair, int] = field(default_factory=dict[Pair, int])
    """Exact score baselines."""
    guess_could_matter: bool = False
    observed_pick_uids: frozenset[int] = frozenset()
    db_winner_uids: frozenset[int] = frozenset()

    @property
    def winners_match(self) -> bool:
        return not any(c.winner_differs for c in self.checks)

    @property
    def pick_matches_observed(self) -> bool:
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
        if self.valuation_failures:
            return "valuation"
        parts: list[str] = []
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
        """The validation exit criterion: `None` when every difference has an accepted
        cause (`proxy` or `model`), else what is unaccounted for."""
        if self.valuation_failures:
            return "valuation-failure"
        if any(
            check.n_pairs == 0 and not check.db_filtered for check in self.checks
        ):
            # Unreachable: the Rust drops solutions with no auction order before ranking.
            return "solution-with-no-contributing-orders"
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
        """Best DB-only minus best ours-only score; `None` unless both sides are non-empty."""
        db_only, ours_only = self.swapped
        if not db_only or not ours_only:
            return None
        return max(c.db_score for c in db_only) - max(c.db_score for c in ours_only)


def score_baselines(valued: Iterable[ValuedBid]) -> dict[Pair, int]:
    """Best recorded score per pair among single-pair solutions: the protocol's exact
    baselines. Needed in surplus mode, where the ranking's baselines are surplus."""
    baselines: dict[Pair, int] = {}
    for entry in valued:
        if len(entry.valuation.pair_surplus) != 1:
            continue
        ((pair, _),) = entry.valuation.pair_surplus.items()
        baselines[pair] = max(baselines.get(pair, 0), entry.bid.score)
    return baselines


def observed_ranking(bundle: AuctionBundle, solutions: Iterable[Solution]) -> Ranking:
    """The `Ranking` the DB recorded. Kept solutions in uid order reproduce `arbitrate`'s
    ordering, tie-breaks included."""
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
    """Re-pick winners on the DB's kept set, isolating step 5 from our filter."""
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
    max_winners: int = MAX_WINNERS,
) -> AuctionReport:
    """Reproduce one auction and diff it against the DB."""
    valued, failures = build_solutions(
        bundle.bids,
        bundle.native_prices,
        weth,
        mode=mode,
        fee_context=bundle.fee_context,
    )
    solutions = [v.solution for v in valued]
    ranking = arbitrate(solutions, max_winners)
    # In score mode the ranking's baselines are already the exact score baselines.
    exact_baselines = (
        ranking.baselines if mode == "score" else score_baselines(valued)
    )

    our_filtered = {s.solution_uid for s in ranking.filtered_out}
    report = AuctionReport(
        auction_id=bundle.auction_id,
        n_solutions=len(bundle.bids),
        valuation_failures=sorted(failures.items()),
        reference_recomputed=compute_reference_scores(ranking, max_winners),
        reference_observed=compute_reference_scores(
            observed_ranking(bundle, solutions), max_winners
        ),
        reference_db=dict(bundle.reference_scores),
        baselines=exact_baselines,
        guess_could_matter=filter_guess_could_matter(ranking, max_winners),
        observed_pick_uids=observed_pick(bundle, solutions, max_winners),
        db_winner_uids=frozenset(b.uid for b in bundle.bids if b.is_winner),
    )

    by_uid = {v.bid.uid: v for v in valued}
    for bid in bundle.bids:
        entry = by_uid.get(bid.uid)
        bracket = (
            filter_bracket(entry.valuation, bid.score, exact_baselines)
            if entry
            else "undetermined"
        )
        report.checks.append(
            SolutionCheck(
                uid=bid.uid,
                solver=bid.solver,
                db_score=bid.score,
                total=entry.solution.total if entry else 0,
                n_pairs=len(entry.solution.pair_values) if entry else 0,
                db_winner=bid.is_winner,
                our_winner=bid.uid in ranking.winner_uids,
                db_filtered=bid.filtered_out,
                our_filtered=bid.uid in our_filtered,
                partially_fillable=any(o.partially_fillable for o in bid.orders),
                pair_values=dict(entry.solution.pair_values) if entry else {},
                pair_surplus=dict(entry.valuation.pair_surplus) if entry else {},
                bracket=bracket,
                filter_scale="surplus" if bid.uid in ranking.proxied_uids else "score",
                score_anchor_failed=bool(entry and entry.solution.score_anchor_failed),
            )
        )

    return report


@dataclass
class Summary:
    auctions: int = 0
    auctions_winners_match: int = 0
    auctions_filter_match: int = 0
    auctions_reference_match_recomputed: int = 0
    auctions_reference_match_observed: int = 0
    auctions_pick_match_observed: int = 0
    solutions: int = 0
    multi_pair_solutions: int = 0
    multi_pair_filter_mismatch: int = 0
    multi_pair_filter_scales: dict[str, int] = field(default_factory=dict[str, int])
    auctions_guess_could_matter: int = 0
    score_anchor_failures: int = 0
    """Expected zero; non-zero means the fee path is wrong."""
    valuation_failures: int = 0
    bracket_counts: dict[str, int] = field(default_factory=dict[str, int])
    filter_causes: dict[str, int] = field(default_factory=dict[str, int])
    mismatched: list[AuctionReport] = field(default_factory=list[AuctionReport])
    unexplained: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    """Exit criterion: empty."""

    @property
    def filter_mismatch_rate(self) -> float:
        if not self.multi_pair_solutions:
            return 0.0
        return self.multi_pair_filter_mismatch / self.multi_pair_solutions

    @property
    def fallback_rate(self) -> float:
        if not self.multi_pair_solutions:
            return 0.0
        return self.multi_pair_filter_scales.get("surplus", 0) / self.multi_pair_solutions

    def add(self, report: AuctionReport) -> None:
        self.auctions += 1
        self.solutions += report.n_solutions
        self.valuation_failures += len(report.valuation_failures)
        self.auctions_winners_match += report.winners_match
        self.auctions_filter_match += report.filter_matches
        self.auctions_reference_match_recomputed += report.reference_matches_recomputed
        self.auctions_reference_match_observed += report.reference_matches_observed
        self.auctions_pick_match_observed += report.pick_matches_observed
        self.auctions_guess_could_matter += report.guess_could_matter

        for check in report.checks:
            self.score_anchor_failures += check.score_anchor_failed
            if check.n_pairs > 1 and check.total:
                self.multi_pair_solutions += 1
                self.bracket_counts[check.bracket] = (
                    self.bracket_counts.get(check.bracket, 0) + 1
                )
                self.multi_pair_filter_scales[check.filter_scale] = (
                    self.multi_pair_filter_scales.get(check.filter_scale, 0) + 1
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
    compared: int = 0
    skipped: int = 0
    """In the model but unvaluable by us."""
    mismatches: list[tuple[int, int, str, int, int]] = field(
        default_factory=list[tuple[int, int, str, int, int]]
    )
    """(auction, solution uid, order uid, ours, theirs)."""

    @property
    def agreed(self) -> int:
        return self.compared - len(self.mismatches)

    def merge(self, other: SurplusCrossCheck) -> None:
        self.compared += other.compared
        self.skipped += other.skipped
        self.mismatches.extend(other.mismatches)


def check_surplus_against_db(
    bundle: AuctionBundle, db_surplus: dict[tuple[int, int, str], int]
) -> SurplusCrossCheck:
    """Diff `valuation.order_surplus` against the dbt model, which can be one atom high
    ([why](../docs/analytics-db.md#order_surplus_atoms_in_surplus_token-rounds))."""
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
