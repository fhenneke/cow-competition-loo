"""Per-order fees in surplus-token atoms (`arbitrator.rs`). The Rust's `f64` detours
define the fee and are reproduced, not improved on."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

from .primitives import ONE_ETH, ceil_div

if TYPE_CHECKING:  # `valuation` imports this module, so a runtime import would cycle.
    from .valuation import Order

PolicyKind = Literal["surplus", "volume", "priceimprovement"]


class Prices(Protocol):
    """Structural `valuation.ClearingPrices`; importing it would cycle."""

    @property
    def sell(self) -> int: ...

    @property
    def buy(self) -> int: ...


class _Prices(NamedTuple):
    sell: int
    buy: int


class FeeError(Exception):
    """In the Rust this discards the whole solution (`arbitrator.rs:152`)."""


@dataclass(frozen=True)
class Policy:
    """A `stg_backend_data__fee_policies` row. `application_order` is a global serial:
    only its relative order within one order means anything."""

    kind: PolicyKind
    surplus_factor: float | None = None
    surplus_max_volume_factor: float | None = None
    volume_factor: float | None = None
    price_improvement_factor: float | None = None
    price_improvement_max_volume_factor: float | None = None


@dataclass(frozen=True)
class Quote:
    sell_amount: int
    buy_amount: int
    fee: int
    """Not stored; derived by `quote_fee`."""


def mul_f64(amount: int, factor: float) -> int:
    """`U256::checked_mul_f64` (`u256_ext.rs:105`): `factor * 1e18` rounds, not truncates."""
    if factor == 1.0:  # identity short-circuit, present in the Rust to avoid overflow
        return amount
    return amount * round(factor * 1e18) // ONE_ETH


@lru_cache(maxsize=1 << 16)
def quote_fee(gas_amount: float, gas_price: float, sell_token_price: float) -> int:
    """`dto/quote.rs:16`: exact rational, floored; widen before dividing. Cached: an
    order's quote is read again in every chunk of auctions it trades in."""
    if sell_token_price == 0.0:
        raise FeeError("quote has a zero sell token price")
    value = Fraction(gas_amount) * Fraction(gas_price) / Fraction(sell_token_price)
    if value < 0:
        raise FeeError(f"negative quote fee ({float(value)})")
    return value.numerator // value.denominator


def _sell_amount(order: Order, prices: Prices) -> int:
    """`arbitrator.rs:521`."""
    if order.side == "sell":
        return order.executed_sell
    if prices.sell == 0:
        raise FeeError(f"order {order.uid}: zero sell price")
    return order.executed_buy * prices.buy // prices.sell


def _buy_amount(order: Order, prices: Prices) -> int:
    """`arbitrator.rs:531`."""
    if order.side == "buy":
        return order.executed_buy
    if prices.buy == 0:
        raise FeeError(f"order {order.uid}: zero buy price")
    return ceil_div(order.executed_sell * prices.sell, prices.buy)


def _surplus_over(
    order: Order, prices: Prices, limit_sell: int, limit_buy: int
) -> int:
    """`arbitrator.rs:351`; like `valuation.surplus_over` but raising `FeeError`."""
    executed = order.executed_buy if order.side == "buy" else order.executed_sell

    if order.side == "buy":
        if limit_buy == 0:
            raise FeeError(f"order {order.uid}: zero limit buy amount")
        if prices.sell == 0:
            raise FeeError(f"order {order.uid}: zero sell price")
        surplus = limit_sell * executed // limit_buy - executed * prices.buy // prices.sell
    else:
        if limit_sell == 0:
            raise FeeError(f"order {order.uid}: zero limit sell amount")
        if prices.buy == 0:
            raise FeeError(f"order {order.uid}: zero buy price")
        surplus = ceil_div(executed * prices.sell, prices.buy) - ceil_div(
            executed * limit_buy, limit_sell
        )

    if surplus < 0:
        raise FeeError(f"order {order.uid}: negative surplus ({surplus})")
    return surplus


def _surplus_fee(surplus: int, factor: float) -> int:
    """`arbitrator.rs:445`; `factor / (1 - factor)` in `f64`, deliberately."""
    if factor >= 1.0:
        raise FeeError(f"surplus factor {factor} is not below 1")
    return mul_f64(surplus, factor / (1.0 - factor))


def _volume_fee(order: Order, prices: Prices, factor: float) -> int:
    """`arbitrator.rs:461`."""
    if order.side == "sell":
        if factor >= 1.0:
            raise FeeError(f"volume factor {factor} is not below 1")
        return mul_f64(_buy_amount(order, prices), factor / (1.0 - factor))
    return mul_f64(_sell_amount(order, prices), factor / (1.0 + factor))


def _adjust_quote_to_order_limits(order: Order, quote: Quote) -> tuple[int, int]:
    """`arbitrator.rs:399`."""
    if order.side == "sell":
        if quote.sell_amount == 0:
            raise FeeError(f"order {order.uid}: zero quote sell amount")
        quote_buy = quote.buy_amount - quote.fee * quote.buy_amount // quote.sell_amount
        if quote_buy < 0:
            raise FeeError(f"order {order.uid}: quote fee exceeds quote buy amount")
        scaled_buy = quote_buy * order.sell_amount // quote.sell_amount
        return order.sell_amount, max(order.buy_amount, scaled_buy)

    if quote.buy_amount == 0:
        raise FeeError(f"order {order.uid}: zero quote buy amount")
    scaled_sell = (quote.sell_amount + quote.fee) * order.buy_amount // quote.buy_amount
    return min(order.sell_amount, scaled_sell), order.buy_amount


