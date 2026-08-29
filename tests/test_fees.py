"""Tests for the protocol- and partner-fee formulas.

The reference numbers come from auction 13509109 solution 0, a sell order carrying all
three policy kinds at once — `priceimprovement(0.5, 0.0098)` then two volume policies,
the last of which is a partner fee. It is the case that exposed the `mul_f64` rounding,
so it is worth keeping as a fixture rather than a synthetic example.
"""

from __future__ import annotations

import pytest

from loo.fees import (
    EMPTY_FEE_CONTEXT,
    FeeContext,
    FeeError,
    FeeSplit,
    Policy,
    Quote,
    fee_split,
    mul_f64,
    order_fees,
    policy_fees,
    quote_fee,
)
from loo.valuation import ClearingPrices, Order, value_solution

WETH = "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
UID = "e1e11c8242da276c0ccb8e72787c89cf697c329aa9b3c83ec286d690c1f3eec2e47afc755ab0ac66d7ede9b0d4e61cdd6d28934e6a710e4c"

# Auction 13509109, solution 0.
LIVE_ORDER = Order(
    uid=UID,
    sell_token=USDC,
    buy_token="ee" * 20,
    sell_amount=120500000000000000000,
    buy_amount=119140722704767911115,
    executed_sell=120500000000000000000,
    executed_buy=120103953122547755793,
    side="sell",
)
LIVE_POLICIES = (
    Policy(
        kind="priceimprovement",
        price_improvement_factor=0.5,
        price_improvement_max_volume_factor=0.0098,
    ),
    Policy(kind="volume", volume_factor=3e-05),
    Policy(kind="volume", volume_factor=0.003),
)
LIVE_QUOTE = Quote(
    sell_amount=120500000000000000000,
    buy_amount=120466675793142735132,
    fee=126819731316335,
)
LIVE_TOTAL_FEE = 367424346879097137
"""Reproduces `proposed_solutions.score` for that solution to the wei."""


def base_prices(order: Order) -> ClearingPrices:
    return ClearingPrices(sell=order.executed_buy, buy=order.executed_sell)


class TestMulF64:
    def test_identity_short_circuits(self) -> None:
        assert mul_f64(12345, 1.0) == 12345

    def test_rounds_rather_than_truncates(self) -> None:
        """The whole reason the live case reproduces.

        `0.003 / (1 - 0.003)` scaled by 1e18 lands on ...731.3, and the protocol's
        `U256::from(f64)` rounds. Truncating gives a smaller fee, which is what left a
        tail of negative score differences before this was pinned down.
        """
        factor = 0.003 / (1.0 - 0.003)
        scaled = factor * 1e18
        assert int(scaled) != round(scaled) or scaled == int(scaled)
        amount = 120103953122547755793
        assert mul_f64(amount, factor) == amount * round(scaled) // 10**18


class TestQuoteFee:
    def test_live_quote(self) -> None:
        assert quote_fee(399631.0, 317298105.0, 0.999861438619223) == 126819731316335

    def test_exact_rational_not_float(self) -> None:
        """A float divide would round; the protocol floors an exact rational."""
        assert quote_fee(3.0, 1.0, 3.0) == 1
        assert quote_fee(1.0, 1.0, 3.0) == 0

    def test_zero_price_is_an_error(self) -> None:
        with pytest.raises(FeeError, match="zero sell token price"):
            quote_fee(1.0, 1.0, 0.0)


class TestPolicyFees:
    def test_live_order_total(self) -> None:
        fees = policy_fees(LIVE_ORDER, LIVE_POLICIES, base_prices(LIVE_ORDER), LIVE_QUOTE)
        assert sum(fees) == LIVE_TOTAL_FEE

    def test_returned_in_application_order(self) -> None:
        """`protocol_fees` walks the list backwards; the result is re-indexed forwards so
        the trailing partner entries stay at the end."""
        fees = policy_fees(LIVE_ORDER, LIVE_POLICIES, base_prices(LIVE_ORDER), LIVE_QUOTE)
        assert len(fees) == len(LIVE_POLICIES)
        # The 0.003 volume policy is applied first (it is last in the list) and on
        # unadjusted prices, so it is much the largest of the three.
        assert fees[2] > fees[1] and fees[2] > fees[0]

    def test_price_improvement_needs_a_quote(self) -> None:
        with pytest.raises(FeeError, match="needs the order's quote"):
            policy_fees(LIVE_ORDER, LIVE_POLICIES, base_prices(LIVE_ORDER), None)

    def test_empty_policy_list_is_zero(self) -> None:
        assert policy_fees(LIVE_ORDER, (), base_prices(LIVE_ORDER), None) == ()


