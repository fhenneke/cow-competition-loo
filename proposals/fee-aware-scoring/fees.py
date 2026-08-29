"""Prototype: recompute CIP-38 score (surplus + protocol fees) from the analytics DB.

Mirrors arbitrator.rs protocol_fees / compute_order_score exactly, including the f64
rounding of `checked_mul_f64` and the floor of `big_rational_to_u256`.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

SCALE = 10**18


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def mul_f64(x: int, factor: float) -> int:
    """`U256::checked_mul_f64` (number/src/u256_ext.rs:105)."""
    if factor == 1.0:
        return x
    scaled = round(factor * 1e18)        # f64 multiply, then round to integer
    return x * scaled // SCALE


@dataclass(frozen=True)
class Order:
    uid: str
    side: str                 # "sell" | "buy"
    sell_amount: int
    buy_amount: int
    executed_sell: int
    executed_buy: int


@dataclass(frozen=True)
class Prices:
    sell: int
    buy: int


@dataclass(frozen=True)
class Policy:
    kind: str                 # surplus | volume | priceimprovement
    surplus_factor: float | None
    surplus_max_volume_factor: float | None
    volume_factor: float | None
    pi_factor: float | None
    pi_max_volume_factor: float | None


@dataclass(frozen=True)
class Quote:
    sell_amount: int
    buy_amount: int
    fee: int


class MathError(Exception):
    pass


def custom_prices_from_executed(o: Order) -> Prices:
    return Prices(sell=o.executed_buy, buy=o.executed_sell)


def surplus_over(o: Order, p: Prices, limit_sell: int, limit_buy: int) -> int:
    executed = o.executed_buy if o.side == "buy" else o.executed_sell
    if o.side == "buy":
        if limit_buy == 0 or p.sell == 0:
            raise MathError("division by zero")
        surplus = (limit_sell * executed // limit_buy) - (executed * p.buy // p.sell)
    else:
        if limit_sell == 0 or p.buy == 0:
            raise MathError("division by zero")
        surplus = ceil_div(executed * p.sell, p.buy) - ceil_div(executed * limit_buy, limit_sell)
    if surplus < 0:
        raise MathError("negative")
    return surplus


def surplus_over_limit_price(o: Order, p: Prices) -> int:
    return surplus_over(o, p, o.sell_amount, o.buy_amount)


def sell_amount(o: Order, p: Prices) -> int:
    return o.executed_sell if o.side == "sell" else o.executed_buy * p.buy // p.sell


def buy_amount(o: Order, p: Prices) -> int:
    return ceil_div(o.executed_sell * p.sell, p.buy) if o.side == "sell" else o.executed_buy


def surplus_fee(surplus: int, factor: float) -> int:
    return mul_f64(surplus, factor / (1.0 - factor))


def volume_fee(o: Order, p: Prices, factor: float) -> int:
    executed = buy_amount(o, p) if o.side == "sell" else sell_amount(o, p)
    adjusted = factor / (1.0 - factor) if o.side == "sell" else factor / (1.0 + factor)
    return mul_f64(executed, adjusted)


def adjust_quote_to_order_limits(o: Order, q: Quote) -> tuple[int, int]:
    if o.side == "sell":
        if q.sell_amount == 0:
            raise MathError("zero quote sell amount")
        quote_buy = q.buy_amount - q.fee * q.buy_amount // q.sell_amount
        if quote_buy < 0:
            raise MathError("negative")
        scaled_buy = quote_buy * o.sell_amount // q.sell_amount
        return o.sell_amount, max(o.buy_amount, scaled_buy)
    if q.buy_amount == 0:
        raise MathError("zero quote buy amount")
    quote_sell = q.sell_amount + q.fee
    scaled_sell = quote_sell * o.buy_amount // q.buy_amount
    return min(o.sell_amount, scaled_sell), o.buy_amount


def price_improvement_over_quote(o: Order, p: Prices, q: Quote) -> int:
    ls, lb = adjust_quote_to_order_limits(o, q)
    try:
        return surplus_over(o, p, ls, lb)
    except MathError as e:
        if str(e) == "negative":
            return 0
        raise


def protocol_fee(o: Order, policy: Policy, p: Prices, quote: Quote | None) -> int:
    if policy.kind == "surplus":
        assert policy.surplus_factor is not None
        assert policy.surplus_max_volume_factor is not None
        s = surplus_over_limit_price(o, p)
        return min(surplus_fee(s, policy.surplus_factor),
                   volume_fee(o, p, policy.surplus_max_volume_factor))
    if policy.kind == "volume":
        assert policy.volume_factor is not None
        return volume_fee(o, p, policy.volume_factor)
    if policy.kind == "priceimprovement":
        assert policy.pi_factor is not None
        assert policy.pi_max_volume_factor is not None
        if quote is None:
            raise MathError("missing quote for price improvement policy")
        pi = price_improvement_over_quote(o, p, quote)
        return min(surplus_fee(pi, policy.pi_factor),
                   volume_fee(o, p, policy.pi_max_volume_factor))
    raise MathError(f"unknown policy kind {policy.kind}")


def calculate_custom_prices(o: Order, fee: int, base: Prices) -> Prices:
    sa, ba = sell_amount(o, base), buy_amount(o, base)
    if o.side == "sell":
        return Prices(sell=ba + fee, buy=sa)
    if sa - fee < 0:
        raise MathError("negative")
    return Prices(sell=ba, buy=sa - fee)


def protocol_fees(o: Order, policies: list[Policy], base: Prices, quote: Quote | None) -> int:
    """`arbitrator.rs:260` — policies applied in reverse order."""
    total = 0
    current = base
    for i in range(len(policies) - 1, -1, -1):
        total += protocol_fee(o, policies[i], current, quote)
        if i != 0:
            current = calculate_custom_prices(o, total, base)
    return total


def order_score_native(
    o: Order, policies: list[Policy], quote: Quote | None, native_price_buy: int
) -> tuple[int, int]:
    """Returns (score_native, protocol_fee_in_surplus_token)."""
    base = custom_prices_from_executed(o)
    user_surplus = surplus_over_limit_price(o, base)
    fees = protocol_fees(o, policies, base, quote)
    total = user_surplus + fees
    if o.side == "buy":
        if o.sell_amount == 0:
            raise MathError("zero limit sell amount")
        total_buy = total * o.buy_amount // o.sell_amount
    else:
        total_buy = total
    return native_price_buy * total_buy // SCALE, fees


def quote_fee(gas_amount: float, gas_price: float, sell_token_price: float) -> int:
    """`dto/quote.rs` — exact rational, floored."""
    if sell_token_price == 0.0:
        raise MathError("zero sell token price")
    value = Fraction(gas_amount) * Fraction(gas_price) / Fraction(sell_token_price)
    if value < 0:
        raise MathError("negative quote fee")
    return value.numerator // value.denominator
