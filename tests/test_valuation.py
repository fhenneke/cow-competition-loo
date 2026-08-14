"""Tests for the surplus and native-conversion formulas."""

from __future__ import annotations

from typing import Any

import pytest

from loo.primitives import NATIVE_TOKEN, Pair, ceil_div, order_owner, price_in_eth
from loo.valuation import (
    Order,
    SolutionValuation,
    ValuationError,
    custom_prices_from_executed,
    order_surplus,
    order_surplus_native,
    order_volume_native,
    solution_total,
    surplus_over,
    to_native,
    value_solution,
)

ONE = 10**18
WETH = "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DAI = "6b175474e89094c44da98b954eedeac495271d0f"


def sell_order(executed_sell: int, executed_buy: int, **kwargs: Any) -> Order:
    return Order(
        uid=kwargs.pop("uid", "a" * 112),
        sell_token=kwargs.pop("sell_token", WETH),
        buy_token=kwargs.pop("buy_token", USDC),
        sell_amount=kwargs.pop("sell_amount", 1000),
        buy_amount=kwargs.pop("buy_amount", 2000),
        executed_sell=executed_sell,
        executed_buy=executed_buy,
        side="sell",
        **kwargs,
    )


def buy_order(executed_sell: int, executed_buy: int, **kwargs: Any) -> Order:
    return Order(
        uid=kwargs.pop("uid", "b" * 112),
        sell_token=kwargs.pop("sell_token", WETH),
        buy_token=kwargs.pop("buy_token", USDC),
        sell_amount=kwargs.pop("sell_amount", 1000),
        buy_amount=kwargs.pop("buy_amount", 2000),
        executed_sell=executed_sell,
        executed_buy=executed_buy,
        side="buy",
        **kwargs,
    )