class TestFeeSplit:
    def test_trailing_n_are_partner(self) -> None:
        split = fee_split([10, 20, 30], partner_count=1)
        assert split == FeeSplit(total=60, partner=30)
        assert split.protocol == 30

    def test_two_partners(self) -> None:
        assert fee_split([10, 20, 30], partner_count=2) == FeeSplit(total=60, partner=50)

    def test_no_partners(self) -> None:
        assert fee_split([10, 20], partner_count=0) == FeeSplit(total=30, partner=0)

    def test_single_policy_is_always_dao(self) -> None:
        """The `L = 1` guard `int_backend_data__fees_per_trade` applies: one fee amount
        is treated as DAO-only however many partners the app data declares."""
        assert fee_split([42], partner_count=1) == FeeSplit(total=42, partner=0)

    def test_more_partners_than_policies_is_refused(self) -> None:
        """The dbt model drops these trades rather than guessing; refusing is the same
        decision, surfaced to the caller as missing data."""
        with pytest.raises(FeeError, match="not determined"):
            fee_split([10, 20], partner_count=2)


class TestOrderFees:
    @staticmethod
    def context(
        *,
        policies: dict[str, tuple[Policy, ...]] | None = None,
        known: frozenset[str] | None = None,
    ) -> FeeContext:
        return FeeContext(
            policies={UID: LIVE_POLICIES} if policies is None else policies,
            quotes={UID: LIVE_QUOTE},
            partner_counts={UID: 1},
            known=frozenset({UID}) if known is None else known,
        )

    def test_live_split(self) -> None:
        split = order_fees(LIVE_ORDER, self.context())
        assert split is not None
        assert split.total == LIVE_TOTAL_FEE
        assert split.partner > 0
        assert split.protocol == split.total - split.partner

    def test_unknown_when_not_covered(self) -> None:
        """An order absent from `known` has unrecorded policies, which must not read as
        a fee of zero."""
        assert order_fees(LIVE_ORDER, self.context(known=frozenset())) is None

    def test_known_but_no_rows_is_zero(self) -> None:
        """Covered and with no policy rows means genuinely fee-free."""
        split = order_fees(LIVE_ORDER, self.context(policies={}))
        assert split == FeeSplit(total=0, partner=0)

    def test_empty_context_reports_everything_unknown(self) -> None:
        assert order_fees(LIVE_ORDER, EMPTY_FEE_CONTEXT) is None


class TestValuationIntegration:
    def test_surplus_plus_fee_is_the_score(self) -> None:
        """The decomposition is exactly additive against `compute_order_score`, which is
        what lets the recorded score validate it."""
        prices = {"ee" * 20: 10**18}
        valuation = value_solution(
            [LIVE_ORDER],
            {UID: True},
            prices,
            WETH,
            FeeContext(
                policies={UID: LIVE_POLICIES},
                quotes={UID: LIVE_QUOTE},
                partner_counts={UID: 1},
                known=frozenset({UID}),
            ),
        )
        assert valuation.fees_complete
        surplus = valuation.order_surplus_native[UID]
        # Auction 13509109 solution 0's recorded score.
        assert surplus + valuation.fee_native == 1330654764658941815

    def test_shares_are_non_negative_and_sum(self) -> None:
        prices = {"ee" * 20: 10**18}
        valuation = value_solution(
            [LIVE_ORDER],
            {UID: True},
            prices,
            WETH,
            FeeContext(
                policies={UID: LIVE_POLICIES},
                quotes={UID: LIVE_QUOTE},
                partner_counts={UID: 1},
                known=frozenset({UID}),
            ),
        )
        assert valuation.partner_fee_native >= 0
        assert valuation.protocol_fee_native_net >= 0
        assert (
            valuation.partner_fee_native + valuation.protocol_fee_native_net
            == valuation.fee_native
        )

    def test_without_a_context_fees_are_unknown_not_zero(self) -> None:
        prices = {"ee" * 20: 10**18}
        valuation = value_solution([LIVE_ORDER], {UID: True}, prices, WETH)
        assert not valuation.fees_complete
        assert valuation.fees_unknown == frozenset({UID})
        assert valuation.fee_native == 0  # nothing recorded, so nothing summed
