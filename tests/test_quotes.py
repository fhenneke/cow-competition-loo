"""The quote competition's pure parts: the pick, the two rankings, and the diff.

Everything here runs without a database. The parts that cannot — the ClickHouse ladder,
the order match — are validated against the record instead, by `validate_picks` on a real
window; a unit test cannot tell us whether the rule is the protocol's.
"""

from __future__ import annotations

from typing import Any

import pytest

from loo.fees import Quote
from loo.quotes import (
    BUY_AMOUNT_TOLERANCE,
    Estimate,
    MatchedQuote,
    Pricing,
    QuoteAnalysis,
    QuoteRequest,
    RecordedQuote,
    UnpricedRequestError,
    counterfactual_quote,
    diff_quote,
    effective_sell_amount,
    leave_one_out,
    match_orders,
    pick,
    price_suspect_orders,
    rank_key,
    validate_picks,
    viable_requests,
)

# One gas unit costs 2 sell-token atoms: gas_price 200, sell_token_price 100.
PRICING = Pricing(gas_price=200.0, sell_token_price=100.0)


def estimate(name: str, out: float, gas: float, verified: bool = True, winner: bool = False):
    return Estimate(
        estimator=name, out_amount=out, gas=gas, verified=verified, is_winner=winner
    )


def sell_request(*estimates: Estimate, in_amount: float = 1_000_000.0, winner: str = ""):
    return QuoteRequest(
        request_id="r1",
        timestamp="2026-07-25 12:00:00.000",
        kind="sell",
        sell_token="aa",
        buy_token="bb",
        in_amount=in_amount,
        winner=winner,
        estimates=estimates,
    )


class TestReasonable:
    """`is_reasonable` (competition/quote.rs:191) — a zero on either field means the
    estimate never entered the comparison, so it cannot win one either."""

    def test_zero_gas_and_zero_out_are_both_unusable(self):
        assert not estimate("a", 100, 0).reasonable
        assert not estimate("a", 0, 100).reasonable
        assert estimate("a", 100, 1).reasonable

    def test_pick_ignores_unreasonable_estimates_even_when_they_are_best(self):
        winner = pick(
            [estimate("zero-gas", 999_999, 0), estimate("real", 100, 10)],
            is_sell=True,
            in_amount=1_000.0,
            pricing=PRICING,
        )
        assert winner is not None and winner.estimator == "real"

    def test_pick_returns_none_when_nothing_is_usable(self):
        assert (
            pick([estimate("a", 0, 0)], is_sell=True, in_amount=1.0, pricing=PRICING)
            is None
        )


class TestVerifiedClass:
    """The hard first key: a verified estimate beats an unverified one at any amount.

    Measured on 5,041 requests carrying both classes, where a verified estimate won all
    5,041 — so this is the protocol's rule, not a tie-break.
    """

    def test_verified_beats_a_much_better_unverified_quote(self):
        winner = pick(
            [
                estimate("greedy", 2_000_000, 1, verified=False),
                estimate("verified", 1_000, 1, verified=True),
            ],
            is_sell=True,
            in_amount=1_000_000.0,
            pricing=PRICING,
        )
        assert winner is not None and winner.estimator == "verified"

    def test_class_only_applies_when_a_verified_estimate_exists(self):
        winner = pick(
            [
                estimate("high", 2_000, 1, verified=False),
                estimate("low", 1_000, 1, verified=False),
            ],
            is_sell=True,
            in_amount=1_000_000.0,
            pricing=PRICING,
        )
        assert winner is not None and winner.estimator == "high"


