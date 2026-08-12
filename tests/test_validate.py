"""Tests for the M1 filter-bracket argument.

The bracket is what turns "our filter disagrees with the DB" into a named cause: it
decides whether *any* valid per-pair score split could have produced the DB's decision.
"""

from __future__ import annotations

from loo.extract import AuctionBundle, Bid
from loo.validate import (
    AuctionReport,
    SolutionCheck,
    filter_bracket,
    observed_pick,
)
from loo.valuation import SolutionValuation
from loo.winner_selection import Solution

A = ("weth", "usdc")
B = ("dai", "usdc")


def valuation(pair_surplus: dict) -> SolutionValuation:
    return SolutionValuation(
        pair_surplus=pair_surplus,
        order_surplus_native={},
        order_surplus_atoms={},
        winner_pairs=frozenset(pair_surplus),
        order_uids=frozenset(),
    )


class TestFilterBracket:
    def test_single_pair_is_exempt(self):
        assert filter_bracket(valuation({A: 1}), 100, {A: 10**9}) == "must_keep"

    def test_must_keep_when_surplus_alone_clears_every_baseline(self):
        """Fees are non-negative, so surplus is a lower bound on the per-pair score. If
        even that clears the baselines, no split can make the solution unfair."""
        assert filter_bracket(valuation({A: 60, B: 40}), 200, {A: 50, B: 30}) == "must_keep"

    def test_must_filter_when_a_pair_cannot_reach_its_baseline(self):
        """Pair A needs 500 and pair B its own surplus of 40, which no split of 200 can
        provide, however the fees are distributed."""
        assert filter_bracket(valuation({A: 60, B: 40}), 200, {A: 500, B: 0}) == "must_filter"

    def test_must_filter_when_the_baselines_jointly_exceed_the_score(self):
        """Each pair could reach its baseline of 60 alone (the other pair's surplus of 10
        leaves up to 90), but not both at once: keeping needs 60 + 60 > 100. The
        pair-by-pair interval test missed exactly this case."""
        assert filter_bracket(valuation({A: 10, B: 10}), 100, {A: 60, B: 60}) == "must_filter"

    def test_undetermined_between_the_bounds(self):
        """A's surplus (60) is under its baseline (100), but a keeping split exists
        (100 + 40 <= 200), so both filter outcomes are consistent."""
        assert filter_bracket(valuation({A: 60, B: 40}), 200, {A: 100, B: 0}) == "undetermined"

    def test_a_pair_without_a_baseline_never_binds(self):
        assert filter_bracket(valuation({A: 60, B: 40}), 200, {}) == "must_keep"

    def test_the_requirement_is_bounded_by_the_recorded_score_not_the_surplus_total(self):
        """With no fees at all the score is the surplus, so a baseline one atom above a
        pair's surplus is decided outright."""
        assert filter_bracket(valuation({A: 60, B: 40}), 100, {A: 61, B: 0}) == "must_filter"


def check(**kwargs) -> SolutionCheck:
    defaults = dict(
        uid=0,
        solver="a",
        db_score=100,
        total=100,
        n_pairs=2,
        db_winner=False,
        our_winner=False,
        db_filtered=False,
        our_filtered=False,
        partially_fillable=False,
        bracket="undetermined",
    )
    defaults.update(kwargs)
    return SolutionCheck(**defaults)


class TestFilterCause:
    """A decisive bracket binds the DB, which filtered on the true split. It binds us only
    in one direction, because we filter on surplus and surplus baselines sit at or below
    the score baselines the protocol used."""

    def test_no_difference_has_no_cause(self):
        assert check().filter_cause is None
        assert check(bracket="must_keep").filter_cause is None

    def test_undetermined_difference_is_blamed_on_the_proxy(self):
        assert check(db_filtered=False, our_filtered=True).filter_cause == "proxy"
        assert check(db_filtered=True, our_filtered=False).filter_cause == "proxy"

    def test_difference_under_must_keep_is_a_bug(self):
        assert (
            check(db_filtered=False, our_filtered=True, bracket="must_keep").filter_cause
            == "bug"
        )
        assert (
            check(db_filtered=True, our_filtered=False, bracket="must_keep").filter_cause
            == "bug"
        )

    def test_keeping_what_the_score_filter_drops_is_a_modelling_difference(self):
        """The one legitimate direction: surplus baselines sit below score baselines, so
        the surplus filter keeps batches the protocol's filter drops."""
        assert (
            check(
                db_filtered=True, our_filtered=False, bracket="must_filter"
            ).filter_cause
            == "model"
        )

    def test_dropping_what_the_score_filter_keeps_is_a_bug(self):
        """The reverse is impossible: a pair clearing its score baseline clears its
        surplus baseline too, so our filter cannot be the one dropping it."""
        assert (
            check(
                db_filtered=False, our_filtered=True, bracket="must_filter"
            ).filter_cause
            == "bug"
        )


