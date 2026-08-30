"""Reproduce the recorded competition and account for every difference.

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

The filtered-out set is the one comparison with an approximation left in it, and only
where a solution's per-pair score split could not be determined — `filter_scale` says
which, and `Summary.fallback_rate` how often.
"""

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
    """What the *true* per-pair score split can and cannot imply about the filter.

    The per-pair split is unknown — scores are stored per solution, not per pair — but
    it is not unconstrained: each pair's true score `v_i` is at least its user surplus
    `u_i` (protocol fees are non-negative) and the `v_i` sum to the recorded score `S`.
    The recorded filter kept the solution iff `v_i >= baseline_i` on every pair, so a
    split it could have kept exists iff the requirements fit inside the score:

        sum(max(baseline_i, u_i))  <=  S

    - `must_filter`  — they do not fit: unfair under every valid split.
    - `must_keep`    — every pair's surplus alone clears its baseline (`u_i >= baseline_i`),
      so any valid split is fair.
    - `undetermined` — both outcomes are consistent with some valid split: a keeping
      split fits, and setting the failing pair's `v_i = u_i` is a filtering one.

    The baselines passed here must be the **score** baselines from `score_baselines`, not
    whatever the configured filter fed itself. They are exact — only single-pair
    solutions set a baseline, and for those the pair's value is the recorded score — which
    is what makes the verdict a statement about the protocol rather than about our model.

    Note the bracket still brackets: it uses only surplus as a *lower bound* on each
    pair's score, so it stays a valid statement about the DB even now that the filter
    itself usually knows the exact split. Where the filter has that split the bracket is
    strictly weaker than what we know, which is why `filter_cause` consults the scale
    first.
    """
    surplus = valuation.pair_surplus
    if len(surplus) <= 1:
        # Single-pair solutions are exempt from the filter entirely.
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
    """`filter_bracket` verdict — what any valid per-pair score split would force."""
    filter_scale: FilterScale = "surplus"
    """How our filter reached its verdict: `score` when every pair either had a recorded
    score or cleared its baseline on surplus alone (a proof either way), `surplus` when
    some pair had neither and the verdict is a guess."""
    score_anchor_failed: bool = False
    """The recomputed fees contradicted the recorded score, so the per-pair split was
    discarded (`valuation.pair_score_split`). Should be zero; a non-zero count means the
    fee path is wrong, not that fee coverage is thin."""

    @property
    def winner_differs(self) -> bool:
        return self.db_winner != self.our_winner

    @property
    def filter_differs(self) -> bool:
        return self.db_filtered != self.our_filtered

    @property
    def filter_cause(self) -> str | None:
        """Why our filter decision differs from the DB's.

        On the **score** scale there is nothing left to excuse a difference. Every pair
        was decided by a proof — a recorded score against a recorded baseline, or a
        surplus that cleared its baseline without needing the fee at all — so the verdict
        is the one the autopilot's own inputs force. A difference means our split, our
        baselines or our pair extraction is wrong: `bug`.

        On the **surplus** fallback the old reading still holds. Where the bracket is
        `undetermined` the true split decides and we do not have it, so the difference is
        the approximation itself: `proxy`. Where the bracket is decisive it binds the DB,
        which filtered on the true split, but binds us only one way — surplus baselines
        sit at or below score baselines, so keeping a solution the score filter drops is
        expected, and that direction is `model`.

        The reverse is impossible on either scale. `must_keep` means every pair's surplus
        already clears its *score* baseline, which is at least its surplus baseline, so
        our filter keeps it too; dropping it anyway means our baselines or valuation are
        wrong. Likewise a DB decision contradicting a decisive bracket cannot come from
        any valid split. Both are `bug`.
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
    """Exact score baselines — in score mode `arbitrate`'s own, which the filter compared
    against. Used by the bracket."""
    guess_could_matter: bool = False
    """Could this auction's guessed filter verdicts have changed who wins? False is a
    proof of harmlessness (`winner_selection.filter_guess_could_matter`). Distinct from
    the guesses themselves: most sit on solutions that lose either way."""
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
        """The validation exit criterion, per auction.

        Returns `None` when every difference has a named, verified cause, and otherwise
        names what is still unaccounted for. Accepted causes are `proxy` (the unknowable
        per-pair split behind the surplus fallback, D2) and `model` (a surplus-based filter
        deliberately answering a different question); a `bug` is not. Both are confined to
        the fallback — a difference on the score scale is never accepted.

        The two "observed" checks are what make this a real gate rather than a
        restatement: they re-run the pick and the reference scores against the DB's own
        filter decisions, so neither can hide behind a proxy filter difference in the
        same auction.
        """
        if self.valuation_failures:
            return "valuation-failure"
        if any(
            check.n_pairs == 0 and not check.db_filtered for check in self.checks
        ):
            # No contributing orders, yet the DB recorded a positive score and kept the
            # solution. The Rust drops zero-value solutions before ranking, so this should
            # be unreachable; the fairness filter would also wave it through vacuously.
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
        """Score difference between the best swapped solution on each side.

        `None` when the winner sets agree, or when one side is empty (the reproduction
        picked more or fewer winners rather than substituting one for another).
        """
        db_only, ours_only = self.swapped
        if not db_only or not ours_only:
            return None
        return max(c.db_score for c in db_only) - max(c.db_score for c in ours_only)


def score_baselines(valued: Iterable[ValuedBid]) -> dict[Pair, int]:
    """Exact per-pair baselines: the best recorded score among single-pair solutions.

    Step 3 only ever considers solutions touching exactly one pair, and for those the
    pair's value *is* the solution's recorded score — so these are the baselines the
    protocol used. `filter_bracket` needs them to say anything about the DB rather than
    about our model.

    In **score** mode `arbitrate` has already computed exactly this — `total` is the
    recorded score there, so `compute_baseline_scores` and this function are the same
    function — and `check_auction` reads the ranking rather than calling this, so the two
    cannot drift. It exists for surplus mode, where the ranking's baselines are surplus
    but the DB still filtered on score, and the bracket would otherwise lose its meaning.
    """
    baselines: dict[Pair, int] = {}
    for entry in valued:
        if len(entry.valuation.pair_surplus) != 1:
            continue
        ((pair, _),) = entry.valuation.pair_surplus.items()
        baselines[pair] = max(baselines.get(pair, 0), entry.bid.score)
    return baselines


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
    # In score mode these *are* the ranking's baselines (see `score_baselines`), so read
    # them rather than deriving a second copy that could drift from the one the filter used.
    exact_baselines = (
        ranking.baselines if mode == "score" else score_baselines(valued)
    )

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
    """Filter decisions differing from the DB. Single-pair solutions are exempt from the
    filter entirely, so only multi-pair solutions can differ — which is what makes them
    the right denominator. Split by `filter_causes` into unknowable (`proxy`), deliberate
    (`model`) and defective (`bug`)."""
    multi_pair_filter_scales: dict[str, int] = field(default_factory=dict[str, int])
    """How each multi-pair solution's verdict was reached: by proof on every pair
    (`score`), or resting on the `surplus` fallback for at least one. The fallback share
    is the residual approximation in the pipeline, and the only place `proxy` and `model`
    differences can still arise."""
    auctions_guess_could_matter: int = 0
    """Auctions where a guessed filter verdict could have moved the winners. The count
    that matters: a guess on a solution that loses either way costs nothing, and most do.
    In `validate` the recorded outcome settles it anyway — this is the number the
    counterfactual has no ground truth for, measured here on the same data."""
    score_anchor_failures: int = 0
    """Solutions whose recomputed fees contradicted the recorded score. Expected to be
    zero; counted apart from the fallback so a broken fee path cannot be mistaken for
    thin fee coverage."""
    valuation_failures: int = 0
    bracket_counts: dict[str, int] = field(default_factory=dict[str, int])
    filter_causes: dict[str, int] = field(default_factory=dict[str, int])
    mismatched: list[AuctionReport] = field(default_factory=list[AuctionReport])
    unexplained: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    """Auctions where a difference is still unaccounted for — the validation exit
    criterion is that this list is empty."""

    @property
    def filter_mismatch_rate(self) -> float:
        """Share of filtered solutions we decide differently from the DB.

        Named for what it measures: before M14 every such difference was the surplus
        proxy's doing and it was called `proxy_error_rate`, but a difference can now also
        be a `bug` on the score scale, which has nothing to do with the fallback. The
        fallback's own share is `fallback_rate`.
        """
        if not self.multi_pair_solutions:
            return 0.0
        return self.multi_pair_filter_mismatch / self.multi_pair_solutions

    @property
    def fallback_rate(self) -> float:
        """Share of filtered solutions whose verdict rested on the surplus fallback."""
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
    """Result of diffing `valuation.order_surplus` against the intermediate dbt model."""

    compared: int = 0
    skipped: int = 0
    """Orders present in the model that we could not value at all. Counted separately so
    "N/N agree" cannot be read as coverage it does not have."""
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