def _price_improvement(order: Order, prices: Prices, quote: Quote) -> int:
    """`arbitrator.rs:383`; no improvement is 0, not an error."""
    limit_sell, limit_buy = _adjust_quote_to_order_limits(order, quote)
    try:
        return _surplus_over(order, prices, limit_sell, limit_buy)
    except FeeError as error:
        if "negative surplus" in str(error):
            return 0
        raise


def _policy_fee(
    order: Order, policy: Policy, prices: Prices, quote: Quote | None
) -> int:
    """`arbitrator.rs:305`."""
    if policy.kind == "surplus":
        if policy.surplus_factor is None or policy.surplus_max_volume_factor is None:
            raise FeeError(f"order {order.uid}: surplus policy is missing its factors")
        surplus = _surplus_over(order, prices, order.sell_amount, order.buy_amount)
        return min(
            _surplus_fee(surplus, policy.surplus_factor),
            _volume_fee(order, prices, policy.surplus_max_volume_factor),
        )

    if policy.kind == "volume":
        if policy.volume_factor is None:
            raise FeeError(f"order {order.uid}: volume policy is missing its factor")
        return _volume_fee(order, prices, policy.volume_factor)

    if policy.price_improvement_factor is None or (
        policy.price_improvement_max_volume_factor is None
    ):
        raise FeeError(f"order {order.uid}: price improvement policy is missing its factors")
    if quote is None:
        raise FeeError(
            f"order {order.uid}: price improvement policy needs the order's quote, "
            f"which stg_backend_data__order_quotes does not have"
        )
    improvement = _price_improvement(order, prices, quote)
    return min(
        _surplus_fee(improvement, policy.price_improvement_factor),
        _volume_fee(order, prices, policy.price_improvement_max_volume_factor),
    )


def _prices_excluding_fees(order: Order, fee: int, base: Prices) -> Prices:
    """`calculate_custom_prices` (`arbitrator.rs:495`)."""
    sold, bought = _sell_amount(order, base), _buy_amount(order, base)
    if order.side == "sell":
        return _Prices(sell=bought + fee, buy=sold)
    if sold - fee < 0:
        raise FeeError(f"order {order.uid}: fees exceed the sold amount")
    return _Prices(sell=bought, buy=sold - fee)


def policy_fees(
    order: Order,
    policies: Sequence[Policy],
    base: Prices,
    quote: Quote | None,
) -> tuple[int, ...]:
    """Per-policy fees in application order, computed in reverse (`arbitrator.rs:260`)."""
    fees = [0] * len(policies)
    total = 0
    prices = base

    for index in range(len(policies) - 1, -1, -1):
        fee = _policy_fee(order, policies[index], prices, quote)
        fees[index] = fee
        total += fee
        if index != 0:
            prices = _prices_excluding_fees(order, total, base)

    return tuple(fees)


class FeeSplit(NamedTuple):
    total: int
    """Every policy: what enters the score; the DB's `protocol_fee_native`."""
    partner: int

    @property
    def protocol(self) -> int:
        """What the protocol keeps; the DB's `protocol_fee_without_partner_fee_native`."""
        return self.total - self.partner


def fee_split(fees: Sequence[int], partner_count: int) -> FeeSplit:
    """Partner fees are the trailing `partner_count` policies, but a single policy is
    always the protocol's (`int_backend_data__fees_per_trade`)."""
    total = sum(fees)
    if len(fees) <= 1 or partner_count <= 0:
        return FeeSplit(total=total, partner=0)
    if partner_count >= len(fees):
        # Undetermined; the dbt model drops these trades too.
        raise FeeError(
            f"{partner_count} partners declared but only {len(fees)} fee policies; "
            f"the split is not determined"
        )
    return FeeSplit(total=total, partner=sum(fees[len(fees) - partner_count :]))


@dataclass(frozen=True)
class FeeContext:
    """Policies are recorded iff the order is in a kept solution (`run_loop.rs:551`), so
    only `known` separates fee-free from unrecorded."""

    policies: Mapping[str, tuple[Policy, ...]]
    quotes: Mapping[str, Quote]
    partner_counts: Mapping[str, int]
    """Partner recipients in the order's app data."""
    known: frozenset[str]
    """Orders whose policy list is recorded, empty list included."""


EMPTY_FEE_CONTEXT = FeeContext(
    policies={}, quotes={}, partner_counts={}, known=frozenset()
)
"""Every fee reads as unrecorded."""


def order_fees(order: Order, context: FeeContext) -> FeeSplit | None:
    """`None` when the order's policies are not recorded for this auction."""
    if order.uid not in context.known:
        return None
    policies = context.policies.get(order.uid, ())
    if not policies:
        return FeeSplit(total=0, partner=0)
    base = _Prices(sell=order.executed_buy, buy=order.executed_sell)
    fees = policy_fees(order, policies, base, context.quotes.get(order.uid))
    return fee_split(fees, context.partner_counts.get(order.uid, 0))