class TestPrimitives:
    def test_price_in_eth_floors(self):
        assert price_in_eth(ONE // 2, 3) == 1  # 1.5 -> 1

    def test_ceil_div(self):
        assert ceil_div(10, 3) == 4
        assert ceil_div(9, 3) == 3
        assert ceil_div(0, 3) == 0
        with pytest.raises(ZeroDivisionError):
            ceil_div(1, 0)

    def test_order_owner_is_the_middle_20_bytes(self):
        uid = "11" * 32 + "22" * 20 + "33" * 4
        assert order_owner(uid) == "22" * 20

    def test_order_owner_rejects_wrong_length(self):
        with pytest.raises(ValueError):
            order_owner("dead")


class TestSellSurplus:
    def test_full_fill(self):
        order = sell_order(executed_sell=1000, executed_buy=2100)
        assert order_surplus(order) == 100

    def test_partial_fill_scales_the_limit(self):
        order = sell_order(executed_sell=500, executed_buy=1050)
        assert order_surplus(order) == 50

    def test_scaled_limit_uses_ceiling_division(self):
        """`surplus_over` ceils the scaled limit buy, so a fill that floor division
        would call profitable yields exactly zero surplus (`arbitrator.rs:369`)."""
        order = sell_order(executed_sell=1, executed_buy=4, sell_amount=3, buy_amount=10)
        assert order_surplus(order) == 0

    def test_exactly_at_the_limit_is_zero_not_an_error(self):
        order = sell_order(executed_sell=1000, executed_buy=2000)
        assert order_surplus(order) == 0

    def test_below_the_limit_raises(self):
        order = sell_order(executed_sell=1000, executed_buy=1900)
        with pytest.raises(ValuationError, match="negative surplus"):
            order_surplus(order)


class TestBuySurplus:
    def test_full_fill(self):
        order = buy_order(executed_sell=900, executed_buy=2000)
        assert order_surplus(order) == 100

    def test_partial_fill_scales_the_limit(self):
        order = buy_order(executed_sell=450, executed_buy=1000)
        assert order_surplus(order) == 50

    def test_paying_more_than_the_limit_raises(self):
        order = buy_order(executed_sell=1100, executed_buy=2000)
        with pytest.raises(ValuationError, match="negative surplus"):
            order_surplus(order)


class TestSurplusOverGeneralForm:
    def test_custom_prices_from_executed_swap_the_sides(self):
        order = sell_order(executed_sell=1000, executed_buy=2100)
        prices = custom_prices_from_executed(order)
        assert (prices.sell, prices.buy) == (2100, 1000)

    def test_arbitrary_limits(self):
        """`surplus_over` is also used for price-improvement fees, against a quote's
        limits rather than the order's."""
        order = sell_order(executed_sell=1000, executed_buy=2100)
        prices = custom_prices_from_executed(order)
        assert surplus_over(order, prices, limit_sell=1000, limit_buy=1500) == 600

    def test_zero_limit_raises_rather_than_dividing(self):
        order = sell_order(executed_sell=1000, executed_buy=2100, sell_amount=0)
        with pytest.raises(ValuationError):
            order_surplus(order)


class TestNativeConversion:
    def test_sell_order_converts_through_the_buy_token(self):
        order = sell_order(executed_sell=1000, executed_buy=2100)
        assert order_surplus_native(order, {USDC: ONE}) == 100

    def test_sell_order_applies_the_price(self):
        order = sell_order(executed_sell=1000, executed_buy=2100)
        assert order_surplus_native(order, {USDC: ONE // 2}) == 50

    def test_buy_order_converts_sell_surplus_into_buy_tokens_first(self):
        """Buy-order surplus is in sell tokens and is scaled by the order's own
        `buy_amount / sell_amount` before the native conversion — which is why buy
        orders need limit amounts (`arbitrator.rs:238`)."""
        order = buy_order(executed_sell=900, executed_buy=2000)
        assert to_native(order, 100, {USDC: ONE}) == 200

    def test_missing_price_raises(self):
        order = sell_order(executed_sell=1000, executed_buy=2100)
        with pytest.raises(ValuationError, match="missing native price"):
            order_surplus_native(order, {DAI: ONE})

    def test_price_is_taken_for_the_buy_token_on_both_sides(self):
        order = buy_order(executed_sell=900, executed_buy=2000)
        with pytest.raises(ValuationError, match="missing native price"):
            order_surplus_native(order, {WETH: ONE})


class TestVolumeNative:
    def test_received_leg_at_the_buy_token_price(self):
        order = sell_order(executed_sell=1000, executed_buy=2100)
        assert order_volume_native(order, {USDC: ONE // 2}) == 1050

    def test_buy_order_uses_the_same_leg(self):
        """Both order kinds are valued on the received (buy) leg, through the same
        buy-token price `to_native` puts into the surplus — so that price cancels out
        of the per-order Δsurplus / volume ratio."""
        order = buy_order(executed_sell=900, executed_buy=2000)
        assert order_volume_native(order, {USDC: ONE}) == 2000

    def test_missing_price_raises(self):
        order = sell_order(executed_sell=1000, executed_buy=2100)
        with pytest.raises(ValuationError, match="missing native price"):
            order_volume_native(order, {DAI: ONE})


class TestValueSolution:
    def test_aggregates_by_raw_directed_pair(self):
        orders = [
            sell_order(1000, 2100, uid="a" * 112),
            sell_order(1000, 2050, uid="c" * 112),
        ]
        valuation = value_solution(
            orders, {"a" * 112: True, "c" * 112: True}, {USDC: ONE}, WETH
        )
        assert valuation.pair_surplus == {(WETH, USDC): 150}
        assert valuation.total == 150
        assert valuation.order_volume_native == {"a" * 112: 2100, "c" * 112: 2050}

    def test_non_contributing_orders_are_skipped_but_still_claim_their_pair(self):
        """`score_by_token_pair` skips them; `pick_winners` does not."""
        orders = [
            sell_order(1000, 2100, uid="a" * 112),
            sell_order(1000, 2100, uid="c" * 112, sell_token=DAI, buy_token=USDC),
        ]
        valuation = value_solution(
            orders, {"a" * 112: True, "c" * 112: False}, {USDC: ONE}, WETH
        )
        assert valuation.pair_surplus == {(WETH, USDC): 100}
        assert valuation.winner_pairs == {(WETH, USDC), (DAI, USDC)}

    def test_native_sentinel_is_normalised_only_for_winner_pairs(self):
        """Step 1 keys on the raw token, step 5 on `as_erc20` — an asymmetry that is in
        the Rust and is mirrored rather than tidied up."""
        orders = [sell_order(1000, 2100, sell_token=NATIVE_TOKEN)]
        valuation = value_solution(
            orders, {"a" * 112: True}, {USDC: ONE}, WETH
        )
        assert valuation.pair_surplus == {(NATIVE_TOKEN, USDC): 100}
        assert valuation.winner_pairs == {(WETH, USDC)}

    def test_a_failing_contributing_order_fails_the_whole_solution(self):
        orders = [
            sell_order(1000, 2100, uid="a" * 112),
            sell_order(1000, 1900, uid="c" * 112),
        ]
        with pytest.raises(ValuationError):
            value_solution(orders, {"a" * 112: True, "c" * 112: True}, {USDC: ONE}, WETH)

    def test_a_failing_non_contributing_order_is_harmless(self):
        orders = [
            sell_order(1000, 2100, uid="a" * 112),
            sell_order(1000, 1900, uid="c" * 112),
        ]
        valuation = value_solution(
            orders, {"a" * 112: True, "c" * 112: False}, {USDC: ONE}, WETH
        )
        assert valuation.total == 100
        # Volumes track contributing orders exactly, like `order_surplus_native`.
        assert valuation.order_volume_native == {"a" * 112: 2100}


class TestSolutionTotal:
    def make(self, pair_surplus: dict[Pair, int]) -> SolutionValuation:
        return SolutionValuation(
            pair_surplus=pair_surplus,
            order_surplus_native={},
            order_surplus_atoms={},
            winner_pairs=frozenset(pair_surplus),
            order_uids=frozenset(),
        )

    def test_surplus_mode_sums_the_decomposition(self):
        assert solution_total(self.make({(WETH, USDC): 60, (DAI, USDC): 40}), "surplus") == 100

    def test_score_mode_takes_the_recorded_score(self):
        """Score mode deliberately leaves the total and the sum of the pair values
        different: the total carries protocol fees, the pair values do not."""
        valuation = self.make({(WETH, USDC): 60, (DAI, USDC): 40})
        assert solution_total(valuation, "score", db_score=250) == 250
        assert valuation.total == 100

    def test_score_mode_needs_a_score(self):
        with pytest.raises(ValueError):
            solution_total(self.make({(WETH, USDC): 1}), "score")
