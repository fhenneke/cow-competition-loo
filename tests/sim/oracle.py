"""An independent, deliberately naive transcription of the mechanism.

Written from docs/winner-selection.md, docs/rewards.md and docs/quotes.md (and the
`arbitrator.rs` / `quote.rs` behaviour they describe), never by importing `loo`. The
world generator uses it to *record* a competition — scores, filter verdicts, winners,
reference scores, mart rewards, quote winners — so `loo` is checked against a record it
had no hand in. Under a mutation of `loo`, this module is untouched, which is the point.

Clarity beats speed here: everything is integers or `Fraction`, except where the
protocol itself detours through `f64` (the fee factors and the quote ranking), which
are reproduced operation for operation because they are part of the definition.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

ONE = 10**18
NATIVE = "ee" * 20
LAMPORTS = 10**9
"""Solana's `NATIVE_PRICE_DENOMINATOR`: its prices are lamports per atom × 1e9."""

Pair = tuple[str, str]


class Refused(Exception):
    """The protocol's arithmetic refuses this input (a division by zero or a negative
    surplus): the Rust drops the whole solution."""


# --- orders, surplus, fees ------------------------------------------------------------


@dataclass(frozen=True)
class Trade:
    """One auction order as one solution executes it."""

    uid: str
    sell_token: str
    buy_token: str
    sell_amount: int
    buy_amount: int
    executed_sell: int
    executed_buy: int
    kind: str
    partially_fillable: bool = False


@dataclass(frozen=True)
class FeePolicy:
    kind: str  # "surplus" | "volume" | "priceimprovement"
    factor: float
    max_volume_factor: float = 0.0


@dataclass(frozen=True)
class QuoteTerms:
    sell_amount: int
    buy_amount: int
    fee: int


def _ceil(numerator: int, denominator: int) -> int:
    if denominator == 0:
        raise Refused("division by zero")
    return math.ceil(Fraction(numerator, denominator))


def _floor(numerator: int, denominator: int) -> int:
    if denominator == 0:
        raise Refused("division by zero")
    return math.floor(Fraction(numerator, denominator))


def surplus_against(
    trade: Trade, price_sell: int, price_buy: int, limit_sell: int, limit_buy: int
) -> int:
    """Surplus in the surplus token against arbitrary limits, with arbitrary clearing
    prices: the sell side rounds both terms up, the buy side down."""
    if trade.kind == "sell":
        received = _ceil(trade.executed_sell * price_sell, price_buy)
        required = _ceil(trade.executed_sell * limit_buy, limit_sell)
        value = received - required
    else:
        allowed = _floor(trade.executed_buy * limit_sell, limit_buy)
        paid = _floor(trade.executed_buy * price_buy, price_sell)
        value = allowed - paid
    if value < 0:
        raise Refused("negative surplus")
    return value


def user_surplus(trade: Trade) -> int:
    """At the executed prices: the price of the sell token is the bought amount."""
    return surplus_against(
        trade, trade.executed_buy, trade.executed_sell, trade.sell_amount, trade.buy_amount
    )


def scale_f64(amount: int, factor: float) -> int:
    """`U256::checked_mul_f64`: factor*1e18 rounded to an integer, then integer math."""
    if factor == 1.0:
        return amount
    return amount * round(factor * 1e18) // ONE


def quote_fee(gas: float, gas_price: float, sell_token_price: float) -> int:
    """gas * gas_price / sell_price as an exact rational, floored."""
    if sell_token_price == 0:
        raise Refused("zero sell token price")
    return math.floor(Fraction(gas) * Fraction(gas_price) / Fraction(sell_token_price))