class TestRanking:
    def test_gas_can_beat_a_higher_amount(self):
        """The whole reason the gross ladder is not the competition: a costlier route
        loses despite quoting more."""
        cheap = estimate("cheap", 1_000_000, 100)
        greedy = estimate("greedy", 1_002_000, 20_000)
        winner = pick([greedy, cheap], is_sell=True, in_amount=1_000_000.0, pricing=PRICING)
        assert winner is not None and winner.estimator == "cheap"

    def test_buy_orders_minimise_the_cost(self):
        """A buy order's `out_amount` is what the user pays, so less is better — and the
        key is negated so callers can still use `max`."""
        cheap = estimate("cheap", 900_000, 100)
        dear = estimate("dear", 1_000_000, 100)
        winner = pick([dear, cheap], is_sell=False, in_amount=1_000_000.0, pricing=PRICING)
        assert winner is not None and winner.estimator == "cheap"
        assert rank_key(
            cheap, is_sell=False, in_amount=1.0, pricing=PRICING, ranking="effective-sell"
        ) > rank_key(
            dear, is_sell=False, in_amount=1.0, pricing=PRICING, ranking="effective-sell"
        )

    def test_effective_sell_amount_nets_the_gas_cost_proportionally(self):
        """`(in − gas x c)/in x out`: 1,000 gas at 2 atoms each is 2,000 of a 1,000,000
        input, so the quote is netted by 0.2%."""
        value = effective_sell_amount(
            estimate("a", 500_000, 1_000),
            is_sell=True,
            in_amount=1_000_000.0,
            pricing=PRICING,
        )
        assert value == pytest.approx(500_000 * (1 - 0.002))

    def test_buy_order_effective_amount_adds_the_cost(self):
        value = effective_sell_amount(
            estimate("a", 500_000, 1_000),
            is_sell=False,
            in_amount=1_000_000.0,
            pricing=PRICING,
        )
        assert value == pytest.approx(502_000)

    def test_autopilot_ranking_needs_the_out_token_price_on_sell_orders(self):
        with pytest.raises(UnpricedRequestError):
            rank_key(
                estimate("a", 1, 1),
                is_sell=True,
                in_amount=1.0,
                pricing=PRICING,
                ranking="autopilot",
            )

    def test_autopilot_and_effective_sell_agree_on_buy_orders(self):
        """A buy order's out token *is* the sell token, so the two rules are the same
        rule there — not an approximation of one another."""
        estimates = [estimate("a", 1_000_000, 500), estimate("b", 1_001_000, 100)]
        for e in estimates:
            assert rank_key(
                e, is_sell=False, in_amount=1.0, pricing=PRICING, ranking="autopilot"
            ) == rank_key(
                e, is_sell=False, in_amount=1.0, pricing=PRICING, ranking="effective-sell"
            )

    def test_autopilot_shifts_where_effective_sell_scales(self):
        """The two differ on sell orders only through the quote's own pricing error, so
        a deliberately wrong out-token price flips the pick."""
        priced = Pricing(gas_price=200.0, sell_token_price=100.0, buy_token_price=1.0)
        estimates = [estimate("low-gas", 1_000_000, 100), estimate("high-gas", 1_010_000, 5_000)]
        assert pick(
            estimates, is_sell=True, in_amount=1_000_000.0, pricing=priced,
            ranking="effective-sell",
        ) == estimates[1]
        assert pick(
            estimates, is_sell=True, in_amount=1_000_000.0, pricing=priced,
            ranking="autopilot",
        ) == estimates[0]


class TestLeaveOneOut:
    def test_removing_a_loser_never_moves_the_winner(self):
        """Follows from `argmax`, and it is what makes 88% of a solver's appearances a
        no-op — worth asserting rather than assuming."""
        best = estimate("best", 1_000_000, 100)
        request = sell_request(best, estimate("mid", 900_000, 100), estimate("worst", 1, 100))
        for removed in ("mid", "worst"):
            assert (
                leave_one_out(request, frozenset({removed}), pricing=PRICING) == best
            )

    def test_removing_a_loser_cannot_change_the_winners_class(self):
        """A removed unverified estimate cannot make a verified winner unverified."""
        best = estimate("best", 1_000, 100, verified=True)
        request = sell_request(best, estimate("loud", 9_999_999, 100, verified=False))
        assert leave_one_out(request, frozenset({"loud"}), pricing=PRICING) == best

    def test_removing_the_winner_promotes_the_runner_up(self):
        request = sell_request(
            estimate("best", 1_000_000, 100), estimate("second", 999_000, 100)
        )
        replacement = leave_one_out(request, frozenset({"best"}), pricing=PRICING)
        assert replacement is not None and replacement.estimator == "second"

    def test_removing_the_only_estimator_leaves_no_quote(self):
        request = sell_request(estimate("only", 1_000_000, 100))
        assert leave_one_out(request, frozenset({"only"}), pricing=PRICING) is None

    def test_removing_the_only_verified_estimator_flips_the_class(self):
        """The one case where a removal can *raise* the nominal amount: the class rule
        was holding a better-priced unverified quote back."""
        request = sell_request(
            estimate("verified", 1_000, 100, verified=True),
            estimate("unverified", 2_000_000, 100, verified=False),
        )
        replacement = leave_one_out(request, frozenset({"verified"}), pricing=PRICING)
        assert replacement is not None and replacement.estimator == "unverified"


class TestCounterfactualQuote:
    def test_sell_request_amounts_keep_their_sides(self):
        quote = counterfactual_quote(
            estimate("a", 990.0, 1_000),
            is_sell=True,
            in_amount=1_000_000.0,
            pricing=PRICING,
        )
        assert quote == Quote(sell_amount=1_000_000, buy_amount=990, fee=2_000)

    def test_buy_request_swaps_them(self):
        quote = counterfactual_quote(
            estimate("a", 1_010.0, 1_000),
            is_sell=False,
            in_amount=1_000_000.0,
            pricing=PRICING,
        )
        assert quote == Quote(sell_amount=1_010, buy_amount=1_000_000, fee=2_000)

    def test_amounts_round_rather_than_floor(self):
        """Flooring a `Float64` ladder amount would bias every counterfactual quote down
        by up to an atom, and so bias every fee delta up."""
        quote = counterfactual_quote(
            estimate("a", 100.7, 0.4), is_sell=True, in_amount=10.6, pricing=PRICING
        )
        assert quote == Quote(sell_amount=11, buy_amount=101, fee=1)


