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
        rule there, differing only in the unit they truncate in: wei for the old rule,
        sell atoms for the new one — the keys are a common factor apart."""
        estimates = [estimate("a", 1_000_000, 500), estimate("b", 1_001_000, 100)]
        keys = {
            ranking: [
                rank_key(e, is_sell=False, in_amount=1.0, pricing=PRICING, ranking=ranking)
                for e in estimates
            ]
            for ranking in ("autopilot", "effective-sell")
        }
        assert keys["autopilot"] == [
            PRICING.sell_token_price * k for k in keys["effective-sell"]
        ]

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


class TestProdTieBreaks:
    """Prod clamps an underwater sell quote to zero and truncates every effective amount
    to an integer before comparing, then keeps the first of equal answers in arrival
    order. Both make ties common on dust requests, and both decided real bnb winners."""

    def test_underwater_sell_estimates_tie_and_the_first_arrival_wins(self):
        # 10 atoms in, gas at 2 atoms each: every estimate costs more than it is worth.
        first = estimate("first", 1_000, 100)
        cheaper = estimate("cheaper", 1_000, 50)  # less under water, but still under
        winner = pick([first, cheaper], is_sell=True, in_amount=10.0, pricing=PRICING)
        assert winner == first
        assert pick([cheaper, first], is_sell=True, in_amount=10.0, pricing=PRICING) == cheaper

    def test_amounts_that_truncate_to_the_same_atom_tie(self):
        """35 atoms out of 2e14 in: 32.79 and 32.24 effective atoms are both 32."""
        pricing = Pricing(gas_price=55_000_000.0, sell_token_price=1.0)
        early = estimate("early", 35, 286_090)  # 32.2464 effective
        late = estimate("late", 35, 229_190)  # 32.794 effective: better, same atom
        assert pick([early, late], is_sell=True, in_amount=2e14, pricing=pricing) == early
        unclamped = [
            effective_sell_amount(e, is_sell=True, in_amount=2e14, pricing=pricing)
            for e in (early, late)
        ]
        assert unclamped[1] > unclamped[0]  # the netting alone would have said `late`

    def test_validation_reproduces_an_arrival_order_tie(self):
        order = recorded()
        request = sell_request(
            estimate("first", 500_000, 1_000, winner=True),
            estimate("second", 500_000, 1_000),
            winner="first",
        )
        result = validate_picks([MatchedQuote(order, request, "corroborated")])
        assert (result.reproduced, result.tied, result.tie_mismatches) == (1, 1, 0)

    def test_a_tie_resolved_to_another_estimate_is_not_a_disagreement(self):
        """Two answers in the same millisecond on the gold layer can come back in the
        other order; the rule still picked an equivalent estimate."""
        order = recorded()
        request = sell_request(
            estimate("loaded-first", 500_000, 1_000),
            estimate("recorded", 500_000, 1_000, winner=True),
            winner="recorded",
        )
        result = validate_picks([MatchedQuote(order, request, "corroborated")])
        assert (result.reproduced, result.tie_mismatches) == (0, 1)
        assert result.disagreements == []


class TestArrivalOrder:
    class FakeClient:
        def __init__(self, answers: dict[str, list[dict[str, Any]]]) -> None:
            self.answers = answers

        def fetch(self, sql: str) -> list[dict[str, Any]]:
            for needle, rows in self.answers.items():
                if needle in sql:
                    return rows
            return []

    def test_gold_ladders_come_back_in_estimate_timestamp_order(self):
        from typing import cast

        from loo import ch
        from loo.quotes import load_ladders

        def row(name: str, arrived: str) -> dict[str, Any]:
            return {
                "request_id": "r1", "estimator": name, "out_amount": 1.0, "gas": 1.0,
                "estimate_verified": 1, "is_winner": 0, "arrived": arrived,
            }

        client = self.FakeClient({"fct_quote_estimates": [
            row("late", "2026-07-25 12:00:00.300"), row("early", "2026-07-25 12:00:00.100"),
        ]})
        ladders = load_ladders(cast(ch.Client, client), "mainnet", [sell_request()])
        assert [e.estimator for e in ladders["r1"]] == ["early", "late"]

    def test_raw_ladders_come_back_in_nats_seq_order(self):
        from typing import cast

        from loo import ch
        from loo.quotes import load_ladders

        request = QuoteRequest(
            request_id="r1", timestamp="2026-07-25 12:00:00.000", kind="sell",
            sell_token="aa", buy_token="bb", in_amount=1.0, winner="", source="raw",
        )
        client = self.FakeClient({"from raw_quotes.price_estimates": [
            {"request_id": "r1", "estimator": "b", "out_amount": "1", "gas": "1",
             "verified": True, "nats_seq": "20"},
            {"request_id": "r1", "estimator": "a", "out_amount": "1", "gas": "1",
             "verified": True, "nats_seq": "3"},
        ]})
        ladders = load_ladders(cast(ch.Client, client), "mainnet", [request])
        assert [e.estimator for e in ladders["r1"]] == ["a", "b"]


def id_order(uid: str, quote_id: int | None, *, out: int = 500_000, solver: str = "cc" * 20):
    from dataclasses import replace

    base = recorded(buy=out)
    return replace(base, order_uid=uid, quote_id=quote_id, quote_solver=solver,
                   created="2026-10-08 12:00:30.000")


class TestQuoteIdLink:
    """`link_by_quote_id` against a fake ClickHouse: the order's id finds its estimate,
    the estimate its request, and the link holds only if the estimate is the recorded
    winner and reproduces the stored amount to the atom."""

    class FakeClient:
        def __init__(
            self,
            winners: list[dict[str, Any]],
            estimates: list[dict[str, Any]],
            requote_winners: list[dict[str, Any]] | None = None,
            requote_estimates: list[dict[str, Any]] | None = None,
            computed: list[dict[str, Any]] | None = None,
        ):
            self.winners, self.estimates = winners, estimates
            self.requote_winners = requote_winners or []
            self.requote_estimates = requote_estimates or []
            self.computed = computed or []
            self.sql: list[str] = []

        def fetch(self, sql: str) -> list[dict[str, Any]]:
            self.sql.append(sql)
            head = sql.split(" where ")[0]
            if "quote_computed" in head:
                return self.computed
            if "request_id is null" in sql:  # the re-quote pass
                if "winning_price_estimates" in head:
                    return self.requote_winners
                return self.requote_estimates
            return self.winners if "winning_price_estimates" in head else self.estimates

    @staticmethod
    def est(request: str, name: str, quote_id: int | None, out: str, seq: int,
            solver: str | None = None) -> dict[str, Any]:
        return {
            "request_id": request, "estimator": name, "quote_id": str(quote_id) if quote_id else None,
            "solver": solver if solver is not None else "0x" + name[0] * 40,
            "kind": "sell", "sell_": "0xaa", "buy_": "0xbb", "in_amount": "1000000",
            "out_amount": out, "gas": "1000", "verified": True, "nats_seq": str(seq),
            "ts": f"2026-10-08 12:00:{seq:02d}.000",
        }

    def link(self, **requotes: list[dict[str, Any]]):
        from typing import cast

        from loo import ch
        from loo.quotes import link_by_quote_id

        estimates = [
            self.est("r1", "w", 11, "500000", 2),
            self.est("r1", "s", 12, "499000", 1),
            self.est("r1", "s", 12, "499000", 1),  # a ReplacingMergeTree duplicate
            self.est("r2", "w", 21, "500000", 4),
            self.est("r2", "early", 22, "500000", 3),  # a streamed answer, not the winner
        ]
        winners = [{"request_id": "r1", "estimator": "w"}, {"request_id": "r2", "estimator": "w"}]
        client = self.FakeClient(winners, estimates, **requotes)
        orders = [
            id_order("o-linked", 11, solver="w" * 40),
            id_order("o-streamed", 22),
            id_order("o-recomputed", 11, out=499_999),
            id_order("o-no-id", None),
            id_order("o-ethflow-zero", 0),
            id_order("o-missing", 99),
        ]
        return link_by_quote_id(cast(ch.Client, client), "mainnet", orders), client

    def test_the_answer_must_reproduce_the_stored_amount(self):
        result, _ = self.link()
        assert [m.order.order_uid for m in result.matches] == ["o-linked", "o-streamed"]
        linked, streamed = result.matches
        assert linked.tier == "corroborated" and linked.request.source == "raw"
        assert linked.request.winner == "w" and linked.stream is None
        assert streamed.stream is not None and streamed.stream.taken == "early"
        assert result.stats.unlinked == {
            "no quote id": 2,
            "id not in the competition record": 1,
            "stored quote not reproduced": 1,
        }
        assert (result.stats.orders, result.stats.linked) == (6, 2)
        assert (result.stats.streamed, result.stats.streamed_early) == (1, 1)

    def test_an_order_carrying_a_non_winning_answer_is_linked_as_streamed(self):
        """`early` arrived first and was forwarded; `w` beat it and won. The order took
        `early`, so the stream decides its counterfactual, not the full ladder."""
        result, _ = self.link(
            computed=[
                {"request_id": "r2", "quote_id": "22", "nats_seq": "10"},
                {"request_id": "r2", "quote_id": "21", "nats_seq": "11"},
            ]
        )
        (streamed,) = [m for m in result.matches if m.order.order_uid == "o-streamed"]
        assert streamed.stream is not None
        assert streamed.stream.taken == "early"
        assert streamed.stream.forwarded == ("early", "w")
        assert (streamed.stream.position, streamed.stream.horizon) == (0, 1)
        assert streamed.stream.complete

    def test_ladders_are_exact_deduplicated_and_in_arrival_order(self):
        result, _ = self.link()
        match = result.matches[0]
        ladder = match.request.estimates
        assert [e.estimator for e in ladder] == ["s", "w"]  # nats_seq 1, then 2
        assert all(isinstance(e.out_amount, int) for e in ladder)
        assert match.request.in_amount == 1_000_000 and isinstance(match.request.in_amount, int)

    def test_the_estimator_map_is_read_from_every_estimate(self):
        """Including `early` and `s`, which won nothing a match could derive them from."""
        result, _ = self.link()
        assert result.estimator_map == {
            "w": "w" * 40, "s": "s" * 40, "early": "e" * 40,
        }

    def test_ids_resolve_to_requests_inside_one_bounded_query_per_table(self):
        _, client = self.link()
        id_queries = [sql for sql in client.sql if "request_id is null" not in sql]
        assert len(id_queries) == 3  # winners, forwarded answers, the ladder
        for sql in id_queries:
            assert "request_id in (select request_id from raw_quotes.price_estimates" in sql
            assert "quote_id in (11, 22, 99)" in sql
            assert "event_timestamp >= '2026-10-07 23:30:00'" in sql

    @staticmethod
    def requote(quote_id: int, name: str, out: str, at: str) -> dict[str, Any]:
        return {
            "quote_id": str(quote_id), "estimator": name, "solver": "0x" + name[0] * 40,
            "sell_": "0xaa", "buy_": "0xbb", "kind": "sell", "in_amount": "1000000",
            "out_amount": out, "gas": "1000", "verified": False, "nats_seq": str(quote_id),
            "ts": f"2026-10-08 12:00:{at}",
        }

    def test_an_order_the_backend_requoted_links_to_that_competition(self):
        """The ethflow case: the id names the UI quote, the stored quote is the backend's
        own request-less re-quote at indexing — found by its exact amount, confirmed as
        the recorded winner, its siblings bounded in time and quote id."""
        result, client = self.link(
            requote_winners=[{"quote_id": "501"}],
            requote_estimates=[
                self.requote(501, "kipseli", "499999", "32.500"),  # the stored amount
                self.requote(503, "fractal", "499000", "32.100"),
                self.requote(505, "dfusion", "499500", "32.300"),
                self.requote(900, "far", "499998", "32.200"),  # another id block
                self.requote(504, "late", "499998", "45.000"),  # another time
            ],
        )
        (requoted,) = [m for m in result.matches if m.order.order_uid == "o-recomputed"]
        assert requoted.tier == "corroborated"
        assert requoted.request.winner == "kipseli"
        assert requoted.request.request_id == "requote-501"
        assert [e.estimator for e in requoted.request.estimates] == [
            "kipseli", "fractal", "dfusion",
        ]  # in quote-id order here, which the fake rows use as `nats_seq`
        assert result.stats.requoted == 1
        assert "stored quote not reproduced" not in result.stats.unlinked
        assert result.estimator_map["kipseli"] == "k" * 40
        (sql,) = [
            q
            for q in client.sql
            if "request_id is null" in q and "winning" not in q.split(" where ")[0]
        ]
        assert "('0xaa', '0xbb', 'sell', '1000000')" in sql

    def test_a_requote_that_did_not_win_is_not_a_link(self):
        result, _ = self.link(
            requote_winners=[{"quote_id": "503"}],
            requote_estimates=[self.requote(501, "kipseli", "499999", "32.500")],
        )
        assert result.stats.requoted == 0
        assert result.stats.unlinked["stored quote not reproduced"] == 1


class TestTrivialOrders:
    def test_a_zero_solver_quote_had_no_competition(self):
        from dataclasses import replace

        assert replace(recorded(), quote_solver="00" * 20).trivial
        assert not recorded().trivial


class TestTwoSidedGuard:
    """`imbalanced_orders`: sold against bought value at each trade's own auction prices
    — what catches a token both feeds price wrong by the same factor."""

    WETH = "ee" * 19 + "ef"  # any address; only its role as the native token's ERC20 matters

    def order(self, uid: str = "o1", sell: str = "aa", buy: str = "bb"):
        from dataclasses import replace

        return replace(recorded(), order_uid=uid, sell_token=sell, buy_token=buy)

    def test_a_token_both_feeds_misprice_is_caught_by_the_other_side(self):
        from loo.quotes import OrderTrade, imbalanced_orders, price_suspect_orders

        order = self.order()
        # Sold 1,000 atoms of `aa`, bought 1,000 of `bb`; `aa` is priced 1,000x too high
        # in the auction *and* by the api (100 wei/atom recorded, so the one-sided check
        # agrees with it and passes).
        prices = {(7, "aa"): 100 * 10**18, (7, "bb"): 10**17}
        trades = [OrderTrade("o1", 7, 1_000, 1_000)]
        assert price_suspect_orders([order], {"aa": 100.0 * 10**18}) == frozenset()
        assert imbalanced_orders([order], trades, prices, weth=self.WETH) == {"o1"}

    def test_each_trade_is_valued_in_its_own_auction(self):
        """A token that tripled over the window: per auction the two sides agree, so
        nothing is excluded — a window median would have flagged the late trade."""
        from loo.quotes import OrderTrade, imbalanced_orders

        early, late = self.order("early"), self.order("late")
        prices = {
            (1, "aa"): 10**18, (1, "bb"): 10**18,
            (2, "aa"): 3 * 10**18, (2, "bb"): 3 * 10**18,
        }
        trades = [OrderTrade("early", 1, 100, 100), OrderTrade("late", 2, 100, 100)]
        assert imbalanced_orders([early, late], trades, prices, weth=self.WETH) == frozenset()

    def test_a_trade_missing_a_price_is_not_checked(self):
        from loo.quotes import OrderTrade, imbalanced_orders

        trades = [OrderTrade("o1", 7, 1_000, 1)]
        assert imbalanced_orders([self.order()], trades, {(7, "aa"): 10**18}, weth=self.WETH) == frozenset()

    def test_the_native_token_is_priced_as_its_wrapped_erc20(self):
        from loo.primitives import NATIVE_TOKEN
        from loo.quotes import OrderTrade, imbalanced_orders

        order = self.order(buy=NATIVE_TOKEN)
        prices = {(7, "aa"): 10**18, (7, self.WETH): 10**15}
        trades = [OrderTrade("o1", 7, 1_000, 1_000)]  # 1,000 aa for 1,000 wei-worth: off
        assert imbalanced_orders([order], trades, prices, weth=self.WETH) == {"o1"}

    def test_the_one_sided_check_uses_the_orders_own_auctions_where_it_traded(self):
        from loo.quotes import OrderTrade, price_suspect_orders, traded_sell_prices

        order = self.order()  # api: 100 wei per atom
        trades = [OrderTrade("o1", 7, 500, 1), OrderTrade("o1", 8, 500, 1)]
        prices = {(7, "aa"): 90 * 10**18, (8, "aa"): 110 * 10**18}
        traded = traded_sell_prices([order], trades, prices, weth=self.WETH)
        assert traded == {"o1": 100.0 * 10**18}
        # A window median 3x away (the token moved) would have excluded it.
        median = {"aa": 300.0 * 10**18}
        assert price_suspect_orders([order], median) == {"o1"}
        assert price_suspect_orders([order], median, traded) == frozenset()


class TestStream:
    """The streamed counterfactual: the answer an order took decides whether a removal
    touches it, and a replacement is what the stream would have shown by the commit."""

    def request(self, *estimates: Estimate, winner: str = "") -> QuoteRequest:
        return sell_request(*estimates, winner=winner)

    def take(self, taken: str, position: int, horizon: int, *, complete: bool = True,
             forwarded: tuple[str, ...] = ()):
        from loo.quotes import StreamTake

        return StreamTake(taken=taken, position=position, horizon=horizon,
                          complete=complete, forwarded=forwarded)

    def diff(self, request: QuoteRequest, take: Any, removed: str, *, buy: int = 500_000):
        order = recorded(buy=buy)
        return diff_quote(
            MatchedQuote(order, request, "corroborated", stream=take), frozenset({removed})
        )

    def test_removing_the_recorded_winner_leaves_an_earlier_answer_standing(self):
        """The order took `a`; that `w` won later does not matter to it."""
        request = self.request(
            estimate("a", 500_000, 1_000), estimate("w", 510_000, 1_000, winner=True),
            winner="w",
        )
        diff = self.diff(request, self.take("a", 0, 1), removed="w")
        assert diff.leaf == "unaffected" and diff.streamed and diff.winner == "a"

    def test_the_replacement_is_the_best_answer_arrived_by_the_commit(self):
        """Commit before `w` was forwarded: `b` (arrived after `a`, never forwarded) is in
        reach, `w` is not."""
        request = self.request(
            estimate("c", 480_000, 1_000), estimate("a", 500_000, 1_000),
            estimate("b", 490_000, 1_000), estimate("w", 510_000, 1_000, winner=True),
            winner="w",
        )
        diff = self.diff(request, self.take("a", 1, 3), removed="a")
        assert diff.leaf == "degraded" and diff.replacement == "b"
        # Committing the moment `a` was forwarded would have shown `c`, not `b`.
        assert diff.commit_dependent

    def test_with_nothing_else_arrived_the_user_waits_for_the_next_answer(self):
        request = self.request(
            estimate("a", 500_000, 1_000), estimate("late", 495_000, 1_000, winner=True),
            winner="late",
        )
        diff = self.diff(request, self.take("a", 0, 1), removed="a")
        assert diff.leaf == "degraded" and diff.replacement == "late"
        assert not diff.commit_dependent

    def test_a_dropped_stream_cannot_say_what_the_user_would_have_waited_for(self):
        request = self.request(estimate("a", 500_000, 1_000))
        diff = self.diff(request, self.take("a", 0, 1, complete=False), removed="a")
        assert diff.leaf == "unrecorded"

    def test_a_finished_stream_with_no_other_answer_leaves_no_quote(self):
        request = self.request(estimate("a", 500_000, 1_000, winner=True), winner="a")
        diff = self.diff(request, self.take("a", 0, 1), removed="a")
        assert diff.leaf == "no_quote"

    def test_the_replay_forwards_strict_improvements_only(self):
        from loo.quotes import replay_stream

        ladder = [
            estimate("a", 500_000, 1_000), estimate("tie", 500_000, 1_000),
            estimate("worse", 400_000, 1_000), estimate("u", 900_000, 1_000, verified=False),
            estimate("b", 510_000, 1_000),
        ]
        forwarded = replay_stream(ladder, is_sell=True, in_amount=1_000_000.0, pricing=PRICING)
        # An unverified answer never beats a verified one, however large.
        assert forwarded == ["a", "b"]

    def test_an_answer_whose_fee_exceeds_the_input_sets_the_bar_unseen(self):
        """The quoter drops it (`SellAmountDoesNotCoverFee`), but the stream already
        counted it as its best, so the next answer must still beat it."""
        from loo.quotes import replay_stream

        ladder = [
            estimate("under", 600_000, 600_000),  # gas cost 1.2 M atoms > 1 M input
            estimate("ok", 500_000, 1_000),
        ]
        forwarded = replay_stream(ladder, is_sell=True, in_amount=1_000_000.0, pricing=PRICING)
        assert forwarded == ["ok"]

    def test_validation_replays_each_stream_against_its_record(self):
        order = recorded()
        request = self.request(
            estimate("a", 500_000, 1_000), estimate("b", 510_000, 1_000, winner=True),
            winner="b",
        )
        good = MatchedQuote(order, request, "corroborated",
                            stream=self.take("a", 0, 1, forwarded=("a", "b")))
        result = validate_picks([good])
        assert (result.streams, result.streams_reproduced) == (1, 1)
        bad_request = replace_request(request, "r9")
        bad = MatchedQuote(order, bad_request, "corroborated",
                           stream=self.take("a", 0, 1, forwarded=("b", "a")))
        result = validate_picks([bad])
        assert (result.streams, result.streams_reproduced) == (1, 0)
        assert result.stream_disagreements == [("r9", ("b", "a"), ("a", "b"))]


def replace_request(request: QuoteRequest, request_id: str) -> QuoteRequest:
    from dataclasses import replace

    return replace(request, request_id=request_id)


class TestStreamHorizon:
    def test_the_commit_lies_before_the_next_forwarded_answer_or_the_order(self):
        from loo.quotes import _stream_take  # pyright: ignore[reportPrivateUsage]

        request = sell_request(
            estimate("a", 1, 1), estimate("b", 1, 1), estimate("w", 1, 1),
            estimate("after", 1, 1), winner="w",
        )
        order = recorded()  # created 12:00:30.000
        arrived = {
            ("r1", "a"): "2026-07-25 12:00:20.000",
            ("r1", "b"): "2026-07-25 12:00:20.100",
            ("r1", "w"): "2026-07-25 12:00:20.200",
            ("r1", "after"): "2026-07-25 12:00:31.000",
        }
        # Took `a`; `w` was forwarded next, so the commit came before `w` arrived.
        early = _stream_take(order, request, "a", ("a", "w"), arrived)
        assert (early.position, early.horizon) == (0, 2)
        # Took the last forwarded answer: everything that arrived before the order counts.
        last = _stream_take(order, request, "w", ("a", "w"), arrived)
        assert (last.position, last.horizon) == (2, 3)
