"""Protocol and partner fees per order, mirroring `arbitrator.rs`.

The fee half of the CIP-38 score. `valuation.py` supplies user surplus; this module
supplies the rest, so `surplus + fees` reproduces `proposed_solutions.score`.

Reproduced wei-exactly against the recorded score for 19,653 of 19,653 solutions over
auctions 13505000-13509867 — see PROPOSAL-fee-aware-scoring.md.

Integer arithmetic throughout, **except** where the Rust itself detours through `f64`.
Those detours are load-bearing and are reproduced rather than improved on; see
`mul_f64`.

## Protocol fee versus partner fee

`protocol_fees` (`arbitrator.rs:260`) returns one number, and that number is what enters
the score. It is not what the protocol receives: an order's policy list ends with one
entry per partner declared in its app data, and those are passed on. `fee_split` splits
it by the trailing-`n` rule.

**The naming here follows budget usage, not the database's.** In the dbt models — and in
`arbitrator.rs` — "protocol fee" means the *gross* amount including the partners' share
(`protocol_fee_native`), and the net figure needs the mouthful
`protocol_fee_without_partner_fee_native`. In budget discussions "protocol fee" means
what the protocol actually keeps. So on `FeeSplit`:

| field | meaning | dbt equivalent |
| --- | --- | --- |
| `total` | every policy — what enters the score | `protocol_fee_native` |
| `partner` | the trailing `n`, passed on | `partner_fee_native` |
| `protocol` | what the protocol keeps | `protocol_fee_without_partner_fee_native` |

Read `total` wherever the DB or the Rust says "protocol fee", and `protocol` wherever a
budget does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

from .primitives import ONE_ETH, ceil_div

if TYPE_CHECKING:  # `valuation` imports this module, so a runtime import would cycle.
    from .valuation import Order

PolicyKind = Literal["surplus", "volume", "priceimprovement"]


class Prices(Protocol):
    """Structural stand-in for `valuation.ClearingPrices`.

    Declared rather than imported because `valuation` imports this module: the fee path
    needs to *construct* adjusted prices, and a structural type is cheaper than moving
    the dataclass into `primitives` or importing at call time.
    """

    @property
    def sell(self) -> int: ...

    @property
    def buy(self) -> int: ...


class _Prices(NamedTuple):
    sell: int
    buy: int


class FeeError(Exception):
    """A fee could not be computed.

    Mirrors `MathError` in the fee path plus the missing-quote case. Like
    `ValuationError`, any such failure discards the whole solution — the Rust propagates
    it out of `score_by_token_pair` (`arbitrator.rs:152`).
    """


@dataclass(frozen=True)
class Policy:
    """One fee policy, as `stg_backend_data__fee_policies` records it.

    Read in ascending `application_order`, which is a **global** serial rather than a
    per-order index, so only its relative order within one order is meaningful.
    """

    kind: PolicyKind
    surplus_factor: float | None = None
    surplus_max_volume_factor: float | None = None
    volume_factor: float | None = None
    price_improvement_factor: float | None = None
    price_improvement_max_volume_factor: float | None = None


@dataclass(frozen=True)
class Quote:
    """The order's quote, for `priceimprovement` policies.

    `fee` is not stored: it is `gas_amount * gas_price / sell_token_price` from
    `stg_backend_data__order_quotes`, evaluated as an exact rational and floored — see
    `quote_fee`.
    """

    sell_amount: int
    buy_amount: int
    fee: int


def mul_f64(amount: int, factor: float) -> int:
    """`U256::checked_mul_f64` (`number/src/u256_ext.rs:105`).

    The conversion of `factor * 1e18` to an integer **rounds**; it does not truncate.
    Truncating instead reproduces 99.1% of scores and leaves a tail of small negative
    errors. Evaluating the product as an exact rational is also wrong, and by more than
    half as much again — the protocol's own `f64` detour is part of the definition of
    the fee, not an approximation of it.
    """
    if factor == 1.0:  # identity short-circuit, present in the Rust to avoid overflow
        return amount
    return amount * round(factor * 1e18) // ONE_ETH


def quote_fee(gas_amount: float, gas_price: float, sell_token_price: float) -> int:
    """The quote's fee in sell-token atoms (`autopilot/.../dto/quote.rs:16`).

    Evaluated over `BigRational` and floored by `big_rational_to_u256`, so the three
    `double precision` columns must be widened to exact rationals *before* dividing.
    Doing it in floating point rounds the quotient the wrong way often enough to shift
    the fee.
    """
    if sell_token_price == 0.0:
        raise FeeError("quote has a zero sell token price")
    value = Fraction(gas_amount) * Fraction(gas_price) / Fraction(sell_token_price)
    if value < 0:
        raise FeeError(f"negative quote fee ({float(value)})")
    return value.numerator // value.denominator


def _sell_amount(order: Order, prices: Prices) -> int:
    """What left the user's wallet (`arbitrator.rs:521`)."""
    if order.side == "sell":
        return order.executed_sell
    if prices.sell == 0:
        raise FeeError(f"order {order.uid}: zero sell price")
    return order.executed_buy * prices.buy // prices.sell


def _buy_amount(order: Order, prices: Prices) -> int:
    """What the user received (`arbitrator.rs:531`)."""
    if order.side == "buy":
        return order.executed_buy
    if prices.buy == 0:
        raise FeeError(f"order {order.uid}: zero buy price")
    return ceil_div(order.executed_sell * prices.sell, prices.buy)