def recorded(
    *,
    kind: str = "sell",
    sell: int = 1_000_000,
    buy: int = 500_000,
    fee: int = 2_000,
    executed: int | None = None,
) -> RecordedQuote:
    """A recorded order. `executed` defaults to the full sell amount, since an order that
    never traded weighs nothing and would make every weighted assertion vacuous."""
    return RecordedQuote(
        app_code="test",
        executed_sell=sell if executed is None else executed,
        order_uid="ab" * 28,
        created="2026-07-25 12:00:30.000",
        kind=kind,
        partially_fillable=False,
        sell_token="aa",
        buy_token="bb",
        order_sell_amount=sell,
        order_buy_amount=buy,
        quote_solver="cc" * 20,
        quote=Quote(sell_amount=sell, buy_amount=buy, fee=fee),
        pricing=PRICING,
        verified=True,
    )


class TestDiff:
    def test_an_untouched_winner_leaves_everything_unaffected(self):
        order = recorded()
        request = sell_request(
            estimate("winner", 500_000, 1_000, winner=True),
            estimate("other", 400_000, 1_000),
            winner="winner",
        )
        diff = diff_quote(
            MatchedQuote(order, request, "corroborated"), frozenset({"other"})
        )
        assert diff.leaf == "unaffected"
        assert diff.delta_bps == 0.0
        assert diff.quote_loo is None

    def test_a_degraded_quote_is_negative_and_carries_a_replacement_quote(self):
        order = recorded()
        request = sell_request(
            estimate("winner", 500_000, 1_000, winner=True),
            estimate("second", 499_000, 1_000),
            winner="winner",
        )
        diff = diff_quote(
            MatchedQuote(order, request, "corroborated"), frozenset({"winner"})
        )
        assert diff.leaf == "degraded"
        assert diff.replacement == "second"
        assert diff.delta_bps < 0
        assert diff.quote_loo == Quote(sell_amount=1_000_000, buy_amount=499_000, fee=2_000)

    def test_the_baseline_is_the_recorded_quote_not_the_ladder_winner(self):
        """Q-D5: the ladder supplies the counterfactual, never the baseline. Here the
        ladder's winner disagrees with the record, and the delta is measured against the
        record all the same."""
        order = recorded(buy=500_000)
        request = sell_request(
            estimate("winner", 400_000, 1_000, winner=True),
            estimate("second", 399_000, 1_000),
            winner="winner",
        )
        diff = diff_quote(
            MatchedQuote(order, request, "corroborated"), frozenset({"winner"})
        )
        base = 500_000 * (1 - 2_000 / 1_000_000)
        assert diff.effective_base == pytest.approx(base)

    def test_no_quote_when_the_removed_solver_was_the_only_estimator(self):
        order = recorded()
        request = sell_request(
            estimate("only", 500_000, 1_000, winner=True), winner="only"
        )
        diff = diff_quote(
            MatchedQuote(order, request, "corroborated"), frozenset({"only"})
        )
        assert diff.leaf == "no_quote"
        assert diff.quote_loo is None
        # A vanished order has no price change to report; its loss is a count.
        assert diff.delta_bps == 0.0

    def test_a_class_flip_stays_out_of_the_bps_figures(self):
        """A flip can read as a large *improvement* in bps while being worse in kind, so
        it is counted and sized, never averaged in."""
        analysis = QuoteAnalysis(
            solver="X", ranking="effective-sell", window_orders=10,
            window_volume_native=1_000_000,
        )
        order = recorded()
        request = sell_request(
            estimate("w", 500_000, 1_000, verified=True, winner=True),
            estimate("loud", 600_000, 1_000, verified=False),
            winner="w",
        )
        analysis.add(diff_quote(MatchedQuote(order, request, "corroborated"), frozenset({"w"})))
        assert analysis.class_flips == 1
        assert analysis.flip_volume_native == order.volume_native
        assert analysis.degraded == 0
        assert analysis.window_bps == 0.0
        assert analysis.conditional_bps is None

    def test_a_class_flip_is_its_own_leaf(self):
        order = recorded()
        request = sell_request(
            estimate("winner", 500_000, 1_000, verified=True, winner=True),
            estimate("loud", 600_000, 1_000, verified=False),
            winner="winner",
        )
        diff = diff_quote(
            MatchedQuote(order, request, "corroborated"), frozenset({"winner"})
        )
        assert diff.leaf == "class_flip"
        # The nominal quote improves while the quality falls, which is exactly why this
        # is counted apart from the bps figure rather than netted into it.
        assert diff.delta_bps > 0

    def test_a_buy_order_degrades_when_the_cost_rises(self):
        order = recorded(kind="buy", sell=1_000_000, buy=500_000)
        request = QuoteRequest(
            request_id="r1",
            timestamp="2026-07-25 12:00:00.000",
            kind="buy",
            sell_token="aa",
            buy_token="bb",
            in_amount=500_000.0,
            winner="winner",
            estimates=(
                estimate("winner", 1_000_000, 1_000, winner=True),
                estimate("second", 1_010_000, 1_000),
            ),
        )
        diff = diff_quote(
            MatchedQuote(order, request, "corroborated"), frozenset({"winner"})
        )
        assert diff.leaf == "degraded"
        # Both sides are costs in sell atoms and are stored as such — the recorded
        # 1,000,000 + 2,000 fee against the replacement's 1,010,000 + 2,000 — and the
        # bps figure is the change in the *rate* shown (buy per sell), so the ratio is
        # base cost over replacement cost: the same bounded measure a sell order gets.
        assert (diff.effective_base, diff.effective_loo) == (1_002_000, 1_012_000)
        assert diff.delta_bps == pytest.approx((1_002_000 / 1_012_000 - 1) * 10_000)
        assert diff.quote_loo == Quote(sell_amount=1_010_000, buy_amount=500_000, fee=2_000)