def protocol_fees(
    trade: Trade, policies: Sequence[FeePolicy], quote: QuoteTerms | None
) -> list[int]:
    """Every policy's fee in the surplus token, in application order.

    The protocol charges the *last* policy first, on the executed prices, and each
    earlier one on the prices the trade would have had without everything charged so
    far."""
    executed_sell, executed_buy = trade.executed_sell, trade.executed_buy

    def sold(price_sell: int, price_buy: int) -> int:
        if trade.kind == "sell":
            return executed_sell
        return _floor(executed_buy * price_buy, price_sell)

    def bought(price_sell: int, price_buy: int) -> int:
        if trade.kind == "buy":
            return executed_buy
        return _ceil(executed_sell * price_sell, price_buy)

    def volume(price_sell: int, price_buy: int, factor: float) -> int:
        if trade.kind == "sell":
            if factor >= 1.0:
                raise Refused("volume factor not below 1")
            return scale_f64(bought(price_sell, price_buy), factor / (1.0 - factor))
        return scale_f64(sold(price_sell, price_buy), factor / (1.0 + factor))

    def cut(surplus: int, factor: float) -> int:
        if factor >= 1.0:
            raise Refused("surplus factor not below 1")
        return scale_f64(surplus, factor / (1.0 - factor))

    def one(policy: FeePolicy, price_sell: int, price_buy: int) -> int:
        if policy.kind == "volume":
            return volume(price_sell, price_buy, policy.factor)
        if policy.kind == "surplus":
            surplus = surplus_against(
                trade, price_sell, price_buy, trade.sell_amount, trade.buy_amount
            )
            return min(cut(surplus, policy.factor),
                       volume(price_sell, price_buy, policy.max_volume_factor))
        if quote is None:
            raise Refused("price improvement without a quote")
        if trade.kind == "sell":
            if quote.sell_amount == 0:
                raise Refused("zero quote sell amount")
            net_buy = quote.buy_amount - _floor(quote.fee * quote.buy_amount, quote.sell_amount)
            if net_buy < 0:
                raise Refused("quote fee exceeds quote")
            limit_sell = trade.sell_amount
            limit_buy = max(trade.buy_amount, _floor(net_buy * trade.sell_amount, quote.sell_amount))
        else:
            if quote.buy_amount == 0:
                raise Refused("zero quote buy amount")
            limit_sell = min(
                trade.sell_amount,
                _floor((quote.sell_amount + quote.fee) * trade.buy_amount, quote.buy_amount),
            )
            limit_buy = trade.buy_amount
        try:
            improvement = surplus_against(trade, price_sell, price_buy, limit_sell, limit_buy)
        except Refused as error:
            if "negative" not in str(error):
                raise
            improvement = 0
        return min(cut(improvement, policy.factor),
                   volume(price_sell, price_buy, policy.max_volume_factor))

    base_sell, base_buy = executed_buy, executed_sell
    fees = [0] * len(policies)
    charged = 0
    price_sell, price_buy = base_sell, base_buy
    for index in reversed(range(len(policies))):
        fees[index] = one(policies[index], price_sell, price_buy)
        charged += fees[index]
        if index == 0:
            break
        s, b = sold(base_sell, base_buy), bought(base_sell, base_buy)
        if trade.kind == "sell":
            price_sell, price_buy = b + charged, s
        else:
            if s - charged < 0:
                raise Refused("fees exceed the sold amount")
            price_sell, price_buy = b, s - charged
    return fees


def partner_part(fees: Sequence[int], partners: int) -> int | None:
    """The trailing `partners` fees; a lone policy is always the DAO's. `None` when
    more partners are declared than there are policies to give them."""
    if len(fees) <= 1 or partners <= 0:
        return 0
    if partners >= len(fees):
        return None
    return sum(fees[len(fees) - partners:])