def bid(uid: int, solver: str, score: int, is_winner: bool, filtered_out: bool) -> Bid:
    return Bid(
        auction_id=1,
        uid=uid,
        solver=solver,
        score=score,
        is_winner=is_winner,
        filtered_out=filtered_out,
        orders=(),
        contributes={},
    )


def sol(uid: int, solver: str, total: int, pairs: frozenset) -> Solution:
    return Solution(
        solver=solver,
        solution_uid=uid,
        total=total,
        pair_values={p: total for p in pairs},
        order_uids=frozenset(),
        winner_pairs=pairs,
    )


class TestObservedPick:
    """`observed_pick` re-runs step 5 on the DB's own kept set, so a pick bug cannot be
    written off as a proxy filter difference in the same auction."""

    def bundle(self, bids) -> AuctionBundle:
        return AuctionBundle(
            auction_id=1,
            jit_owners=frozenset(),
            native_prices={},
            bids=tuple(bids),
            reference_scores={},
        )

    def test_reproduces_a_simple_recorded_pick(self):
        bundle = self.bundle(
            [bid(0, "a", 100, True, False), bid(1, "b", 90, False, False)]
        )
        solutions = [sol(0, "a", 100, frozenset({A})), sol(1, "b", 90, frozenset({A}))]
        assert observed_pick(bundle, solutions) == frozenset({0})

    def test_ignores_solutions_the_db_filtered(self):
        """The filtered solution outscores everything, but is excluded by fiat."""
        bundle = self.bundle(
            [
                bid(0, "a", 100, True, False),
                bid(1, "b", 90, False, False),
                bid(2, "c", 500, False, True),
            ]
        )
        solutions = [
            sol(0, "a", 100, frozenset({A})),
            sol(1, "b", 90, frozenset({A})),
            sol(2, "c", 500, frozenset({A})),
        ]
        assert observed_pick(bundle, solutions) == frozenset({0})

    def test_orders_by_score_not_by_uid(self):
        """`uid` order is winner-first; the pre-pick order was pure score order. Only
        the latter reproduces the pick, so this must sort rather than trust uid."""
        bundle = self.bundle(
            [
                bid(0, "a", 100, True, False),
                bid(1, "c", 80, True, False),
                bid(2, "b", 90, False, False),
            ]
        )
        solutions = [
            sol(0, "a", 100, frozenset({A})),
            sol(1, "c", 80, frozenset({B})),
            sol(2, "b", 90, frozenset({A, B})),
        ]
        assert observed_pick(bundle, solutions) == frozenset({0, 1})


class TestUnexplained:
    def test_clean_report_is_explained(self):
        report = AuctionReport(
            auction_id=1,
            n_solutions=1,
            observed_pick_uids=frozenset({0}),
            db_winner_uids=frozenset({0}),
        )
        assert report.unexplained is None

    def test_pick_difference_is_flagged_even_alongside_a_proxy_filter_difference(self):
        """The case the weaker `winners_match and filter_matches` test used to miss."""
        report = AuctionReport(
            auction_id=1,
            n_solutions=2,
            checks=[
                check(uid=0, db_filtered=False, our_filtered=True, bracket="undetermined")
            ],
            observed_pick_uids=frozenset({1}),
            db_winner_uids=frozenset({0}),
        )
        assert report.filter_causes == {"proxy": 1}
        assert report.unexplained == "pick-differs-on-observed-kept-set"

    def test_a_modelling_difference_is_explained(self):
        report = AuctionReport(
            auction_id=1,
            n_solutions=1,
            checks=[
                check(db_filtered=True, our_filtered=False, bracket="must_filter")
            ],
            observed_pick_uids=frozenset({0}),
            db_winner_uids=frozenset({0}),
        )
        assert report.filter_causes == {"model": 1}
        assert report.unexplained is None

    def test_a_bug_is_not_explained(self):
        report = AuctionReport(
            auction_id=1,
            n_solutions=1,
            checks=[
                check(db_filtered=False, our_filtered=True, bracket="must_keep")
            ],
            observed_pick_uids=frozenset({0}),
            db_winner_uids=frozenset({0}),
        )
        assert report.unexplained == "filter-decision-no-valid-split-explains"

    def test_a_solution_with_no_contributing_orders_is_flagged(self):
        """The DB recorded a score and kept it, yet nothing in it counts toward score.
        The filter would wave it through vacuously, so it has to be surfaced."""
        report = AuctionReport(
            auction_id=1,
            n_solutions=1,
            checks=[check(n_pairs=0, db_filtered=False, our_filtered=False)],
            observed_pick_uids=frozenset({0}),
            db_winner_uids=frozenset({0}),
        )
        assert report.unexplained == "solution-with-no-contributing-orders"

    def test_valuation_failure_dominates(self):
        report = AuctionReport(
            auction_id=1,
            n_solutions=1,
            valuation_failures=[(0, "negative surplus")],
            observed_pick_uids=frozenset({0}),
            db_winner_uids=frozenset({0}),
        )
        assert report.unexplained == "valuation-failure"