class TestAnalysis:
    def build(self) -> QuoteAnalysis:
        analysis = QuoteAnalysis(
            solver="X",
            ranking="effective-sell",
            window_orders=10,
            window_volume_native=1_000_000,
        )
        return analysis

    def test_unaffected_orders_count_only_towards_the_denominator(self):
        analysis = self.build()
        order = recorded()
        request = sell_request(
            estimate("w", 500_000, 1_000, winner=True), winner="w"
        )
        analysis.add(diff_quote(MatchedQuote(order, request, "corroborated"), frozenset({"z"})))
        assert analysis.matched == 1
        assert analysis.quote_wins == 0
        assert analysis.window_bps == 0.0

    def test_window_and_conditional_readings_use_different_denominators(self):
        analysis = self.build()
        order = recorded()
        request = sell_request(
            estimate("w", 500_000, 1_000, winner=True),
            estimate("s", 495_000, 1_000),
            winner="w",
        )
        diff = diff_quote(MatchedQuote(order, request, "corroborated"), frozenset({"w"}))
        analysis.add(diff)
        assert analysis.degraded == 1
        window, conditional = analysis.window_bps, analysis.conditional_bps
        assert window is not None and conditional is not None
        # The order's volume is executed_sell x sell_token_price = 1,000,000 x 100, a
        # hundred times the 1,000,000 the analysis was told the window traded, so the
        # window reading is a hundred times the conditional one: the two are formed
        # from different sums, and the conditional one is just this order's own bps.
        assert conditional == pytest.approx(diff.delta_bps)
        assert window == pytest.approx(conditional * 100)

    def test_no_quote_share_is_over_every_order_the_window_created(self):
        analysis = self.build()
        order = recorded()
        request = sell_request(estimate("w", 500_000, 1_000, winner=True), winner="w")
        analysis.add(diff_quote(MatchedQuote(order, request, "corroborated"), frozenset({"w"})))
        assert analysis.no_quote == 1
        assert analysis.no_quote_share == 0.1

    def test_only_corroborated_matches_may_move_a_fee(self):
        """A heuristic match would put a different request's runner-up against the
        recorded winner's amount and call the difference a fee."""
        analysis = self.build()
        order = recorded()
        request = sell_request(
            estimate("w", 500_000, 1_000, winner=True),
            estimate("s", 495_000, 1_000),
            winner="w",
        )
        analysis.add(diff_quote(MatchedQuote(order, request, "heuristic"), frozenset({"w"})))
        assert analysis.degraded_all == 1
        # ...and it stays out of the headline sums as well as out of the fee path.
        assert analysis.degraded == 0
        assert analysis.window_bps == 0.0
        assert analysis.window_bps_all_tiers is not None
        assert analysis.window_bps_all_tiers < 0
        assert analysis.counterfactual_quotes == {}