def native(trade: Trade, atoms: int, prices: Mapping[str, int], unit: int = ONE) -> int:
    """Surplus-token atoms to native wei (lamports on Solana, whose prices divide by
    `unit` = 1e9), through the buy token's price; a buy order's sell-token amount goes
    through its own limit ratio first."""
    price = prices.get(trade.buy_token)
    if price is None:
        raise Refused("unpriced buy token")
    if trade.kind == "sell":
        return atoms * price // unit
    if trade.sell_amount == 0:
        raise Refused("zero limit sell")
    return (atoms * trade.buy_amount // trade.sell_amount) * price // unit


@dataclass(frozen=True)
class OrderScore:
    surplus_atoms: int
    fee_atoms: int
    surplus_native: int
    score_native: int
    fee_native: int
    partner_native: int | None
    """`None` when the partner split is undetermined."""


def order_score(
    trade: Trade,
    policies: Sequence[FeePolicy],
    quote: QuoteTerms | None,
    partners: int,
    prices: Mapping[str, int],
    unit: int = ONE,
) -> OrderScore:
    """`compute_order_score`, with its decomposition: score = native(surplus + fees)."""
    surplus = user_surplus(trade)
    fees = protocol_fees(trade, policies, quote)
    total = sum(fees)
    score = native(trade, surplus + total, prices, unit)
    surplus_native = native(trade, surplus, prices, unit)
    partner = partner_part(fees, partners)
    partner_native = (
        None
        if partner is None
        else score - native(trade, surplus + total - partner, prices, unit)
    )
    return OrderScore(surplus, total, surplus_native, score, score - surplus_native,
                      partner_native)


# --- winner selection -----------------------------------------------------------------


@dataclass(frozen=True)
class Bid:
    """One solution as the arbitration sees it."""

    uid: int
    solver: str
    total: int
    values: Mapping[Pair, int]
    """What the fairness filter compares, per raw directed pair."""
    claims: frozenset[Pair]
    """`canonical_token`-normalised pairs, which is what winners must not share."""


@dataclass
class Outcome:
    ranked: list[Bid] = field(default_factory=list[Bid])
    filtered: list[Bid] = field(default_factory=list[Bid])
    winners: set[int] = field(default_factory=set[int])
    baselines: dict[Pair, int] = field(default_factory=dict[Pair, int])


def _pick(bids: Sequence[Bid], max_winners: int) -> list[Bid]:
    taken: list[Bid] = []
    claimed: set[Pair] = set()
    for bid in bids:
        if len(taken) == max_winners:
            break
        if not (bid.claims & claimed):
            taken.append(bid)
            claimed |= bid.claims
    return taken


def arbitrate(bids: Sequence[Bid], max_winners: int) -> Outcome:
    """Steps 2-5. `bids` in tie-break order (the autopilot's shuffle, or `uid` order
    when replaying a record)."""
    outcome = Outcome()
    for bid in bids:
        if len(bid.values) == 1:
            (pair,) = bid.values
            outcome.baselines[pair] = max(outcome.baselines.get(pair, 0), bid.total)
    scored = [b for b in bids if b.values]
    order = sorted(range(len(scored)), key=lambda i: (-scored[i].total, i))
    kept: list[Bid] = []
    for i in order:
        bid = scored[i]
        fair = len(bid.values) == 1 or all(
            value >= outcome.baselines.get(pair, 0) for pair, value in bid.values.items()
        )
        (kept if fair else outcome.filtered).append(bid)
    winners = _pick(kept, max_winners)
    outcome.winners = {b.uid for b in winners}
    outcome.ranked = [b for b in kept if b.uid in outcome.winners] + [
        b for b in kept if b.uid not in outcome.winners
    ]
    return outcome


def references(ranked: Sequence[Bid], winners: set[int], max_winners: int
               ) -> dict[str, tuple[int, frozenset[str]]]:
    """Step 6: per winning solver, the winners' total of a re-pick over `ranked` (in
    its stored order) without any of that solver's solutions, and whose they are."""
    result: dict[str, tuple[int, frozenset[str]]] = {}
    for bid in ranked:
        if bid.uid not in winners or bid.solver in result:
            continue
        rest = [b for b in ranked if b.solver != bid.solver]
        picked = _pick(rest, max_winners)
        result[bid.solver] = (sum(b.total for b in picked), frozenset(b.solver for b in picked))
    return result


def reference_picks(ranked: Sequence[Bid], winners: set[int], max_winners: int
                    ) -> dict[str, frozenset[int]]:
    """Step 6's re-picks themselves: per winning solver, the uids the pick without it
    takes."""
    result: dict[str, frozenset[int]] = {}
    for bid in ranked:
        if bid.uid not in winners or bid.solver in result:
            continue
        rest = [b for b in ranked if b.solver != bid.solver]
        result[bid.solver] = frozenset(b.uid for b in _pick(rest, max_winners))
    return result


# --- rewards --------------------------------------------------------------------------


@dataclass(frozen=True)
class WinRecord:
    solver: str
    score: int
    settled: bool
    upper: Decimal | None
    penalty: Decimal | None


@dataclass(frozen=True)
class RewardRow:
    competition: int
    observed: int
    reference: int
    uncapped: int
    upper: Decimal | None
    lower: Decimal | None
    capped: Decimal | None


def rewards(
    wins: Sequence[WinRecord],
    reference: Mapping[str, int],
    flat_lower: int | None,
    excluded: bool,
    *,
    capped: bool | None = None,
) -> dict[str, RewardRow]:
    """`fct_solver_rewards_per_auction`, from docs/rewards.md. Caps are computed when
    `capped` (default: when there is a flat floor). Solana's mart has no flat floor: a
    win without a penalty cap leaves the solver's floor, and its capped reward, NULL."""
    if capped is None:
        capped = flat_lower is not None
    winning = sum(w.score for w in wins)
    rows: dict[str, RewardRow] = {}
    for solver in dict.fromkeys(w.solver for w in wins):
        mine = [w for w in wins if w.solver == solver]
        competition = sum(w.score for w in mine)
        observed = sum(w.score for w in mine if w.settled)
        uncapped = winning - competition + observed - min(winning, reference[solver])
        upper: Decimal | None = None
        lower: Decimal | None = None
        clamped: Decimal | None = None
        if capped:
            upper = (
                None
                if any(w.upper is None for w in mine)
                else sum((w.upper for w in mine if w.upper is not None), Decimal(0))
            )
            if flat_lower == 0:
                lower = Decimal(0)
            elif any(w.penalty is None for w in mine):
                lower = None if flat_lower is None else Decimal(flat_lower)
            else:
                lower = -sum((w.penalty for w in mine if w.penalty is not None and not w.settled),
                             Decimal(0))
            if upper is not None and lower is not None:
                clamped = Decimal(0) if excluded else min(max(Decimal(uncapped), lower), upper)
        rows[solver] = RewardRow(competition, observed, reference[solver], uncapped,
                                 upper, lower, clamped)
    return rows


# --- the quote competition ------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    estimator: str
    out_amount: int
    gas: int
    verified: bool


def answer_usable(answer: Answer) -> bool:
    return answer.out_amount > 0 and answer.gas > 0


def gas_in_sell(gas: float, gas_price: float, sell_token_price: float) -> float:
    return gas * gas_price / sell_token_price


def quote_key(answer: Answer, is_sell: bool, in_amount: float, gas_price: float,
              sell_token_price: float) -> float:
    """The protocol's effective amount, truncated, larger is better; an underwater
    sell answer is worth zero."""
    cost = gas_in_sell(answer.gas, gas_price, sell_token_price)
    if is_sell:
        value = answer.out_amount - cost * (answer.out_amount / in_amount)
        return float(math.trunc(value)) if value > 0 else 0.0
    return -float(math.trunc(answer.out_amount + cost))


def quote_winner(answers: Sequence[Answer], is_sell: bool, in_amount: float,
                 gas_price: float, sell_token_price: float) -> Answer | None:
    """Verified beats unverified at any amount; then the key; ties go to the first."""
    usable = [a for a in answers if answer_usable(a)]
    if any(a.verified for a in usable):
        usable = [a for a in usable if a.verified]
    best: Answer | None = None
    best_key = 0.0
    for answer in usable:
        key = quote_key(answer, is_sell, in_amount, gas_price, sell_token_price)
        if best is None or key > best_key:
            best, best_key = answer, key
    return best


def stream_forwards(answers: Sequence[Answer], is_sell: bool, in_amount: float,
                    gas_price: float, sell_token_price: float) -> list[Answer]:
    """What a stream sends: each usable answer beating the best so far on (verified,
    key), less those whose gas does not fit a sell amount — which still raise the bar."""
    sent: list[Answer] = []
    best: tuple[bool, float] | None = None
    for answer in answers:
        if not answer_usable(answer):
            continue
        key = (answer.verified, quote_key(answer, is_sell, in_amount, gas_price,
                                          sell_token_price))
        if best is None or key > best:
            best = key
            fits = not is_sell or gas_in_sell(answer.gas, gas_price, sell_token_price) < in_amount
            if fits:
                sent.append(answer)
    return sent


# --- display --------------------------------------------------------------------------


def eth_text(wei: int | Decimal, places: int, decimals: int = 18) -> str:
    """Native wei as ETH (lamports as SOL: `decimals` 9), rounded half away from zero,
    by `Decimal` quantize."""
    value = Decimal(int(wei)) / Decimal(10**decimals)
    quantum = Decimal(1).scaleb(-places)

    return str(value.quantize(quantum, rounding=ROUND_HALF_UP))


def signed_eth_text(wei: int | Decimal, places: int = 4, decimals: int = 18) -> str:
    return ("+" if int(wei) > 0 else "") + eth_text(wei, places, decimals)