def _surplus_over(
    order: Order, prices: Prices, limit_sell: int, limit_buy: int
) -> int:
    """`surplus_over` (`arbitrator.rs:351`), duplicated from `valuation` because the fee
    path calls it with quote-derived limits and with fee-adjusted prices, neither of
    which the surplus path has a use for."""
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
    """A cut of surplus, at the adjusted factor (`arbitrator.rs:445`).

    The trade already has the fee applied, so the factor is re-expressed as
    `factor / (1 - factor)` — an `f64` division, deliberately.
    """
    if factor >= 1.0:
        raise FeeError(f"surplus factor {factor} is not below 1")
    return mul_f64(surplus, factor / (1.0 - factor))


def _volume_fee(order: Order, prices: Prices, factor: float) -> int:
    """A cut of volume, at the side-dependent adjusted factor (`arbitrator.rs:461`)."""
    if order.side == "sell":
        if factor >= 1.0:
            raise FeeError(f"volume factor {factor} is not below 1")
        return mul_f64(_buy_amount(order, prices), factor / (1.0 - factor))
    return mul_f64(_sell_amount(order, prices), factor / (1.0 + factor))


def _adjust_quote_to_order_limits(order: Order, quote: Quote) -> tuple[int, int]:
    """`adjust_quote_to_order_limits` (`arbitrator.rs:399`).

    The `max`/`min` against the order's own limits is what handles out-of-market orders:
    a quote better than the limit price cannot make the price-improvement baseline worse
    than the limit.
    """
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
    """`price_improvement_over_quote` (`arbitrator.rs:383`) — no improvement is 0, not an
    error."""
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
    """One policy's fee in the surplus token (`arbitrator.rs:305`)."""
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
    """`calculate_custom_prices` (`arbitrator.rs:495`) — the prices the trade would have
    had without the fees accumulated so far."""
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
    """Each policy's fee in the surplus token, **in application order**.

    `protocol_fees` (`arbitrator.rs:260`) walks the list in **reverse**, each policy
    seeing prices adjusted for everything charged after it. So the trailing partner
    policies are charged first, on the unadjusted prices, and the DAO's policies see
    prices net of them. The returned tuple is re-indexed back to application order, so
    `fee_split` can take the trailing `n` without having to think about the direction.
    """
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


@dataclass(frozen=True)
class FeeSplit:
    """One order's fees in the surplus token."""

    total: int
    """Every policy. This is what `arbitrator.rs` adds to surplus to get the score."""
    partner: int
    """The trailing `n` policies, passed on to the partners in the order's app data."""

    @property
    def protocol(self) -> int:
        """What the protocol keeps, net of the partners' share — the number a solver's
        value *to the protocol* is measured in, and what a budget calls the protocol fee.
        The DB calls this `protocol_fee_without_partner_fee_native`; its own
        `protocol_fee_native` is `total`."""
        return self.total - self.partner


def fee_split(fees: Sequence[int], partner_count: int) -> FeeSplit:
    """Split per-policy fees into the partners' share and the DAO's.

    The rule is `int_backend_data__fees_per_trade`: the trailing `n` positions are
    partner fees, where `n` is the number of recipients in the order's app data, with one
    guard — a **single** policy is always DAO, however many partners the app data
    declares. Reproduces that model's split exactly on all 16,431 settled trades of
    2026-08-01..04.
    """
    total = sum(fees)
    if len(fees) <= 1 or partner_count <= 0:
        return FeeSplit(total=total, partner=0)
    if partner_count >= len(fees):
        # The model drops these trades outright rather than guessing which positions are
        # whose. Refusing is the same decision: the caller reports it as missing data.
        raise FeeError(
            f"{partner_count} partners declared but only {len(fees)} fee policies; "
            f"the split is not determined"
        )
    return FeeSplit(total=total, partner=sum(fees[len(fees) - partner_count :]))


@dataclass(frozen=True)
class FeeContext:
    """Everything one auction needs to compute protocol fees.

    `known` is the coverage test, and it is the whole reason this is not simply a
    lookup. `stg_backend_data__fee_policies` records an order's policies for an auction
    **iff** the order appears in a solution of that auction that was not filtered out
    (`autopilot/src/run_loop.rs:551`), so an order with no rows is either genuinely
    fee-free or unrecorded, and only membership of `known` separates the two. See
    docs/analytics-db.md.
    """

    policies: Mapping[str, tuple[Policy, ...]]
    quotes: Mapping[str, Quote]
    partner_counts: Mapping[str, int]
    """Partner recipients declared in the order's app data — the `n` of `fee_split`."""
    known: frozenset[str]
    """Order uids whose policy list is recorded for this auction, empty list included."""


EMPTY_FEE_CONTEXT = FeeContext(
    policies={}, quotes={}, partner_counts={}, known=frozenset()
)
"""Used when fees are not being computed at all, so every lookup reports 'unknown'."""


def order_fees(order: Order, context: FeeContext) -> FeeSplit | None:
    """One order's fees, or `None` when its policies are not recorded for this auction.

    A JIT order is never in `auction.orders`, so it never carries policies and its fee is
    exactly 0 — but it is also never in `known`, so callers must decide that case before
    reaching here, from whether the order contributes to score at all.
    """
    if order.uid not in context.known:
        return None
    policies = context.policies.get(order.uid, ())
    if not policies:
        return FeeSplit(total=0, partner=0)
    base = _Prices(sell=order.executed_buy, buy=order.executed_sell)
    fees = policy_fees(order, policies, base, context.quotes.get(order.uid))
    return fee_split(fees, context.partner_counts.get(order.uid, 0))