class TestMatching:
    def request_at(self, timestamp: str, in_amount: float, winner_out: float = 500_000.0):
        return QuoteRequest(
            request_id=f"r-{timestamp}",
            timestamp=timestamp,
            kind="sell",
            sell_token="aa",
            buy_token="bb",
            in_amount=in_amount,
            winner="w",
            estimates=(estimate("w", winner_out, 1_000, winner=True),),
        )

    def match(
        self,
        orders: list[RecordedQuote],
        requests: list[QuoteRequest],
    ) -> tuple[list[MatchedQuote], dict[str, str]]:
        """The two-phase path the loader uses: filter on time, then corroborate."""
        return match_orders(orders, viable_requests(orders, requests))

    def test_a_corroborating_amount_wins_over_a_closer_time(self):
        """The identity the matcher never used decides, because it is the only evidence
        outside the heuristic itself."""
        order = recorded(buy=500_000)
        wrong_amount_but_closer = self.request_at(
            "2026-07-25 12:00:29.000", 1_000_000.0, winner_out=123.0
        )
        corroborating = self.request_at("2026-07-25 12:00:00.000", 1_000_000.0)
        matches, _ = self.match([order], [wrong_amount_but_closer, corroborating])
        assert len(matches) == 1
        assert matches[0].tier == "corroborated"
        assert matches[0].request.request_id == corroborating.request_id

    def test_a_request_outside_the_time_window_is_not_a_match(self):
        order = recorded()
        stale = self.request_at("2026-07-25 11:00:00.000", 1_000_000.0)
        assert self.match([order], [stale])[0] == []

    def test_a_request_after_creation_matches_within_the_slack(self):
        """A placement-time re-quote lands just after the order exists."""
        order = recorded()
        late = self.request_at("2026-07-25 12:01:00.000", 1_000_000.0)
        assert len(self.match([order], [late])[0]) == 1

    def test_a_buy_orders_amount_matches_within_the_slippage_tolerance(self):
        order = recorded(kind="buy", sell=1_000_000, buy=500_000)
        near = QuoteRequest(
            request_id="r1",
            timestamp="2026-07-25 12:00:00.000",
            kind="buy",
            sell_token="aa",
            buy_token="bb",
            in_amount=500_000.0 * (1 + BUY_AMOUNT_TOLERANCE / 2),
            winner="w",
            estimates=(estimate("w", 1_000_000, 1_000, winner=True),),
        )
        assert len(self.match([order], [near])[0]) == 1

    def test_the_time_filter_runs_before_any_ladder_is_loaded(self):
        """The reason matching is two-phase: a popular round amount matches every request
        that ever asked for it — 1.9 M over a ten-day mainnet window — and only the time
        filter cuts that to something a `request_id in (…)` scan can afford."""
        order = recorded()
        near = self.request_at("2026-07-25 12:00:00.000", 1_000_000.0)
        far = self.request_at("2026-07-25 06:00:00.000", 1_000_000.0)
        wrong_amount = self.request_at("2026-07-25 12:00:00.000", 999.0)
        viable = viable_requests([order], [near, far, wrong_amount])
        assert [r.request_id for r in viable[order.order_uid]] == [near.request_id]

    def test_the_estimator_map_comes_only_from_corroborated_matches(self):
        order = recorded(buy=500_000)
        corroborating = self.request_at("2026-07-25 12:00:00.000", 1_000_000.0)
        _, mapping = self.match([order], [corroborating])
        assert mapping == {"w": order.quote_solver}

        other = recorded(buy=1)
        heuristic = self.request_at("2026-07-25 12:00:00.000", 1_000_000.0)
        _, empty = self.match([other], [heuristic])
        assert empty == {}

    def test_the_estimator_map_is_keyed_on_the_winning_estimate_not_the_request_row(self):
        """The tier rests on the `is_winner` row; the map must rest on the same identity,
        or a request row whose `winner` column disagrees would map the wrong name."""
        order = recorded(buy=500_000)
        request = QuoteRequest(
            request_id="r",
            timestamp="2026-07-25 12:00:00.000",
            kind="sell",
            sell_token="aa",
            buy_token="bb",
            in_amount=1_000_000.0,
            winner="stale-name",
            estimates=(estimate("w", 500_000.0, 1_000, winner=True),),
        )
        _, mapping = self.match([order], [request])
        assert mapping == {"w": order.quote_solver}


class TestLadders:
    """`load_ladders` against a fake client: the per-day bounds and the deduplication
    that a `ReplacingMergeTree` needs before its parts merge."""

    class FakeClient:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows
            self.sql: list[str] = []

        def fetch(self, sql: str) -> list[dict[str, Any]]:
            self.sql.append(sql)
            return self.rows

    def test_duplicate_rows_collapse_to_one_estimate_per_estimator(self):
        from typing import cast

        from loo import ch
        from loo.quotes import load_ladders

        row = {
            "request_id": "r1", "estimator": "w", "out_amount": 500_000.0, "gas": 1_000.0,
            "estimate_verified": 1, "is_winner": 1,
        }
        client = self.FakeClient([row, dict(row), {**row, "estimator": "s", "is_winner": 0}])
        request = sell_request(winner="w")
        ladders = load_ladders(cast(ch.Client, client), "mainnet", [request])
        assert sorted(e.estimator for e in ladders["r1"]) == ["s", "w"]
        # ...so the winner is unambiguous and the match can still be corroborated.
        with_ladder = QuoteRequest(
            request_id="r1", timestamp=request.timestamp, kind="sell", sell_token="aa",
            buy_token="bb", in_amount=1_000_000.0, winner="w", estimates=ladders["r1"],
        )
        assert with_ladder.recorded_winner is not None

    def test_each_query_is_bounded_to_the_requests_own_day(self):
        from typing import cast

        from loo import ch
        from loo.quotes import load_ladders

        client = self.FakeClient([])
        load_ladders(cast(ch.Client, client), "mainnet", [sell_request(winner="w")])
        (sql,) = client.sql
        assert "event_timestamp >= '2026-07-24 23:30:00'" in sql
        assert "event_timestamp < '2026-07-26 00:01:30'" in sql
        assert "request_id in ('r1')" in sql


