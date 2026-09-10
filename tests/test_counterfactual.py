"""Tests for the leave-one-out counterfactual.

Two things carry most of the weight and are pinned down hardest:

- removing a solver may only ever *relax* the fairness filter, never tighten it, so an
  un-kept survivor is an error rather than a finding;
- the outcome rule is applied identically to both sides, so a winner that survives the
  removal cancels exactly and cannot manufacture a surplus difference.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from loo.counterfactual import (
    Analysis,
    AuctionCounterfactual,
    MissingSettlementError,
    OrderDiff,
    OrderOutcome,
    RelaxationError,
    analyse_auction,
    diff_outcomes,
    leave_one_out,
    side_outcomes,
    un_filtered,
)
from loo.extract import AuctionBundle, Bid, Settlement
from loo.fees import FeeContext, Policy, Quote
from loo.primitives import Pair
from loo.valuation import Order, SolutionValuation, ValuedBid
from loo.winner_selection import Solution, arbitrate

ONE = 10**18
WETH = "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DAI = "6b175474e89094c44da98b954eedeac495271d0f"

SETTLED = Settlement(landed=True, in_time=True)
NOT_SETTLED = Settlement(landed=False, in_time=False)
LANDED_LATE = Settlement(landed=True, in_time=False)

A = (WETH, USDC)
B = (DAI, USDC)


def solution(
    solver: str,
    uid: int,
    total: int,
    pair_values: dict[Pair, int] | None = None,
    winner_pairs: frozenset[Pair] | None = None,
) -> Solution:
    values: dict[Pair, int] = pair_values if pair_values is not None else {A: total}
    return Solution(
        solver=solver,
        solution_uid=uid,
        total=total,
        pair_values=values,
        order_uids=frozenset(),
        winner_pairs=winner_pairs if winner_pairs is not None else frozenset(values),
    )


def sell_order(uid: str, pair: tuple[str, str] = A, executed_buy: int = 2100) -> Order:
    """A sell order with `executed_buy - 2000` atoms of surplus in the buy token."""
    return Order(
        uid=uid,
        sell_token=pair[0],
        buy_token=pair[1],
        sell_amount=1000,
        buy_amount=2000,
        executed_sell=1000,
        executed_buy=executed_buy,
        side="sell",
    )


def bid(
    uid: int,
    solver: str,
    score: int,
    orders: list[Order],
    *,
    is_winner: bool = False,
    filtered_out: bool = False,
    contributes: bool = True,
) -> Bid:
    return Bid(
        auction_id=1,
        uid=uid,
        solver=solver,
        score=score,
        is_winner=is_winner,
        filtered_out=filtered_out,
        orders=tuple(orders),
        contributes={order.uid: contributes for order in orders},
    )


def bundle(bids: list[Bid]) -> AuctionBundle:
    return AuctionBundle(
        auction_id=1,
        jit_owners=frozenset(),
        native_prices={USDC: ONE},
        bids=tuple(bids),
        reference_scores={},
    )


def valued(
    entry_bid: Bid,
    surplus: dict[str, int],
    volumes: dict[str, int] | None = None,
    fees_unknown: frozenset[str] = frozenset(),
) -> ValuedBid:
    """A `ValuedBid` carrying exactly the per-order surplus the test wants."""
    return ValuedBid(
        bid=entry_bid,
        solution=solution(entry_bid.solver, entry_bid.uid, entry_bid.score),
        valuation=SolutionValuation(
            pair_surplus={A: sum(surplus.values())},
            order_surplus_native=surplus,
            order_surplus_atoms={},
            winner_pairs=frozenset({A}),
            order_uids=frozenset(o.uid for o in entry_bid.orders),
            order_volume_native=volumes or {},
            fees_unknown=fees_unknown,
        ),
    )


class TestLeaveOneOut:
    def test_removes_every_solution_of_the_solver(self):
        """A solver may bid several times and all of them go, as in
        `compute_reference_scores` (`arbitrator.rs:620`)."""
        solutions = [
            solution("x", 0, 100),
            solution("x", 1, 90),
            solution("b", 2, 80),
        ]
        ranking = leave_one_out(solutions, frozenset({"x"}))
        assert ranking.winner_uids == frozenset({2})

    def test_removing_an_absent_solver_changes_nothing(self):
        solutions = [solution("a", 0, 100), solution("b", 1, 90)]
        assert leave_one_out(solutions, frozenset({"x"})).winner_uids == arbitrate(
            solutions
        ).winner_uids


class TestUnFiltered:
    def test_removing_a_baseline_setter_un_filters_a_batch(self):
        """The case PLAN.md section 5 calls out as the most interesting one.

        `x`'s single-pair solution sets the WETH->USDC baseline at 100, which makes the
        batch unfair on that pair. Without `x` the baseline falls to 40 and the batch is
        fair, kept, and wins.
        """
        solutions = [
            solution("x", 0, 100, {A: 100}),
            solution("batch", 1, 130, {A: 60, B: 70}),
            solution("c", 2, 40, {A: 40}),
        ]
        baseline = arbitrate(solutions)
        assert {s.solution_uid for s in baseline.filtered_out} == {1}

        loo = leave_one_out(solutions, frozenset({"x"}))
        assert un_filtered(baseline, loo, frozenset({"x"})) == frozenset({1})
        assert loo.winner_uids == frozenset({1})

    def test_nothing_un_filtered_when_the_filter_is_unchanged(self):
        solutions = [solution("x", 0, 100), solution("b", 1, 90)]
        baseline = arbitrate(solutions)
        loo = leave_one_out(solutions, frozenset({"x"}))
        assert un_filtered(baseline, loo, frozenset({"x"})) == frozenset()

    def test_un_keeping_a_survivor_is_an_error_not_a_finding(self):
        """Baselines are maxima, so dropping solutions can only lower them and fairness is
        monotone. Only a defect can produce the other direction, so it is raised."""
        weak_baseline = solution("x", 0, 50, {A: 50})
        kept = solution("b", 1, 130, {A: 60, B: 70})
        baseline = arbitrate([weak_baseline, kept])
        assert {s.solution_uid for s in baseline.ranked} == {0, 1}
        # A ranking that lost `b` anyway — what a broken filter would produce.
        broken = arbitrate([weak_baseline])
        with pytest.raises(RelaxationError, match="baselines can only fall"):
            un_filtered(baseline, broken, frozenset({"x"}))


class TestSideOutcomes:
    def winner(self, uid: int, solver: str, order_uid: str, surplus: int) -> ValuedBid:
        return valued(
            bid(uid, solver, 100, [sell_order(order_uid)], is_winner=True),
            {order_uid: surplus},
        )

    def test_a_recorded_winner_takes_its_own_failed_settlement(self):
        """Under `inherited` a solution that won for real is judged on what it actually
        did — only `assume-settled` overrides the record."""
        side = side_outcomes(
            [self.winner(0, "a", "o1", 100)],
            outcome_rule="inherited",
            settled={0: NOT_SETTLED},
            recorded_winner_uids=frozenset({0}),
            inherit={A: False},
        )
        assert side.orders["o1"].executed is False
        assert side.orders["o1"].surplus_native is None
        assert side.orders["o1"].observed is True
        assert side.replacements == frozenset()

    def test_a_late_settlement_counts_as_a_failure(self):
        """A batch that lands after its deadline really does fill the order, so the user does
        receive surplus — but the protocol pays nothing for it, and the surplus and reward
        sides have to agree on which winners delivered. So it is carried as a failure with
        zero surplus, and `landed_late` records that the discarded surplus was real. 16 of
        8,784 landings in the M1 window were late."""
        side = side_outcomes(
            [self.winner(0, "a", "o1", 100)],
            outcome_rule="inherited",
            settled={0: LANDED_LATE},
            recorded_winner_uids=frozenset({0}),
        )
        assert side.orders["o1"].executed is False
        assert side.orders["o1"].surplus_native is None
        assert side.orders["o1"].landed_late is True

    def test_lateness_is_counted_so_the_discarded_surplus_stays_visible(self):
        result = analyse_auction(
            bundle(
                [
                    bid(0, "x", 200, [sell_order("o1", executed_buy=2200)], is_winner=True),
                    bid(1, "b", 100, [sell_order("o1", executed_buy=2100)]),
                ]
            ),
            WETH,
            frozenset({"x"}),
            settled={0: LANDED_LATE},
        )
        (diff,) = result.order_diffs
        assert diff.late_base and diff.unsettled_base
        # The slot was late, so the replacement inherits a failure too: nothing executes.
        assert (diff.executed_base, diff.executed_loo) == (False, False)
        assert result.delta_surplus == 0

        analysis = Analysis()
        analysis.add(result)
        assert analysis.orders_lost_to_lateness == 1
        assert analysis.orders_unsettled_base == 1

    def test_proposed_rule_ignores_settlement(self):
        side = side_outcomes(
            [self.winner(0, "a", "o1", 100)],
            outcome_rule="assume-settled",
            settled={0: NOT_SETTLED},
            recorded_winner_uids=frozenset({0}),
        )
        assert side.orders["o1"].executed is True
        assert side.orders["o1"].surplus_native == 100
        assert side.orders["o1"].observed is False
        assert side.orphans == frozenset()

    def test_inherited_rule_reverts_a_replacement_of_a_reverted_slot(self):
        """The rule this milestone runs on: settlement belongs to the slot. The batch that
        really held WETH->USDC reverted, so whatever would have replaced it reverts too."""
        side = side_outcomes(
            [self.winner(3, "a", "o1", 100)],
            outcome_rule="inherited",
            settled={},
            recorded_winner_uids=frozenset({0}),
            inherit={A: False},
        )
        assert side.orders["o1"].executed is False
        assert side.orders["o1"].surplus_native is None
        assert side.replacements == frozenset({3})
        assert side.inherited_reverts == frozenset({3})
        assert side.orphans == frozenset()

    def test_inherited_rule_settles_a_replacement_of_a_settled_slot(self):
        side = side_outcomes(
            [self.winner(3, "a", "o1", 100)],
            outcome_rule="inherited",
            settled={},
            recorded_winner_uids=frozenset({0}),
            inherit={A: True},
        )
        assert side.orders["o1"].executed is True
        assert side.orders["o1"].surplus_native == 100
        assert side.inherited_reverts == frozenset()
        assert side.orphans == frozenset()

    def test_a_batch_needs_every_slot_it_spans_to_have_settled(self):
        """One reverting leg would have taken the whole batch with it, so a replacement
        spanning a settled and a reverted slot reverts."""
        entry = valued(
            bid(3, "a", 100, [sell_order("o1"), sell_order("o2", pair=B)], is_winner=False),
            {"o1": 100, "o2": 70},
        )
        entry = ValuedBid(
            bid=entry.bid,
            solution=solution("a", 3, 170, {A: 100, B: 70}),
            valuation=entry.valuation,
        )
        side = side_outcomes(
            [entry],
            outcome_rule="inherited",
            settled={},
            recorded_winner_uids=frozenset({0}),
            inherit={A: True, B: False},
        )
        assert side.inherited_reverts == frozenset({3})
        assert side.orders["o1"].executed is False
        assert side.orders["o2"].executed is False

    def test_a_replacement_holding_a_pair_nobody_won_is_an_orphan(self):
        """Nothing to inherit, so the settlement has to be assumed. This is the mapping
        failure PLAN.md section 5 asks to count rather than hide."""
        side = side_outcomes(
            [self.winner(3, "a", "o1", 100)],
            outcome_rule="inherited",
            settled={},
            recorded_winner_uids=frozenset({0}),
            inherit={B: False},
        )
        assert side.orphans == frozenset({3})
        assert side.orders["o1"].executed is True

    def test_a_recorded_winner_without_a_settlement_row_is_loud(self):
        """Defaulting either way would move baseline surplus and read as a finding."""
        with pytest.raises(MissingSettlementError, match="no settlement outcome"):
            side_outcomes(
                [self.winner(0, "a", "o1", 100)],
                outcome_rule="inherited",
                settled={},
                recorded_winner_uids=frozenset({0}),
            )

    def test_zero_surplus_on_an_executed_order_keeps_the_flag(self):
        side = side_outcomes(
            [self.winner(0, "a", "o1", 0)],
            outcome_rule="assume-settled",
            settled={},
            recorded_winner_uids=frozenset(),
        )
        assert side.orders["o1"].executed is True
        assert side.orders["o1"].surplus_native == 0

    def test_volume_and_partial_flag_ride_along(self):
        entry = valued(
            bid(0, "a", 100, [sell_order("o1")]), {"o1": 100}, volumes={"o1": 2100}
        )
        side = side_outcomes(
            [entry],
            outcome_rule="assume-settled",
            settled={},
            recorded_winner_uids=frozenset(),
        )
        assert side.orders["o1"].volume_native == 2100
        assert side.orders["o1"].partially_fillable is False

    def test_an_unexecuted_order_has_no_volume(self):
        """Like `surplus_native`: an order that does not trade has no received leg."""
        entry = valued(
            bid(0, "a", 100, [sell_order("o1")]), {"o1": 100}, volumes={"o1": 2100}
        )
        side = side_outcomes(
            [entry],
            outcome_rule="inherited",
            settled={0: NOT_SETTLED},
            recorded_winner_uids=frozenset({0}),
        )
        assert side.orders["o1"].volume_native is None

    def test_a_non_contributing_order_is_executed_at_zero_user_surplus(self):
        """A JIT order outside `surplus_capturing_jit_order_owners`: traded, but its
        surplus is the market maker's, not a user's."""
        entry = valued(bid(0, "a", 100, [sell_order("jit")], contributes=False), {})
        side = side_outcomes(
            [entry],
            outcome_rule="assume-settled",
            settled={},
            recorded_winner_uids=frozenset(),
        )
        assert side.orders["jit"].executed is True
        assert side.orders["jit"].contributes is False
        assert side.orders["jit"].surplus_native == 0

    def test_two_winners_cannot_share_an_order(self):
        """`pick_winners` gives winners disjoint pairs and an order fixes its own pair, so
        this is unreachable — asserted rather than assumed."""
        with pytest.raises(AssertionError, match="claimed twice"):
            side_outcomes(
                [self.winner(0, "a", "o1", 100), self.winner(1, "b", "o1", 90)],
                outcome_rule="assume-settled",
                settled={},
                recorded_winner_uids=frozenset(),
            )


class TestDiffOutcomes:
    def outcome(self, surplus: int | None, executed: bool = True) -> OrderOutcome:
        return OrderOutcome(
            executed=executed, surplus_native=surplus, contributes=True, solver="a"
        )

    def test_union_of_both_sides(self):
        diffs = diff_outcomes({"o1": self.outcome(100)}, {"o2": self.outcome(50)})
        assert [d.order_uid for d in diffs] == ["o1", "o2"]

    def test_an_order_only_the_baseline_traded_is_unexecuted_on_the_other_side(self):
        (diff,) = diff_outcomes({"o1": self.outcome(100)}, {})
        assert diff.executed_base and not diff.executed_loo
        assert diff.surplus_base == 100 and diff.surplus_loo is None
        assert diff.only_with_solver and not diff.only_without_solver
        assert diff.delta_surplus == -100  # the counterfactual loses the fill

    def test_delta_substitutes_zero_only_when_aggregating(self):
        (diff,) = diff_outcomes({}, {"o1": self.outcome(70)})
        assert diff.surplus_base is None
        assert diff.delta_surplus == 70
        assert diff.only_without_solver

    def test_an_order_traded_on_both_sides_nets_out(self):
        (diff,) = diff_outcomes({"o1": self.outcome(100)}, {"o1": self.outcome(60)})
        assert diff.delta_surplus == -40
        assert not diff.only_with_solver and not diff.only_without_solver

    def test_contributes_is_true_if_either_side_says_so(self):
        jit = OrderOutcome(
            executed=True, surplus_native=0, contributes=False, solver="a"
        )
        (diff,) = diff_outcomes({"o1": jit}, {"o1": self.outcome(5)})
        assert diff.contributes

    def test_volumes_and_the_partial_flag_are_carried(self):
        base = OrderOutcome(
            executed=True,
            surplus_native=100,
            contributes=True,
            volume_native=1000,
            partially_fillable=True,
        )
        loo = OrderOutcome(
            executed=True, surplus_native=60, contributes=True, volume_native=990
        )
        (diff,) = diff_outcomes({"o1": base}, {"o1": loo})
        assert (diff.volume_base, diff.volume_loo) == (1000, 990)
        # A property of the order, so whichever side saw it decides.
        assert diff.partially_fillable

    def test_an_unexecuted_side_has_no_volume(self):
        base = OrderOutcome(
            executed=True, surplus_native=100, contributes=True, volume_native=1000
        )
        (diff,) = diff_outcomes({"o1": base}, {})
        assert (diff.volume_base, diff.volume_loo) == (1000, None)


class TestFeeDeltas:
    """The protocol-fee side of a counterfactual (M13).

    Deltas are counterfactual minus actual like every other one here, so a **negative**
    `delta_protocol_fees` means the protocol collects less without the solver.
    """

    def outcome(
        self,
        surplus: int | None,
        fee: int | None,
        partner: int | None = 0,
        *,
        executed: bool = True,
        fees_known: bool = True,
    ) -> OrderOutcome:
        return OrderOutcome(
            executed=executed,
            surplus_native=surplus,
            contributes=True,
            solver="a",
            fee_native=fee,
            partner_fee_native=partner,
            fees_known=fees_known,
        )

    def auction(self, diffs: tuple[OrderDiff, ...]) -> AuctionCounterfactual:
        return AuctionCounterfactual(
            auction_id=1, n_solutions=2, solver_present=True, order_diffs=diffs
        )

    def test_fee_lost_with_the_solver_gone(self):
        (diff,) = diff_outcomes(
            {"o1": self.outcome(100, fee=30, partner=10)},
            {"o1": self.outcome(60, fee=18, partner=6)},
        )
        assert diff.delta_protocol_and_partner_fee == -12
        assert diff.delta_partner_fee == -4
        assert diff.delta_protocol_fee == -8

    def test_an_order_that_stops_trading_loses_its_whole_fee(self):
        (diff,) = diff_outcomes({"o1": self.outcome(100, fee=30, partner=10)}, {})
        assert diff.fee_loo is None
        assert diff.delta_protocol_and_partner_fee == -30
        assert diff.delta_protocol_fee == -20

    def test_auction_sums_split_partner_from_dao(self):
        result = self.auction(
            diff_outcomes(
                {"o1": self.outcome(100, fee=30, partner=10)},
                {"o1": self.outcome(60, fee=18, partner=6)},
            )
        )
        assert result.protocol_and_partner_fees_base == 30
        assert result.partner_fees_base == 10
        assert result.protocol_fees_base == 20
        assert result.delta_protocol_fees == -8
        assert result.fees_complete

    def test_an_unpriced_order_makes_the_auction_incomplete(self):
        result = self.auction(
            diff_outcomes(
                {"o1": self.outcome(100, fee=None, partner=None, fees_known=False)},
                {"o1": self.outcome(60, fee=18, partner=6)},
            )
        )
        assert not result.fees_complete

    def test_an_unexecuted_order_is_priced_at_zero_not_unknown(self):
        """An order that did not trade pays nothing, so an unrecorded policy list must
        not take the whole auction out of the fee sums.

        Goes through `side_outcomes` because that is where the distinction is drawn: the
        valuation reports the order as unpriced either way, and only the settlement
        outcome decides whether that matters.
        """
        unpriced = valued(
            bid(0, "a", 100, [sell_order("o1")], is_winner=True),
            {"o1": 100},
            fees_unknown=frozenset({"o1"}),
        )
        failed = side_outcomes(
            [unpriced],
            outcome_rule="inherited",
            settled={0: NOT_SETTLED},
            recorded_winner_uids=frozenset({0}),
        )
        assert failed.orders["o1"].executed is False
        assert failed.orders["o1"].fees_known is True

        landed = side_outcomes(
            [unpriced],
            outcome_rule="assume-settled",
            settled={},
            recorded_winner_uids=frozenset({0}),
        )
        assert landed.orders["o1"].executed is True
        assert landed.orders["o1"].fees_known is False

    def test_incomplete_auctions_are_counted_not_summed(self):
        """Both sides drop together, so the window sum stays like-for-like."""
        analysis = Analysis()
        analysis.add(
            self.auction(
                diff_outcomes(
                    {"o1": self.outcome(100, fee=None, partner=None, fees_known=False)},
                    {"o1": self.outcome(60, fee=18, partner=6)},
                )
            )
        )
        assert analysis.auctions_fees_incomplete == 1
        assert analysis.auctions_fees_counted == 0
        assert analysis.delta_protocol_fees == 0

        analysis.add(
            self.auction(
                diff_outcomes(
                    {"o1": self.outcome(100, fee=30, partner=10)},
                    {"o1": self.outcome(60, fee=18, partner=6)},
                )
            )
        )
        assert analysis.auctions_fees_counted == 1
        assert analysis.delta_protocol_fees == -8
        assert analysis.delta_partner_fees == -4
        assert analysis.delta_protocol_and_partner_fees == -12


class TestRecordedExecutedVolume:
    """The denominator of "averaged over all traded volume": what the recorded winners
    executed, computed through `analyse_auction` whether or not the solver bid."""

    def test_a_reverted_winner_trades_nothing_under_inherited(self):
        auction = bundle([bid(0, "a", 100, [sell_order("o1")], is_winner=True)])
        inherited = analyse_auction(
            auction, WETH, frozenset({"x"}), settled={0: NOT_SETTLED}
        )
        assert inherited.executed_volume_base == 0
        assert inherited.executed_orders_base == 0
        assert inherited.executed_orders_all_base == 0

        assumed = analyse_auction(
            auction, WETH, frozenset({"x"}), outcome_rule="assume-settled"
        )
        assert assumed.executed_volume_base == 2100
        assert assumed.executed_orders_base == 1
        assert assumed.executed_orders_all_base == 1

    def test_partially_fillable_orders_split_the_denominators(self):
        """The price figure's numerator excludes them (their Δsurplus mixes quantity
        into price), so their volume and fill-or-kill count stay out — but an executed
        order is an executed order for the coverage share (D24), so the all-orders
        count sees them."""
        partial = replace(sell_order("o1"), partially_fillable=True)
        result = analyse_auction(
            bundle([bid(0, "a", 100, [partial], is_winner=True)]),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
        )
        assert result.executed_volume_base == 0
        assert result.executed_orders_base == 0
        assert result.executed_orders_all_base == 1

    def test_jit_orders_are_not_user_volume(self):
        result = analyse_auction(
            bundle(
                [bid(0, "a", 100, [sell_order("o1")], is_winner=True, contributes=False)]
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
        )
        assert result.executed_volume_base == 0
        assert result.executed_orders_all_base == 0

    def test_a_non_winning_bid_contributes_nothing(self):
        result = analyse_auction(
            bundle(
                [
                    bid(0, "a", 100, [sell_order("o1")], is_winner=True),
                    bid(1, "b", 90, [sell_order("o1", executed_buy=2050)]),
                ]
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
        )
        assert result.executed_volume_base == 2100

    def test_a_recorded_winner_without_settlement_is_loud_even_when_absent(self):
        """The denominator needs the same settlement coverage the sides do; defaulting
        would silently skew every overall figure."""
        with pytest.raises(MissingSettlementError, match="no settlement outcome"):
            analyse_auction(
                bundle([bid(0, "a", 100, [sell_order("o1")], is_winner=True)]),
                WETH,
                frozenset({"x"}),
                settled={},
            )

    def test_price_suspect_auctions_stay_out_of_the_window_volume(self):
        """A fabricated price fabricates volume too; the suspect exclusion must cover
        the denominator exactly as it covers every other statistic."""
        result = analyse_auction(
            bundle([bid(0, "a", 100, [sell_order("o1")], is_winner=True)]),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
        )
        suspect = replace(result, price_suspect_uids=frozenset({0}))
        analysis = Analysis()
        analysis.add(suspect)
        assert analysis.window_volume_base == 0


class TestAnalyseAuction:
    def test_an_auction_the_solver_skipped_is_still_counted(self):
        result = analyse_auction(
            bundle([bid(0, "a", 100, [sell_order("o1")], is_winner=True)]),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
        )
        assert result.solver_present is False
        assert result.order_diffs == ()
        assert result.delta_surplus == 0
        # The window denominator still sees what this auction traded (D10's logic).
        assert result.executed_volume_base == 2100
        assert result.executed_orders_base == 1

        analysis = Analysis()
        analysis.add(result)
        assert (analysis.auctions, analysis.auctions_with_solver) == (1, 0)
        assert analysis.window_volume_base == 2100
        assert analysis.window_orders_base == 1
        assert analysis.window_order_executions_base == 1

    def test_a_replaced_winner_gives_users_less(self):
        """`x` wins the pair with a better fill; without it the runner-up wins the same
        order at a worse price. The difference is the surplus the user loses."""
        result = analyse_auction(
            bundle(
                [
                    bid(0, "x", 200, [sell_order("o1", executed_buy=2200)], is_winner=True),
                    bid(1, "b", 100, [sell_order("o1", executed_buy=2100)]),
                ]
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
        )
        assert result.solver_won_baseline and result.solver_won_db
        assert result.baseline_winner_uids == frozenset({0})
        assert result.loo_winner_uids == frozenset({1})
        assert result.delta_surplus == -100  # the worse fill: users lose 100
        assert result.replacements_loo == frozenset({1})
        assert result.replacements_base == frozenset()
        # `x`'s batch settled, so the replacement inherits a settled slot.
        assert result.inherited_reverts_loo == frozenset()
        assert result.orphans_loo == frozenset()
        # The received legs at USDC's native price of 1 — the denominator for the
        # relative reading of the same move.
        (diff,) = result.order_diffs
        assert (diff.volume_base, diff.volume_loo) == (2200, 2100)
        assert diff.partially_fillable is False

    def test_a_reverted_slot_stays_reverted_in_the_counterfactual(self):
        """The whole point of the `inherited` rule. `x` won the slot and reverted, so the
        user got nothing. Its replacement inherits that revert, so removing `x` neither
        gains nor loses the user anything — the auction simply produced no fill. A rule
        that attached settlement to the solution instead would credit the replacement
        with settling and book a phantom gain from removing `x`; that rule (`observed`)
        was removed in the M4 review.
        """
        auction = bundle(
            [
                bid(0, "x", 200, [sell_order("o1", executed_buy=2200)], is_winner=True),
                bid(1, "b", 100, [sell_order("o1", executed_buy=2100)]),
            ]
        )
        inherited = analyse_auction(
            auction, WETH, frozenset({"x"}), settled={0: NOT_SETTLED}
        )
        (diff,) = inherited.order_diffs
        assert (diff.executed_base, diff.executed_loo) == (False, False)
        assert inherited.inherited_reverts_loo == frozenset({1})
        assert inherited.delta_surplus == 0

    def test_an_order_executed_only_because_of_the_solver(self):
        """`x` batches two pairs; the only other bid covers one of them, so the DAI order
        goes unexecuted without `x`. This is the coverage question."""
        result = analyse_auction(
            bundle(
                [
                    bid(
                        0,
                        "x",
                        400,
                        [sell_order("o1"), sell_order("o2", pair=B)],
                        is_winner=True,
                    ),
                    bid(1, "b", 100, [sell_order("o1")]),
                ]
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
        )
        saved = [d.order_uid for d in result.order_diffs if d.only_with_solver]
        assert saved == ["o2"]
        assert result.delta_surplus == -100  # o1 nets out, o2's fill is lost entirely

    def test_a_surviving_winner_cancels_even_when_it_failed_to_settle(self):
        """The reason the outcome rule is applied to both sides identically. `b` wins with
        and without `x` and never settled, so it contributes nothing either way — a rule
        applied to one side only would report its whole surplus as a difference."""
        result = analyse_auction(
            bundle(
                [
                    bid(0, "x", 200, [sell_order("o1")], is_winner=True),
                    bid(1, "b", 100, [sell_order("o2", pair=B)], is_winner=True),
                ]
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED, 1: NOT_SETTLED},
        )
        assert result.loo_winner_uids == frozenset({1})
        by_uid = {d.order_uid: d for d in result.order_diffs}
        assert by_uid["o2"].executed_base is False
        assert by_uid["o2"].executed_loo is False
        assert by_uid["o2"].delta_surplus == 0
        assert result.delta_surplus == -100  # o1 alone

    def test_assume_settled_scores_the_reverted_auction_on_proposals_alone(self):
        """The everything-lands-in-time scenario: `x`'s revert is ignored on both sides,
        so the delta is the price difference between the two proposals — the user loses
        the better fill in the counterfactual."""
        result = analyse_auction(
            bundle(
                [
                    bid(0, "x", 200, [sell_order("o1", executed_buy=2200)], is_winner=True),
                    bid(1, "b", 100, [sell_order("o1", executed_buy=2100)]),
                ]
            ),
            WETH,
            frozenset({"x"}),
            outcome_rule="assume-settled",
            settled={0: NOT_SETTLED},
        )
        (diff,) = result.order_diffs
        assert not diff.unsettled_base
        assert result.delta_surplus == -100

    def test_un_filtering_is_reported(self):
        result = analyse_auction(
            bundle(
                [
                    bid(0, "x", 100, [sell_order("o1", executed_buy=2100)], is_winner=True),
                    bid(
                        1,
                        "batch",
                        130,
                        [
                            sell_order("o1", executed_buy=2060),
                            sell_order("o2", pair=B, executed_buy=2070),
                        ],
                        filtered_out=True,
                    ),
                ]
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
        )
        assert result.un_filtered_uids == frozenset({1})
        assert result.un_filtered_winner_uids == frozenset({1})
        assert result.filter_relaxed

    def test_a_valuation_failure_abandons_the_auction(self):
        """A fill below the limit price cannot be valued, and the Rust discards the whole
        solution. M1 measured zero of these over three days, so it is surfaced rather
        than tolerated."""
        result = analyse_auction(
            bundle([bid(0, "x", 100, [sell_order("o1", executed_buy=1900)])]),
            WETH,
            frozenset({"x"}),
        )
        assert result.valuation_failures
        assert result.order_diffs == ()

        analysis = Analysis()
        analysis.add(result)
        assert analysis.auctions_skipped == 1
        assert analysis.auctions_with_solver == 0


class TestRetention:
    """Which auctions `Analysis` keeps rather than merely counting.

    Δsurplus only moves when the winner set does, but a reward moves on a reference score
    alone, so keeping only `winner_set_changed` would hand M3 a set missing most of the
    auctions whose rewards actually change.
    """

    def result(self, **kwargs: Any):
        from loo.counterfactual import AuctionCounterfactual

        defaults: dict[str, Any] = dict(auction_id=1, n_solutions=2, solver_present=True)
        defaults.update(kwargs)
        return AuctionCounterfactual(**defaults)

    def test_an_unchanged_auction_is_only_counted(self):
        analysis = Analysis()
        analysis.add(self.result())
        assert analysis.changed == []

    def test_a_moved_reference_score_alone_is_kept(self):
        """The removed solver's *non-winning* solution can be a winner of the without-`s`
        pick inside `compute_reference_scores`, so `s`'s reference score falls when the
        solver goes even though no win changes hands."""
        result = self.result(
            baseline_reference_scores={"a": 100},
            loo_reference_scores={"a": 90},
        )
        assert not result.winner_set_changed
        assert result.reference_scores_moved
        analysis = Analysis()
        analysis.add(result)
        assert analysis.changed == [result]


class TestAnalysis:
    def diff(self, **kwargs: Any) -> OrderDiff:
        defaults: dict[str, Any] = dict(
            order_uid="o1",
            contributes=True,
            executed_base=True,
            executed_loo=True,
            surplus_base=100,
            surplus_loo=100,
        )
        defaults.update(kwargs)
        return OrderDiff(**defaults)

    def test_jit_orders_are_counted_apart_from_user_orders(self):
        analysis = Analysis()
        analysis.add(
            AuctionCounterfactual(
                auction_id=1,
                n_solutions=2,
                solver_present=True,
                order_diffs=(
                    self.diff(order_uid="user", executed_loo=False, surplus_loo=None),
                    self.diff(
                        order_uid="jit",
                        contributes=False,
                        executed_loo=False,
                        surplus_loo=None,
                    ),
                ),
            )
        )
        assert analysis.orders_only_with_solver == 1
        assert analysis.jit_orders_only_with_solver == 1
        assert analysis.orders_compared == 1
        assert analysis.delta_surplus == -100  # the JIT order's surplus is not the user's


class TestJointRemoval:
    """Several solvers in `removed` leave in one re-arbitration: the joint
    counterfactual, with nothing attributed to individual members."""

    def test_all_removed_solvers_leave_in_one_arbitration(self):
        result = analyse_auction(
            bundle(
                [
                    bid(0, "x", 300, [sell_order("o1", executed_buy=2300)], is_winner=True),
                    bid(1, "y", 200, [sell_order("o1", executed_buy=2200)]),
                    bid(2, "z", 100, [sell_order("o1", executed_buy=2100)]),
                ]
            ),
            WETH,
            frozenset({"x", "y"}),
            settled={0: SETTLED},
        )
        # The runner-up leaves with the winner, so the third solution wins: the fill
        # degrades by 200 atoms, not the 100 a single removal of "x" would cost.
        assert result.loo_winner_uids == frozenset({2})
        assert result.delta_surplus == -200
        assert result.removed_present == frozenset({"x", "y"})
        assert result.solver_won_db and result.solver_won_baseline

    def test_any_present_member_makes_the_auction_count_and_is_recorded(self):
        result = analyse_auction(
            bundle(
                [
                    bid(0, "x", 200, [sell_order("o1", executed_buy=2200)], is_winner=True),
                    bid(1, "y", 100, [sell_order("o1", executed_buy=2100)]),
                ]
            ),
            WETH,
            frozenset({"y", "ghost"}),
            settled={0: SETTLED},
        )
        assert result.solver_present
        assert result.removed_present == frozenset({"y"})

        analysis = Analysis(addresses=frozenset({"y", "ghost"}))
        analysis.add(result)
        # `addresses - addresses_seen` is the dead weight the CLI refuses to carry.
        assert analysis.addresses_seen == frozenset({"y"})


# --- the quote side reaching into the fee side ---------------------------------------

PRICE_IMPROVEMENT = Policy(
    kind="priceimprovement",
    price_improvement_factor=0.5,
    price_improvement_max_volume_factor=0.1,
)
RECORDED = Quote(sell_amount=1000, buy_amount=2050, fee=0)
WORSE = Quote(sell_amount=1000, buy_amount=2020, fee=0)
BETTER = Quote(sell_amount=1000, buy_amount=2200, fee=0)


def quoted_bundle(
    bids: list[Bid],
    quotes: dict[str, Quote],
    *,
    auction_id: int = 1,
    policies: dict[str, tuple[Policy, ...]] | None = None,
    known_extra: tuple[str, ...] = (),
) -> AuctionBundle:
    """A bundle whose fee context charges a price-improvement fee on every quoted
    order. `sell_order` executes 1,000 for 2,100 against a 2,000 limit, so the
    improvement over `RECORDED` is 2,100 − 2,050 = 50 and over `WORSE` 80 — at a 50%
    factor (re-expressed as 1.0) the fees are 50 and 80, in USDC atoms at a native
    price of one, and the recharge is +30."""
    # WETH at twice USDC's price, so the 1,000 -> 2,100 fill values the same on both
    # legs and the D14 price-imbalance guard has nothing to exclude.
    return AuctionBundle(
        auction_id=auction_id,
        jit_owners=frozenset(),
        native_prices={USDC: ONE, WETH: 2 * ONE},
        bids=tuple(bids),
        reference_scores={},
        fee_context=FeeContext(
            policies=(
                policies
                if policies is not None
                else {uid: (PRICE_IMPROVEMENT,) for uid in quotes}
            ),
            quotes=quotes,
            partner_counts={},
            known=frozenset(quotes) | frozenset(known_extra),
        ),
    )


class TestQuoteSubstitution:
    """The quote side reaching into the fee side (D28), in every auction that executed
    a quoted order whether or not the solver bid there (D33), with the two limit checks
    that license holding executions and ranking fixed (D34)."""

    def test_the_fee_arithmetic_the_class_rests_on(self):
        """Straight from `fees.py`, so every delta below can be read off by hand."""
        from loo.fees import order_fees

        context = quoted_bundle([], {"o1": RECORDED}).fee_context
        recorded = order_fees(sell_order("o1"), context)
        replaced = order_fees(sell_order("o1"), replace(context, quotes={"o1": WORSE}))
        assert recorded is not None and recorded.total == 50
        assert replaced is not None and replaced.total == 80

    def test_a_recharged_fee_reaches_the_delta_in_an_auction_the_solver_never_bid_in(self):
        """The quote winner often does not bid on its own order; the fee reference
        moved all the same, and before D33 this auction returned all-zero deltas."""
        result = analyse_auction(
            quoted_bundle(
                [bid(0, "a", 100, [sell_order("o1")], is_winner=True)], {"o1": RECORDED}
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
            counterfactual_quotes={"o1": WORSE},
        )
        assert result.solver_present is False
        assert result.quote_carried_uids == frozenset({"o1"})
        assert result.quote_substituted_uids == frozenset({"o1"})
        assert result.baseline_winner_uids == result.loo_winner_uids == frozenset({0})
        assert result.delta_surplus == 0
        # With the recorded quotes on both sides nothing differs here; the recharge is
        # the quote's own figure, per order and in total.
        assert result.delta_protocol_fees == 0
        assert result.quote_fee_recharges == {"o1": 30}
        assert result.quote_fee_recharge == result.quote_fee_recharge_protocol == 30
        assert result.quote_fee_recharge_partner == 0
        assert result.quote_limit_unclearable_uids == frozenset()
        assert result.quote_shift_winner_set_changed is False
        # Retained: the per-auction moves are what `compare` converts to USD.
        assert result.anything_moved

        analysis = Analysis()
        analysis.add(result)
        assert (analysis.auctions, analysis.auctions_with_solver) == (1, 0)
        assert analysis.auctions_quote_only == 1
        assert analysis.auctions_quote_checked == 1
        assert analysis.auctions_quote_substituted == 1
        assert analysis.quote_substituted_orders == 1
        assert analysis.delta_protocol_fees == 0
        assert analysis.quote_fee_recharge == analysis.quote_fee_recharge_protocol == 30
        # Nothing of this auction enters the fee levels: the solver never bid here.
        assert analysis.protocol_fees_base == 0
        assert analysis.surplus_base == 0
        assert analysis.changed == [result]

    def test_the_baseline_keeps_the_recorded_quote_and_the_counterfactual_the_replacement(self):
        """`x` bid and lost; removing it changes no winner. The whole fee delta is the
        recharge, on the counterfactual side only."""
        result = analyse_auction(
            quoted_bundle(
                [
                    bid(0, "a", 200, [sell_order("o1", executed_buy=2100)], is_winner=True),
                    bid(1, "x", 100, [sell_order("o1", executed_buy=2090)]),
                ],
                {"o1": RECORDED},
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
            counterfactual_quotes={"o1": WORSE},
        )
        assert result.solver_present is True
        assert result.baseline_winner_uids == result.loo_winner_uids == frozenset({0})
        assert result.protocol_and_partner_fees_base == 50
        assert result.protocol_and_partner_fees_loo == 50  # recorded quote on both sides
        assert result.quote_fee_recharge == 30
        assert result.delta_surplus == 0

        analysis = Analysis()
        analysis.add(result)
        assert (analysis.auctions_with_solver, analysis.auctions_quote_only) == (1, 0)
        assert analysis.quote_substituted_orders == 1
        assert (analysis.delta_protocol_fees, analysis.quote_fee_recharge) == (0, 30)

    def test_a_carried_quote_without_a_price_improvement_policy_recharges_nothing(self):
        result = analyse_auction(
            quoted_bundle(
                [bid(0, "a", 100, [sell_order("o1")], is_winner=True)],
                {"o1": RECORDED},
                policies={"o1": ()},
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
            counterfactual_quotes={"o1": WORSE},
        )
        assert result.quote_carried_uids == frozenset({"o1"})
        assert result.quote_substituted_uids == frozenset()
        assert result.delta_protocol_fees == 0 and result.quote_fee_recharge == 0
        assert not result.anything_moved

    def test_an_unusable_replacement_drops_out_alone(self):
        """A replacement whose fee exceeds its own sell amount cannot be charged
        against; it keeps the recorded quote while the other order is still
        recharged, rather than the whole auction reading as unpriced."""
        result = analyse_auction(
            quoted_bundle(
                [bid(0, "a", 100, [sell_order("o1"), sell_order("o2")], is_winner=True)],
                {"o1": RECORDED, "o2": RECORDED},
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
            counterfactual_quotes={
                "o1": WORSE,
                "o2": Quote(sell_amount=1000, buy_amount=2020, fee=2000),
            },
        )
        assert result.quote_carried_uids == frozenset({"o1"})
        assert result.quote_substituted_uids == frozenset({"o1"})
        assert result.fees_complete
        assert result.quote_fee_recharges == {"o1": 30}

    def test_a_better_replacement_can_tighten_the_limit_past_the_execution(self):
        """The one direction in which holding the execution fixed is not free: a
        class flip's replacement may quote more, and the user would then have signed
        a limit the recorded fill no longer clears. Counted, not modelled."""
        result = analyse_auction(
            quoted_bundle(
                [bid(0, "a", 100, [sell_order("o1")], is_winner=True)], {"o1": RECORDED}
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
            counterfactual_quotes={"o1": BETTER},
        )
        assert result.quote_substituted_uids == frozenset({"o1"})
        # 2,000 x 2200/2050 = 2,146 required against 2,100 received.
        assert result.quote_limit_unclearable_uids == frozenset({"o1"})
        # No improvement over a quote above the fill: the recharge is the whole fee, lost.
        assert result.quote_fee_recharge == -50

        analysis = Analysis()
        analysis.add(result)
        assert analysis.quote_limit_unclearable_orders == 1

    def test_a_weaker_limit_that_would_flip_the_ranking_is_counted_not_applied(self):
        """`a` loses the pair to `b` by five. Had `o1` been signed against a quote of
        1,000 instead of 2,050, the mechanism would have measured ~1,024 more surplus
        on `a`'s fill and `a` would have won. Scores are never recomputed (D1); the
        auction is flagged so the design's assumption is tested rather than held."""
        result = analyse_auction(
            quoted_bundle(
                [
                    bid(0, "a", 100, [sell_order("o1")]),
                    bid(1, "b", 105, [sell_order("o3")], is_winner=True),
                ],
                {"o1": RECORDED},
                known_extra=("o3",),
            ),
            WETH,
            frozenset({"x"}),
            settled={1: SETTLED},
            counterfactual_quotes={"o1": Quote(sell_amount=1000, buy_amount=1000, fee=0)},
        )
        assert result.baseline_winner_uids == result.loo_winner_uids == frozenset({1})
        assert result.quote_carried_uids == frozenset({"o1"})
        # `o1` sits in the losing bid: nothing was recharged, nothing moved...
        assert result.quote_substituted_uids == frozenset()
        assert not result.anything_moved
        # ...but the ranking channel is live here, and says so.
        assert result.quote_shift_winner_set_changed is True

        analysis = Analysis()
        analysis.add(result)
        assert analysis.auctions_quote_checked == 1
        assert analysis.auctions_quote_shift_would_change_winners == 1
        assert analysis.quote_shift_auctions == [1]
        assert analysis.changed == []

    def test_an_absent_auction_with_no_carried_order_is_still_skipped(self):
        result = analyse_auction(
            quoted_bundle(
                [bid(0, "a", 100, [sell_order("o1")], is_winner=True)], {"o1": RECORDED}
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
            counterfactual_quotes={"elsewhere": WORSE},
        )
        assert result.solver_present is False
        assert result.order_diffs == ()
        analysis = Analysis()
        analysis.add(result)
        assert analysis.auctions_quote_checked == 0

    def test_an_order_recharged_in_two_auctions_is_one_order(self):
        analysis = Analysis()
        for auction_id in (1, 2):
            analysis.add(
                analyse_auction(
                    quoted_bundle(
                        [bid(0, "a", 100, [sell_order("o1")], is_winner=True)],
                        {"o1": RECORDED},
                        auction_id=auction_id,
                    ),
                    WETH,
                    frozenset({"x"}),
                    settled={0: SETTLED},
                    counterfactual_quotes={"o1": WORSE},
                )
            )
        assert analysis.auctions_quote_substituted == 2
        assert analysis.quote_substituted_orders == 1
        assert analysis.quote_fee_recharge == 60