class TestShiftedLimit:
    """The one channel besides the fee through which a quote reaches the solve side,
    and the two facts about it that let executions and ranking be held fixed (D34)."""

    def order(self, side: str = "sell", executed_sell: int = 1000, executed_buy: int = 2100):
        from loo.valuation import Order

        return Order(
            uid="o1", sell_token="aa", buy_token="bb", sell_amount=1000, buy_amount=2000,
            executed_sell=executed_sell, executed_buy=executed_buy, side=side,  # type: ignore[arg-type]
        )

    def test_a_worse_quote_weakens_the_limit_and_always_clears(self):
        from loo.quotes import shifted_limit

        recorded = Quote(sell_amount=1000, buy_amount=2050, fee=0)
        worse = Quote(sell_amount=1000, buy_amount=2020, fee=0)
        shift = shifted_limit(self.order(), recorded, worse)
        assert shift is not None and shift.clears
        # The user would have signed 2,000 x 2020/2050 instead of 2,000, so the
        # mechanism would have measured 29 more atoms of surplus on the same fill.
        assert shift.surplus_shift_atoms == 29

    def test_a_better_quote_can_tighten_the_limit_past_the_execution(self):
        from loo.quotes import shifted_limit

        recorded = Quote(sell_amount=1000, buy_amount=2050, fee=0)
        better = Quote(sell_amount=1000, buy_amount=2200, fee=0)
        shift = shifted_limit(self.order(), recorded, better)
        assert shift is not None and not shift.clears  # 2,146 required, 2,100 received
        assert shift.surplus_shift_atoms == -146

    def test_the_fee_is_part_of_the_effective_rate_on_both_sides(self):
        from loo.quotes import shifted_limit

        # A 50-atom fee on 1,000 sold nets the 2,050 down to 1,947.5; a fee-free quote
        # of 1,947 shows the user the same price, so the limit barely moves.
        recorded = Quote(sell_amount=1000, buy_amount=2050, fee=50)
        same_net = Quote(sell_amount=1000, buy_amount=2050 * 950 // 1000, fee=0)
        shift = shifted_limit(self.order(), recorded, same_net)
        assert shift is not None and shift.clears
        assert abs(shift.surplus_shift_atoms) <= 1

    def test_a_buy_order_shifts_the_sell_limit(self):
        from loo.quotes import shifted_limit

        recorded = Quote(sell_amount=940, buy_amount=2000, fee=10)  # costs 950
        worse = Quote(sell_amount=960, buy_amount=2000, fee=10)  # costs 970
        shift = shifted_limit(
            self.order(side="buy", executed_sell=950, executed_buy=2000), recorded, worse
        )
        assert shift is not None and shift.clears
        # The signed 1,000 sell limit becomes 1,000 x 970/950: 21 more atoms of room.
        assert shift.surplus_shift_atoms == 21

    def test_nothing_to_test_without_an_execution_or_a_rate(self):
        from loo.quotes import shifted_limit

        recorded = Quote(sell_amount=1000, buy_amount=2050, fee=0)
        assert shifted_limit(self.order(executed_sell=0, executed_buy=0), recorded, recorded) is None
        underwater = Quote(sell_amount=1000, buy_amount=2050, fee=1000)
        assert shifted_limit(self.order(), underwater, recorded) is None


class TestValidation:
    def test_a_reproduced_pick_counts_and_a_tie_is_flagged(self):
        order = recorded()
        request = sell_request(
            estimate("w", 500_000, 1_000, winner=True),
            estimate("tie", 500_000, 1_000),
            winner="w",
        )
        result = validate_picks([MatchedQuote(order, request, "corroborated")])
        assert result.reproduced == 1
        assert result.tied == 1

    def test_a_disagreement_is_named_rather_than_averaged_away(self):
        order = recorded()
        request = sell_request(
            estimate("recorded", 400_000, 1_000, winner=True),
            estimate("better", 500_000, 1_000),
            winner="recorded",
        )
        result = validate_picks([MatchedQuote(order, request, "corroborated")])
        assert result.reproduced == 0
        assert result.disagreements == [(request.request_id, "recorded", "better")]

    def test_the_class_rule_is_only_tested_by_mixed_requests(self):
        order = recorded()
        homogeneous = sell_request(
            estimate("w", 500_000, 1_000, winner=True), winner="w"
        )
        mixed = sell_request(
            estimate("w", 500_000, 1_000, verified=True, winner=True),
            estimate("u", 900_000, 1_000, verified=False),
            winner="w",
        )
        result = validate_picks(
            [
                MatchedQuote(order, homogeneous, "corroborated"),
                MatchedQuote(order, mixed, "corroborated"),
            ]
        )
        assert result.mixed_class == 1
        assert result.mixed_class_correct == 1
        assert result.reproduced == 2

    def test_a_price_suspect_match_is_left_out_of_the_check(self):
        """Its `sell_token_price` is the very number the ranking weights gas with, so
        a disagreement there would be the guard's own exclusion, not the rule's."""
        order = recorded()
        request = sell_request(
            estimate("recorded", 400_000, 1_000, winner=True),
            estimate("better", 500_000, 1_000),
            winner="recorded",
        )
        result = validate_picks(
            [MatchedQuote(order, request, "corroborated")],
            price_suspects=frozenset({order.order_uid}),
        )
        assert result.price_suspect == 1
        assert result.requests == 0
        assert result.disagreements == []


class TestGuards:
    """The two leaves that exist to stop a ratio being formed where it means nothing."""

    def analysis(self) -> QuoteAnalysis:
        return QuoteAnalysis(
            solver="X", ranking="effective-sell", window_orders=10,
            window_volume_native=1_000_000,
        )

    def test_an_order_that_never_traded_weighs_nothing(self):
        """Quoted volume is not a market: five probe orders of ten million WETH were 52.4
        of one window's 52.5 M ETH quoted, and none of them traded."""
        probe = recorded(sell=10_000_000 * 10**18, executed=0)
        assert probe.volume_native == 0
        assert probe.quoted_volume_native > 0
        assert not probe.traded

    def test_a_price_suspect_order_is_excluded_from_both_sides(self):
        analysis = self.analysis()
        order = recorded()
        request = sell_request(
            estimate("w", 500_000, 1_000, winner=True),
            estimate("s", 495_000, 1_000),
            winner="w",
        )
        diff = diff_quote(
            MatchedQuote(order, request, "corroborated"),
            frozenset({"w"}),
            price_suspects=frozenset({order.order_uid}),
        )
        assert diff.leaf == "price_suspect"
        # Weight zeroed as well as the delta: the same wrong price sets both.
        assert diff.volume_native == 0
        analysis.add(diff)
        assert analysis.price_suspect == 1
        assert analysis.degraded == 0
        assert analysis.window_bps == 0.0

    def test_a_gas_cost_that_swallows_the_input_is_not_a_price_change(self):
        """The protocol clamps such an estimate to zero value (quote.rs:285); a ratio
        against it is not a basis-point figure."""
        analysis = self.analysis()
        order = recorded(sell=1_000, buy=500, fee=2_000)  # fee exceeds the sell amount
        request = sell_request(
            estimate("w", 500, 10, winner=True),
            estimate("s", 495, 600),
            in_amount=1_000.0,
            winner="w",
        )
        diff = diff_quote(MatchedQuote(order, request, "corroborated"), frozenset({"w"}))
        assert diff.leaf == "nonpositive"
        analysis.add(diff)
        assert analysis.nonpositive == 1
        assert analysis.window_bps == 0.0
        # Nor does it reach the fee path: the same arithmetic is what
        # `fees._adjust_quote_to_order_limits` refuses, and a refused replacement would
        # only make the order's fee unknown on one side. The recorded quote stays.
        assert diff.quote_loo is None
        assert analysis.counterfactual_quotes == {}

    def test_a_price_suspect_order_the_solver_lost_is_not_a_win(self):
        """The guard leaves are excluded *wins*. An order the removed solver merely lost
        must stay unaffected however suspect its price is — otherwise every matched
        suspect order counts as a win of every solver in the window."""
        analysis = self.analysis()
        order = recorded()
        request = sell_request(
            estimate("w", 500_000, 1_000, winner=True),
            estimate("loser", 495_000, 1_000),
            winner="w",
        )
        diff = diff_quote(
            MatchedQuote(order, request, "corroborated"),
            frozenset({"loser"}),
            price_suspects=frozenset({order.order_uid}),
        )
        assert diff.leaf == "unaffected"
        analysis.add(diff)
        assert analysis.quote_wins == 0
        assert analysis.price_suspect == 0

    def test_an_unpriced_request_is_counted_and_charges_nothing(self):
        """The `autopilot` rule needs the buy token's native price on a sell order; a
        token the window's auctions never priced is reported, not guessed."""
        analysis = QuoteAnalysis(
            solver="X", ranking="autopilot", window_orders=10,
            window_volume_native=1_000_000,
        )
        order = recorded()
        request = sell_request(
            estimate("w", 500_000, 1_000, winner=True),
            estimate("s", 495_000, 1_000),
            winner="w",
        )
        diff = diff_quote(
            MatchedQuote(order, request, "corroborated"),
            frozenset({"w"}),
            ranking="autopilot",
            buy_token_prices={},
        )
        assert diff.leaf == "unpriced"
        assert diff.quote_loo is None
        analysis.add(diff)
        assert analysis.unpriced == 1
        assert analysis.quote_wins == 1
        assert analysis.degraded == 0
        assert analysis.counterfactual_quotes == {}


class TestPriceGuard:
    def test_agreement_within_the_threshold_is_left_alone(self):
        order = recorded()
        # order_quotes says 100 wei/atom; the auction feed says the same, scaled by 1e18.
        prices = {order.sell_token: 100.0 * 10**18}
        assert price_suspect_orders([order], prices) == frozenset()

    def test_a_feed_disagreeing_by_orders_of_magnitude_is_excluded(self):
        order = recorded()
        prices = {order.sell_token: 100.0 * 10**18 * 1e7}
        assert price_suspect_orders([order], prices) == frozenset({order.order_uid})

    def test_a_token_the_window_never_priced_cannot_be_tested(self):
        """D14's rule: exclude what can be proven wrong, never what cannot be checked."""
        order = recorded()
        assert price_suspect_orders([order], {}) == frozenset()


class TestRawLayer:
    """Competitions the gold tables do not hold: `gold_quotes` is built from the
    `quote_requested` event, so a competition whose request event never arrived is in
    neither gold table while its estimates and winner are complete in `raw_quotes`."""

    class FakeClient:
        def __init__(self, answers: dict[str, list[dict[str, Any]]]) -> None:
            self.answers = answers
            self.sql: list[str] = []

        def fetch(self, sql: str) -> list[dict[str, Any]]:
            self.sql.append(sql)
            for needle, rows in self.answers.items():
                if needle in sql:
                    return rows
            return []

    def test_the_gold_candidate_query_excludes_fast_but_not_id_less_requests(self):
        from typing import cast

        from loo import ch
        from loo.quotes import load_candidate_requests

        client = self.FakeClient({})
        load_candidate_requests(cast(ch.Client, client), "mainnet", [recorded()])
        (sql,) = client.sql
        # `order_quotes` never holds a fast quote, so fast requests are not candidates;
        # `quote_id` says nothing about whether the competition priced an order.
        assert "quote_id" not in sql
        assert "price_quality != 'fast'" in sql

    def test_raw_candidates_group_estimates_by_request_and_match_the_exact_amount(self):
        from typing import cast

        from loo import ch
        from loo.quotes import load_raw_candidates

        order = recorded()
        row = {
            "request_id": "raw-1", "ts": "2026-07-25 12:00:00.000", "sell_": "0xaa",
            "buy_": "0xbb", "kind_": "sell", "in_amount_": "1000000",
        }
        client = self.FakeClient({"price_estimates": [row]})
        (request,) = load_raw_candidates(cast(ch.Client, client), "mainnet", [order])
        (sql,) = client.sql
        assert "raw_quotes.price_estimates" in sql and "group by request_id" in sql
        assert "('0xaa', '0xbb', '1000000')" in sql  # the exact decimal string
        assert " success " in sql and "environment" not in sql
        assert (request.source, request.price_quality, request.winner) == ("raw", "unknown", "")
        assert request.in_amount == 1_000_000.0

    def test_raw_ladders_take_the_winner_from_its_own_event_stream(self):
        from typing import cast

        from loo import ch
        from loo.quotes import load_ladders, with_ladder

        request = QuoteRequest(
            request_id="raw-1", timestamp="2026-07-25 12:00:00.000", kind="sell",
            sell_token="aa", buy_token="bb", in_amount=1_000_000.0, winner="", source="raw",
        )
        client = self.FakeClient({
            "winning_price_estimates": [{"request_id": "raw-1", "estimator": "w"}],
            "price_estimates": [
                {"request_id": "raw-1", "estimator": "w", "out_amount": "500000", "gas": "1000", "verified": True},
                {"request_id": "raw-1", "estimator": "s", "out_amount": "495000", "gas": "1000", "verified": None},
            ],
        })
        ladders = load_ladders(cast(ch.Client, client), "mainnet", [request])
        by_name = {e.estimator: e for e in ladders["raw-1"]}
        assert by_name["w"].is_winner and not by_name["s"].is_winner
        assert by_name["w"].verified and not by_name["s"].verified
        assert by_name["w"].out_amount == 500_000.0
        # The request row carried no winner; the ladder's mark supplies it, so
        # `diff_quote` asks the same first question on both sources.
        assert with_ladder(request, ladders["raw-1"]).winner == "w"


class TestImplausibleReplacement:
    """A replacement quote more than 2x off the recorded one is a broken estimate promoted
    to winner, not a price change — in either direction. The case that forced this: an
    estimator answered a buy request with a cost of a few atoms, and once the winner was
    removed that answer won and read as +1.16e15 bps."""

    def analysis(self) -> QuoteAnalysis:
        return QuoteAnalysis(
            solver="X", ranking="effective-sell", window_orders=10,
            window_volume_native=1_000_000,
        )

    def test_a_near_free_buy_replacement_is_excluded_not_an_improvement(self):
        order = recorded(kind="buy", sell=1_000_000, buy=500_000)
        request = QuoteRequest(
            request_id="r1", timestamp="2026-07-25 12:00:00.000", kind="buy",
            sell_token="aa", buy_token="bb", in_amount=500_000.0, winner="winner",
            estimates=(estimate("winner", 1_000_000, 1_000, winner=True), estimate("broken", 3, 1)),
        )
        diff = diff_quote(MatchedQuote(order, request, "corroborated"), frozenset({"winner"}))
        assert diff.leaf == "implausible"
        assert diff.replacement == "broken"
        assert diff.quote_loo is None
        analysis = self.analysis()
        analysis.add(diff)
        assert analysis.implausible == 1
        assert analysis.implausible_volume_native == order.volume_native
        assert analysis.degraded == 0 and analysis.window_bps == 0.0
        assert analysis.counterfactual_quotes == {}

    def test_a_sell_replacement_worth_a_third_is_excluded_too(self):
        order = recorded()
        request = sell_request(
            estimate("winner", 500_000, 1_000, winner=True),
            estimate("third", 160_000, 1_000),
            winner="winner",
        )
        diff = diff_quote(MatchedQuote(order, request, "corroborated"), frozenset({"winner"}))
        assert diff.leaf == "implausible"

    def test_a_replacement_within_the_factor_stays_a_degradation(self):
        order = recorded()
        request = sell_request(
            estimate("winner", 500_000, 1_000, winner=True),
            estimate("half-ish", 260_000, 1_000),  # 0.52x: a bad quote, not a broken one
            winner="winner",
        )
        diff = diff_quote(MatchedQuote(order, request, "corroborated"), frozenset({"winner"}))
        assert diff.leaf == "degraded"
        assert diff.delta_bps == pytest.approx((260_000 / 500_000 - 1) * 10_000)
