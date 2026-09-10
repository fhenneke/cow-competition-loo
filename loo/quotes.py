"""The quote competition: who priced an order into existence, and what changes without
them.

The solve competition decides who *fills* an order; the quote competition decides the
price the user was shown, and therefore the order's limit price and the baseline every
`priceimprovement` fee is charged against. Removing a solver moves both. This module is
the quote half: it reproduces the pick, re-runs it without a solver, and hands the
resulting counterfactual quote to the fee path that already exists (`fees.py`).

## The pick

`services/crates/price-estimation/src/competition/quote.rs`:

```
is_reasonable          (:191)  drop any estimate with gas == 0 or out_amount == 0
compare_quote_result   (:203)  a verified estimate beats an unverified one at any amount
compare_quote          (:226)  otherwise rank on effective_eth_out
effective_eth_out      (:276)  out_amount × native_price(out_token) ∓ gas × gas_price
                               sell: minus fees, maximise;  buy: plus fees, minimise
```

Two facts make that reproducible from recorded data, and both are measured
(docs/quotes.md):

1. The **verified class is a hard first key.** Over 5,041 requests carrying both
   verified and unverified estimates, a verified one won all 5,041.
2. Neither `native_price` nor `gas_price` is in ClickHouse, but they enter only through
   a ratio that is *common to every estimator of one request*. Netting the gas cost in
   the **sell** token instead — at `order_quotes.gas_price / sell_token_price`, both
   recorded per order — reproduces the recorded winner on **99.6%** of mainnet and
   **100.0%** of bnb order-creating requests. Ranking on the gross `out_amount` alone
   reproduces 61.5% / 90.2%.

So there are two ranking rules, and `RANKING` names them:

- `effective-sell` (default) — the effective quote netted in the sell token. Needs
  nothing that is not recorded on the order, and is the same quantity the
  price-improvement fee is charged against, so pick and fee cannot disagree.
- `autopilot` — the protocol's own out-token rule, which needs the *buy* token's native
  price for sell orders. Kept as the cross-check: it measures how far the two differ,
  which is exactly the quote's own pricing error, since `in × sell_price ≈ out ×
  buy_price` is what a quote asserts. For a **buy** order the out token *is* the sell
  token, so the two rules are identical there by construction.

## What is exact and what is not

Amounts in `gold_quotes` are `Float64`, so nothing derived from the ladder is
integer-exact — see `ch.as_float`. The recorded quote, its fee and every solve-side
number stay exact, because they come from Postgres: the ladder supplies only the
*counterfactual*, never the baseline (Q-D5). A match failure therefore degrades the
counterfactual and can be counted; it can never corrupt a recorded number.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import TYPE_CHECKING, Literal

from . import ch
from .db import Connection, as_int, chunked, fetch
from .fees import FeeError, Quote, quote_fee
from .primitives import ONE_ETH

if TYPE_CHECKING:  # only `shifted_limit` needs the type; `valuation` imports `fees` too
    from .valuation import Order

QuoteRanking = Literal["effective-sell", "autopilot"]

RANKING: dict[QuoteRanking, str] = {
    "effective-sell": (
        "the effective quote netted in the sell token, at the order's recorded "
        "gas_price / sell_token_price — the same quantity the price-improvement fee is "
        "charged against, and reproducible from the record alone"
    ),
    "autopilot": (
        "the protocol's own rule (competition/quote.rs:276): out_amount valued in the "
        "out token's native price less gas x gas_price, with the buy token's native "
        "price taken from the window's auction prices for sell orders"
    ),
}

PRICE_IMBALANCE_THRESHOLD = 2.0
"""D14 on the quote side. Two independent feeds price the order's sell token: the api's,
recorded per order as `order_quotes.sell_token_price` (wei per atom), and the autopilot's,
in `auction_prices` (the same quantity scaled by 1e18). They agree to four decimals on
real tokens — measured 1.0000 for WETH, 0.9999 for USDC, 1.0004 for USDT — and disagree by
factors of 1e7 to 1e18 where one of them is broken. An order past this threshold is
excluded from every quote figure and counted, because the same wrong price sets both its
weight and its gas cost: one such order carried 6.9 M ETH of apparent executed volume,
90% of a corrected window."""

BUY_AMOUNT_TOLERANCE = 0.005
"""A buy order's quoted amount carries a slippage buffer, so the request it came from
matches within half a percent rather than exactly. Sell orders match exactly."""

PRE_CREATION_WINDOW_SECONDS = 30 * 60
POST_CREATION_WINDOW_SECONDS = 90
"""A quote request can precede its order by up to half an hour, and a placement-time
re-quote lands just after creation."""


# --- the ladder ---------------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    """One estimator's answer to one request, as `fct_quote_estimates` recorded it."""

    estimator: str
    out_amount: float
    gas: float
    verified: bool
    is_winner: bool = False

    @property
    def reasonable(self) -> bool:
        """`is_reasonable` (`competition/quote.rs:191`) — a zero on either field means
        the estimate never entered the comparison at all."""
        return self.out_amount > 0 and self.gas > 0


@dataclass(frozen=True)
class Pricing:
    """The scalars one request's ranking needs.

    `gas_price` and `sell_token_price` are recorded on the order's own quote, so they
    are exact for every order-creating request. `buy_token_price` is only consulted by
    the `autopilot` rule on sell orders and is `None` when the window's auctions never
    priced that token — in which case the request is reported unpriced, not guessed.
    """

    gas_price: float
    sell_token_price: float
    buy_token_price: float | None = None

    def gas_cost_in_sell_atoms(self, gas: float) -> float:
        return gas * self.gas_price / self.sell_token_price


@dataclass(frozen=True)
class QuoteRequest:
    """One request and the ladder it produced."""

    request_id: str
    timestamp: str
    kind: str
    sell_token: str
    buy_token: str
    in_amount: float
    winner: str
    """The estimator `winning_price_estimates` recorded. Empty when the request has no
    recorded winner, which `pick` then has nothing to be checked against."""
    price_quality: str = "unknown"
    """`optimal` or `verified` as the request row records it — never `fast`, which the
    candidate query excludes because `order_quotes` never holds a fast quote. `unknown`
    for a competition recovered from the raw layer, whose request event — the only
    carrier of the field — is what is missing; those ladders run 7–10 estimators deep,
    which a fast (early-return) ladder never does."""
    source: Literal["gold", "raw"] = "gold"
    """`gold` for `gold_quotes.fct_quote_requests`; `raw` for a competition the gold
    tables do not hold, reconstructed from `raw_quotes.price_estimates` grouped by
    request id (see `load_raw_candidates`)."""
    estimates: tuple[Estimate, ...] = ()

    @property
    def is_sell(self) -> bool:
        return self.kind == "sell"

    @property
    def reasonable(self) -> tuple[Estimate, ...]:
        return tuple(e for e in self.estimates if e.reasonable)

    @property
    def recorded_winner(self) -> Estimate | None:
        winners = [e for e in self.reasonable if e.is_winner]
        return winners[0] if len(winners) == 1 else None


def effective_sell_amount(
    estimate: Estimate, *, is_sell: bool, in_amount: float, pricing: Pricing
) -> float:
    """The quote's effective amount, netted for the estimator's own gas in the sell
    token — the `order_quotes_effective` netting without the volume factor, which is
    also what `fees._adjust_quote_to_order_limits` charges against.

    Returned on the *out* side: buy atoms for a sell order, sell atoms for a buy one.
    """
    cost = pricing.gas_cost_in_sell_atoms(estimate.gas)
    if is_sell:
        if in_amount <= 0:
            raise ValueError("a sell request with no in_amount cannot be netted")
        return (in_amount - cost) / in_amount * estimate.out_amount
    return estimate.out_amount + cost


def rank_key(
    estimate: Estimate,
    *,
    is_sell: bool,
    in_amount: float,
    pricing: Pricing,
    ranking: QuoteRanking,
) -> float:
    """The comparison key, oriented so that **larger is better** under both order kinds
    and both rules — a buy order's cost is negated rather than compared backwards, so
    every caller can use plain `max`."""
    if ranking == "effective-sell":
        value = effective_sell_amount(
            estimate, is_sell=is_sell, in_amount=in_amount, pricing=pricing
        )
        return value if is_sell else -value
    if ranking == "autopilot":
        # out_amount x native(out) ∓ gas x gas_price, divided by native(out): the
        # division is common to the request, so it does not move the ordering.
        price = pricing.sell_token_price if not is_sell else pricing.buy_token_price
        if not price:
            raise UnpricedRequestError(
                "the autopilot ranking needs the out token's native price"
            )
        cost = estimate.gas * pricing.gas_price / price
        return estimate.out_amount - cost if is_sell else -(estimate.out_amount + cost)
    raise ValueError(f"unknown quote ranking {ranking!r}")


class UnpricedRequestError(RuntimeError):
    """A request the chosen ranking cannot evaluate for want of a native price."""


def pick(
    estimates: Iterable[Estimate],
    *,
    is_sell: bool,
    in_amount: float,
    pricing: Pricing,
    ranking: QuoteRanking = "effective-sell",
) -> Estimate | None:
    """The winning estimate under the protocol's rule, or `None` if nothing is usable.

    Verified beats unverified at any amount (`QuoteVerificationMode::Prefer`, which the
    data confirms is what prod runs), then the ranking key decides. Ties keep the first
    estimate in the given order, which is the order `fct_quote_estimates` returned —
    arbitrary, and the reason `Ranking.tied` exists to count them.
    """
    usable = [e for e in estimates if e.reasonable]
    if not usable:
        return None
    if any(e.verified for e in usable):
        usable = [e for e in usable if e.verified]
    return max(
        usable,
        key=lambda e: rank_key(
            e, is_sell=is_sell, in_amount=in_amount, pricing=pricing, ranking=ranking
        ),
    )


def leave_one_out(
    request: QuoteRequest,
    removed: frozenset[str],
    *,
    pricing: Pricing,
    ranking: QuoteRanking = "effective-sell",
) -> Estimate | None:
    """Who wins the request with every estimator in `removed` absent.

    `None` means no quote at all: the request produced nothing, so the order it created
    would never have existed.
    """
    return pick(
        (e for e in request.estimates if e.estimator not in removed),
        is_sell=request.is_sell,
        in_amount=request.in_amount,
        pricing=pricing,
        ranking=ranking,
    )


def counterfactual_quote(
    estimate: Estimate, *, is_sell: bool, in_amount: float, pricing: Pricing
) -> Quote:
    """The `fees.Quote` an estimate would have put on the order.

    The request's `in_amount` is the sell side of a sell request and the buy side of a
    buy one, so the two amounts swap roles with the kind. `fee` is the estimator's own
    gas cost in sell atoms — the same `quote_fee` the recorded quote carries, computed
    from the estimator's `gas` instead of the winner's.

    Amounts are rounded rather than floored: they come from a `Float64` ladder, where a
    floor would bias every counterfactual quote down by up to one atom and so bias
    every fee delta up.
    """
    fee = round(pricing.gas_cost_in_sell_atoms(estimate.gas))
    if fee < 0:
        raise FeeError(f"negative counterfactual quote fee ({fee})")
    if is_sell:
        return Quote(
            sell_amount=round(in_amount), buy_amount=round(estimate.out_amount), fee=fee
        )
    return Quote(
        sell_amount=round(estimate.out_amount), buy_amount=round(in_amount), fee=fee
    )


# --- the recorded orders and their quotes -------------------------------------------

ORDERS_SQL = """
with traded as (
    select t.order_uid, sum(t.sell_amount) as executed_sell
    from dbt.stg_backend_data__trades t
    join dbt.stg_backend_data__orders o on o.uid = t.order_uid
    where o.creation_timestamp >= %(start)s and o.creation_timestamp < %(end)s
    group by t.order_uid
)
select
    encode(o.uid, 'hex')                as order_uid,
    o.creation_timestamp,
    o.kind::text                        as kind,
    o.partially_fillable,
    encode(o.sell_token, 'hex')         as sell_token,
    encode(o.buy_token, 'hex')          as buy_token,
    o.sell_amount                       as order_sell_amount,
    o.buy_amount                        as order_buy_amount,
    encode(oq.solver, 'hex')            as quote_solver,
    oq.sell_amount                      as quote_sell_amount,
    oq.buy_amount                       as quote_buy_amount,
    oq.gas_amount,
    oq.gas_price,
    oq.sell_token_price,
    oq.verified                         as quote_verified,
    coalesce(ad.app_code, '(none)')     as app_code,
    coalesce(t.executed_sell, 0)        as executed_sell
from dbt.stg_backend_data__orders o
join dbt.stg_backend_data__order_quotes oq on oq.order_uid = o.uid
left join dbt.stg_backend_data__app_data ad on ad.contract_app_data = o.app_data
left join traded t on t.order_uid = o.uid
where o.creation_timestamp >= %(start)s and o.creation_timestamp < %(end)s
order by o.creation_timestamp
"""

# The autopilot's native price per token: the median over the window's auctions. Crude by
# design — it is a cross-check and the `autopilot` ranking's input, never a valuation —
# and a token the window never priced stays absent rather than being guessed.
TOKEN_PRICES_SQL = """
select encode(ap.token, 'hex') as token, percentile_cont(0.5) within group (
           order by ap.price
       ) as price
from dbt.stg_backend_data__auction_prices ap
where ap.auction_id = any(%(ids)s)
  and ap.token = any(%(tokens)s)
group by ap.token
"""


@dataclass(frozen=True)
class RecordedQuote:
    """One order created in the window, with the quote that priced it.

    Every field here comes from Postgres and is exact. `in_amount` is the amount the
    quote request carried — the sell side of a sell order, the buy side of a buy one —
    and is the key the ClickHouse request is matched on.
    """

    order_uid: str
    created: str
    kind: str
    partially_fillable: bool
    sell_token: str
    buy_token: str
    order_sell_amount: int
    order_buy_amount: int
    quote_solver: str
    quote: Quote
    pricing: Pricing
    verified: bool
    app_code: str
    """The integration that placed the order. Match coverage varies by an order of
    magnitude between app codes, so it is the only honest unit to report coverage in."""
    executed_sell: int = 0
    """Sell-side atoms this order actually traded, summed over its trades — 0 for an
    order that never traded."""

    @property
    def is_sell(self) -> bool:
        return self.kind == "sell"

    @property
    def in_amount(self) -> float:
        return float(self.quote.sell_amount if self.is_sell else self.quote.buy_amount)

    @property
    def out_amount(self) -> float:
        """The amount the winning estimate must have returned, if this order's quote
        came from the request we matched it to — the corroboration key (Tier A)."""
        return float(self.quote.buy_amount if self.is_sell else self.quote.sell_amount)

    @property
    def volume_native(self) -> int:
        """The order's **executed** sell-side value in native wei, at the quote's own
        sell-token price — the weight of every quality figure and the window denominator.

        Executed rather than quoted, and that is not a detail. Quoted volume is not a
        market: five probe orders of 1e25 atoms — ten million WETH — were 52.4 M of one
        ten-day window's 52.5 M ETH of quoted volume, and none of them traded. Weighting
        by what traded drops them by construction, and matches the denominator D23 uses
        on the solve side. An order that never traded weighs nothing: its quote moved a
        limit price nobody used.
        """
        return round(self.executed_sell * self.pricing.sell_token_price)

    @property
    def quoted_volume_native(self) -> int:
        """The quoted sell-side value, kept only to report how much of the window never
        traded."""
        return round(self.quote.sell_amount * self.pricing.sell_token_price)

    @property
    def traded(self) -> bool:
        return self.executed_sell > 0


def load_recorded_quotes(
    conn: Connection, start: str, end: str
) -> tuple[list[RecordedQuote], int]:
    """Orders created in `[start, end)` that carry a recorded quote.

    Returns the orders and the number of rows dropped because their quote could not be
    evaluated — a zero `sell_token_price` makes the gas cost undefined, and an order
    whose own quote cannot be netted cannot have a counterfactual either.
    """
    orders: list[RecordedQuote] = []
    dropped = 0
    for row in fetch(conn, ORDERS_SQL, {"start": start, "end": end}):
        try:
            fee = quote_fee(row["gas_amount"], row["gas_price"], row["sell_token_price"])
        except FeeError:
            dropped += 1
            continue
        if not row["sell_token_price"]:
            dropped += 1
            continue
        orders.append(
            RecordedQuote(
                app_code=row["app_code"],
                executed_sell=as_int(row["executed_sell"]),
                order_uid=row["order_uid"],
                created=row["creation_timestamp"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                kind=row["kind"],
                partially_fillable=bool(row["partially_fillable"]),
                sell_token=row["sell_token"],
                buy_token=row["buy_token"],
                order_sell_amount=as_int(row["order_sell_amount"]),
                order_buy_amount=as_int(row["order_buy_amount"]),
                quote_solver=row["quote_solver"],
                quote=Quote(
                    sell_amount=as_int(row["quote_sell_amount"]),
                    buy_amount=as_int(row["quote_buy_amount"]),
                    fee=fee,
                ),
                pricing=Pricing(
                    gas_price=float(row["gas_price"]),
                    sell_token_price=float(row["sell_token_price"]),
                ),
                verified=bool(row["quote_verified"]),
            )
        )
    return orders, dropped


# --- ClickHouse: candidate requests and their ladders -------------------------------

REQUEST_COLUMNS = (
    "request_id, toString(event_timestamp) as event_timestamp, kind, "
    "lower(sell_token) as sell_token, lower(buy_token) as buy_token, "
    "in_amount, winner, price_quality"
)


def _time_bounds(start: str, end: str) -> tuple[str, str]:
    """The ClickHouse window: wide enough for a request that precedes its order by the
    matching window, and for a re-quote just after the last order of the window."""
    from datetime import datetime, timedelta

    first = datetime.fromisoformat(start) - timedelta(
        seconds=PRE_CREATION_WINDOW_SECONDS
    )
    last = datetime.fromisoformat(end) + timedelta(seconds=POST_CREATION_WINDOW_SECONDS)
    return first.strftime("%Y-%m-%d %H:%M:%S"), last.strftime("%Y-%m-%d %H:%M:%S")


def load_candidate_requests(
    client: ch.Client,
    network: str,
    orders: Sequence[RecordedQuote],
    *,
    chunk: int = 400,
    log: Callable[[str], None] | None = None,
) -> list[QuoteRequest]:
    """Requests that could have created one of `orders`, by tokens, kind and amount.

    Issued **per creation day**, because the amount filter alone is not selective: a
    popular round amount matches every request that ever asked for it, so a ten-day
    window asked in one go came back with 1.9 M rows for 38 k orders. Splitting by the
    day the order was created bounds each query to that day's partitions and to the
    requests that could plausibly precede it. The window is therefore implied by the
    orders themselves and needs no separate bounds.

    Sell requests match the amount exactly; buy requests carry a slippage buffer and
    match within `BUY_AMOUNT_TOLERANCE`.
    """
    emit: Callable[[str], None] = log or (lambda message: None)
    by_day: dict[str, list[RecordedQuote]] = {}
    for order in orders:
        by_day.setdefault(order.created[:10], []).append(order)
    found: dict[str, QuoteRequest] = {}
    for day in sorted(by_day):
        from datetime import date, timedelta

        after = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        for request in _candidates_for_day(
            client, network, by_day[day], day, after, chunk=chunk
        ):
            found[request.request_id] = request
    emit(
        f"{len(found)} candidate quote requests for {len(orders)} orders "
        f"over {len(by_day)} creation days"
    )
    return list(found.values())


def _candidates_for_day(
    client: ch.Client,
    network: str,
    orders: Sequence[RecordedQuote],
    start: str,
    end: str,
    *,
    chunk: int,
) -> list[QuoteRequest]:
    first, last = _time_bounds(start, end)
    # `fast` requests are excluded because `order_quotes` never holds a fast quote: the
    # quote an order was placed from is always a non-fast competition, and a fast one
    # that happens to share the amount is the UI's poll, not the order's quote — on
    # 2026-07-25 the 765 Trust Wallet orders matched fast requests with a 0.45% gap to
    # the stored amount, and their real competitions were in the raw layer, exact to the
    # atom. `quote_id is not null` is *not* a filter here: the competitions the gold
    # layer never received have no `quote_computed` either, yet their quotes are on the
    # orders.
    base = (
        f"chain_id = {ch.chain_id(network)} and environment = 'prod' "
        f"and price_quality != 'fast' "
        f"and event_timestamp >= '{first}' and event_timestamp < '{last}'"
    )
    found: dict[str, QuoteRequest] = {}

    sells = sorted(
        {
            (o.sell_token, o.buy_token, o.in_amount)
            for o in orders
            if o.is_sell and o.in_amount > 0
        }
    )
    for part in chunked(sells, chunk):
        tuples = ", ".join(
            f"('0x{sell}', '0x{buy}', {amount!r})" for sell, buy, amount in part
        )
        sql = (
            f"select {REQUEST_COLUMNS} from {ch.DATABASE}.fct_quote_requests "
            f"where {base} and kind = 'sell' "
            f"and (lower(sell_token), lower(buy_token), in_amount) in ({tuples})"
        )
        for row in client.fetch(sql):
            found[row["request_id"]] = _request(row)

    buys = sorted(
        {
            (o.sell_token, o.buy_token, o.in_amount)
            for o in orders
            if not o.is_sell and o.in_amount > 0
        }
    )
    for part in chunked(buys, chunk // 2):
        conditions = " or ".join(
            f"(lower(sell_token) = '0x{sell}' and lower(buy_token) = '0x{buy}' "
            f"and in_amount between {amount * (1 - BUY_AMOUNT_TOLERANCE)!r} "
            f"and {amount * (1 + BUY_AMOUNT_TOLERANCE)!r})"
            for sell, buy, amount in part
        )
        sql = (
            f"select {REQUEST_COLUMNS} from {ch.DATABASE}.fct_quote_requests "
            f"where {base} and kind = 'buy' and ({conditions})"
        )
        for row in client.fetch(sql):
            found[row["request_id"]] = _request(row)

    return list(found.values())


def _request(row: ch.Row) -> QuoteRequest:
    return QuoteRequest(
        request_id=row["request_id"],
        timestamp=str(row["event_timestamp"]),
        kind=row["kind"],
        sell_token=row["sell_token"].removeprefix("0x"),
        buy_token=row["buy_token"].removeprefix("0x"),
        in_amount=ch.as_float(row["in_amount"]),
        winner=row["winner"] or "",
        price_quality=row["price_quality"] or "unknown",
        source="gold",
    )


RAW_LADDER_COLUMNS = (
    "request_id, toString(min(event_timestamp)) as ts, lower(any(sell_token)) as sell_, "
    "lower(any(buy_token)) as buy_, any(kind) as kind_, any(in_amount) as in_amount_"
)


def load_raw_candidates(
    client: ch.Client,
    network: str,
    orders: Sequence[RecordedQuote],
    *,
    chunk: int = 300,
    log: Callable[[str], None] | None = None,
) -> list[QuoteRequest]:
    """Competitions the gold tables do not hold, reconstructed from the raw estimates.

    `gold_quotes` is built from the `quote_requested` event, and a competition whose
    request event never arrived is in neither gold table — on 2026-07-25 mainnet 8,625
    of 1,537,915 competitions, 0.6% of requests but the competitions behind **a quarter
    of the day's orders**: API callers whose ladders (7–10 estimators, a recorded winner,
    no `quote_computed`) are complete in `raw_quotes.price_estimates`. The fields the
    request row would carry — tokens, kind, `in_amount` — are on every estimate row, so
    the competition is recoverable by grouping on `request_id`.

    Called only for the orders the gold layer could not place, per creation day: the
    raw tables are sorted `(chain_id, estimator, sell_token, buy_token, nats_seq)` and
    partitioned by month, so every lookup here is a filtered scan of a month partition,
    kept small by the number of keys it is asked for. Amounts are exact decimal strings
    in the raw layer and are compared as such; a buy order's slippage buffer is matched
    on the parsed value within `BUY_AMOUNT_TOLERANCE`, as in the gold query.
    """
    from datetime import date, timedelta

    emit: Callable[[str], None] = log or (lambda message: None)
    by_day: dict[str, list[RecordedQuote]] = {}
    for order in orders:
        by_day.setdefault(order.created[:10], []).append(order)
    found: dict[str, QuoteRequest] = {}
    for day in sorted(by_day):
        after = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        first, last = _time_bounds(day, after)
        # `request_id` is nullable in the raw layer (49 estimates on 2026-07-25 had none);
        # a null id is no competition, so it is filtered out here rather than grouped.
        base = (
            f"chain_id = {ch.chain_id(network)} and success and request_id is not null "
            f"and event_timestamp >= '{first}' and event_timestamp < '{last}'"
        )
        sells = sorted(
            {
                (o.sell_token, o.buy_token, str(o.quote.sell_amount))
                for o in by_day[day]
                if o.is_sell and o.quote.sell_amount > 0
            }
        )
        for part in chunked(sells, chunk):
            tuples = ", ".join(
                f"('0x{sell}', '0x{buy}', '{amount}')" for sell, buy, amount in part
            )
            sql = (
                f"select {RAW_LADDER_COLUMNS} from {ch.RAW_DATABASE}.price_estimates "
                f"where {base} and kind = 'sell' "
                f"and (lower(sell_token), lower(buy_token), in_amount) in ({tuples}) "
                f"group by request_id"
            )
            for row in client.fetch(sql):
                if row["request_id"]:
                    found[row["request_id"]] = _raw_request(row)
        buys = sorted(
            {
                (o.sell_token, o.buy_token, o.quote.buy_amount)
                for o in by_day[day]
                if not o.is_sell and o.quote.buy_amount > 0
            }
        )
        for part in chunked(buys, chunk // 2):
            conditions = " or ".join(
                f"(lower(sell_token) = '0x{sell}' and lower(buy_token) = '0x{buy}' "
                f"and toFloat64OrZero(in_amount) between "
                f"{amount * (1 - BUY_AMOUNT_TOLERANCE)!r} and "
                f"{amount * (1 + BUY_AMOUNT_TOLERANCE)!r})"
                for sell, buy, amount in part
            )
            sql = (
                f"select {RAW_LADDER_COLUMNS} from {ch.RAW_DATABASE}.price_estimates "
                f"where {base} and kind = 'buy' and ({conditions}) group by request_id"
            )
            for row in client.fetch(sql):
                if row["request_id"]:
                    found[row["request_id"]] = _raw_request(row)
    emit(
        f"{len(found)} competitions recovered from the raw layer for {len(orders)} orders "
        f"the gold tables could not place"
    )
    return list(found.values())


def _raw_request(row: ch.Row) -> QuoteRequest:
    return QuoteRequest(
        request_id=row["request_id"],
        timestamp=str(row["ts"]),
        kind=row["kind_"],
        sell_token=row["sell_"].removeprefix("0x"),
        buy_token=row["buy_"].removeprefix("0x"),
        in_amount=ch.as_float(row["in_amount_"]),
        winner="",  # filled from `winning_price_estimates` once the ladder is loaded
        price_quality="unknown",
        source="raw",
    )


def load_ladders(
    client: ch.Client,
    network: str,
    requests: Sequence[QuoteRequest],
    *,
    chunk: int = 2000,
) -> dict[str, tuple[Estimate, ...]]:
    """Every estimator's answer, per request.

    `request_id in (…)` is not a key prefix, so each query is bounded to the **day of
    the requests it asks for** — an estimate lands seconds after its request, so the
    same generous bounds the candidate query uses hold here — and the cost is one scan
    of that day's partitions per chunk rather than of the whole window's.

    The tables are `ReplacingMergeTree`s, which hold a row twice until a background
    merge collapses it, so a ladder is deduplicated on `(request_id, estimator)`: a
    duplicated winner row would otherwise make `recorded_winner` read as ambiguous and
    silently demote a corroborated match to heuristic.
    """
    from datetime import date, timedelta

    by_day: dict[tuple[str, str], list[str]] = {}
    for request in requests:
        by_day.setdefault((request.source, request.timestamp[:10]), []).append(
            request.request_id
        )
    rows: dict[tuple[str, str], Estimate] = {}
    raw_winners: set[tuple[str, str]] = set()
    for (source, day) in sorted(by_day):
        after = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        first, last = _time_bounds(day, after)
        window = f"event_timestamp >= '{first}' and event_timestamp < '{last}'"
        for part in chunked(sorted(set(by_day[(source, day)])), chunk):
            ids = ch.quote_literals(part)
            if source == "gold":
                sql = (
                    f"select request_id, estimator, out_amount, gas, estimate_verified, "
                    f"is_winner from {ch.DATABASE}.fct_quote_estimates "
                    f"where chain_id = {ch.chain_id(network)} and environment = 'prod' "
                    f"and {window} and request_id in ({ids})"
                )
                for row in client.fetch(sql):
                    rows[(row["request_id"], row["estimator"])] = Estimate(
                        estimator=row["estimator"],
                        out_amount=ch.as_float(row["out_amount"]),
                        gas=ch.as_float(row["gas"]),
                        verified=bool(row["estimate_verified"]),
                        is_winner=bool(row["is_winner"]),
                    )
                continue
            # The raw layer keeps the winner in its own event stream, so two queries:
            # the answers, then who won. Both bounded the same way.
            for row in client.fetch(
                f"select request_id, estimator from "
                f"{ch.RAW_DATABASE}.winning_price_estimates "
                f"where chain_id = {ch.chain_id(network)} and {window} "
                f"and request_id in ({ids})"
            ):
                raw_winners.add((row["request_id"], row["estimator"]))
            sql = (
                f"select request_id, estimator, out_amount, gas, verified "
                f"from {ch.RAW_DATABASE}.price_estimates "
                f"where chain_id = {ch.chain_id(network)} and success "
                f"and request_id is not null and {window} and request_id in ({ids})"
            )
            for row in client.fetch(sql):
                key = (row["request_id"], row["estimator"])
                rows[key] = Estimate(
                    estimator=row["estimator"],
                    out_amount=ch.as_float(row["out_amount"]),
                    gas=ch.as_float(row["gas"]),
                    verified=bool(row["verified"]),
                    is_winner=key in raw_winners,
                )
    ladders: dict[str, list[Estimate]] = {}
    for (request_id, _), estimate in rows.items():
        ladders.setdefault(request_id, []).append(estimate)
    return {rid: tuple(items) for rid, items in ladders.items()}


def with_ladder(request: QuoteRequest, estimates: tuple[Estimate, ...]) -> QuoteRequest:
    """Attach a ladder, and for a request whose row carried no winner — every raw one —
    take the winner from the ladder's own `is_winner` mark, so `diff_quote`'s first
    test (did a removed estimator win?) has the same footing on both sources."""
    winner = request.winner
    if not winner:
        marked = [e for e in estimates if e.is_winner]
        if len(marked) == 1:
            winner = marked[0].estimator
    return replace(request, estimates=estimates, winner=winner)


# --- matching -----------------------------------------------------------------------

MatchTier = Literal["corroborated", "heuristic"]


@dataclass(frozen=True)
class MatchedQuote:
    """One order joined to the request that priced it."""

    order: RecordedQuote
    request: QuoteRequest
    tier: MatchTier
    """`corroborated` when the winning estimate's `out_amount` reproduces the amount
    stored on the order to the last float bit — an identity the matcher never used, true
    of 78% of matches. `heuristic` is tokens/kind/amount/time only, which is mostly an
    order created from a later re-quote."""


def _seconds_between(later: str, earlier: str) -> float:
    from datetime import datetime

    return (
        datetime.fromisoformat(later) - datetime.fromisoformat(earlier)
    ).total_seconds()


def viable_requests(
    orders: Sequence[RecordedQuote], requests: Sequence[QuoteRequest]
) -> dict[str, list[QuoteRequest]]:
    """`order_uid -> the requests that could have created it`, on tokens, kind, amount
    and time alone.

    Deliberately ladder-free, and that is the point: a day of mainnet answers the amount
    filter with tens of thousands of requests — 800 k over a ten-day window, because a
    popular round amount matches every request that ever asked for it — while the *time*
    filter cuts that to a couple per order. Loading ladders before the time filter means
    a `request_id in (…)` scan over two orders of magnitude more ids than any order can
    use, against a table where that predicate is not a key prefix.

    Each token-pair bucket is sorted by amount and searched with `bisect`, because a
    linear scan per order is 38 k orders against buckets tens of thousands deep — a
    minutes-long Python loop for a lookup the amount key already determines.
    """
    from bisect import bisect_left, bisect_right

    buckets: dict[tuple[str, str, str], list[tuple[float, QuoteRequest]]] = {}
    for request in requests:
        buckets.setdefault(
            (request.sell_token, request.buy_token, request.kind), []
        ).append((request.in_amount, request))
    indexed = {
        key: (
            [amount for amount, _ in sorted(items, key=lambda item: item[0])],
            [request for _, request in sorted(items, key=lambda item: item[0])],
        )
        for key, items in buckets.items()
    }

    viable: dict[str, list[QuoteRequest]] = {}
    for order in orders:
        found = indexed.get((order.sell_token, order.buy_token, order.kind))
        if found is None:
            continue
        amounts, bucket = found
        tolerance = 1e-9 if order.is_sell else BUY_AMOUNT_TOLERANCE
        span = tolerance * order.in_amount
        lo = bisect_left(amounts, order.in_amount - span)
        hi = bisect_right(amounts, order.in_amount + span)
        keep = [
            request
            for request in bucket[lo:hi]
            if -POST_CREATION_WINDOW_SECONDS
            <= _seconds_between(order.created, request.timestamp)
            <= PRE_CREATION_WINDOW_SECONDS
        ]
        if keep:
            viable[order.order_uid] = keep
    return viable


def match_orders(
    orders: Sequence[RecordedQuote],
    viable: Mapping[str, Sequence[QuoteRequest]],
    *,
    log: Callable[[str], None] | None = None,
) -> tuple[list[MatchedQuote], dict[str, str]]:
    """Choose one request per order from its viable set, and derive the estimator map.

    Takes the output of `viable_requests` with ladders attached, since the corroboration
    test needs the winning estimate's amount. Among the candidates of one order the
    corroborated one wins, then the latest — a re-quote at placement time is the quote
    the order actually carries. The solver recorded on the order is **not** consulted
    here: it is what the estimator map is derived *from*, so using it to choose would
    make that derivation circular.

    Returns the matches and `estimator -> solver address`, built only from
    corroborated matches and keyed on the winning estimate's own name rather than the
    request row's `winner` column, so the map rests on the same identity the tier does.
    Per chain by construction (this runs on one network's data) and never carried
    across chains: quote-redesign measured bnb resolving differently.

    The tiering is the correctness lever. A `corroborated` match is confirmed by an
    identity outside the matcher, so the counterfactual ladder is provably the one the
    order was priced from; a `heuristic` one is not, and every figure is reported over
    both sets so the difference is visible rather than assumed away.
    """
    emit: Callable[[str], None] = log or (lambda message: None)
    matches: list[MatchedQuote] = []
    for order in orders:
        candidates = viable.get(order.order_uid, ())
        ranked: list[tuple[bool, float, QuoteRequest]] = []
        for request in candidates:
            winner = request.recorded_winner
            corroborated = winner is not None and winner.out_amount == order.out_amount
            age = _seconds_between(order.created, request.timestamp)
            ranked.append((corroborated, -age, request))
        if not ranked:
            continue
        corroborated, _, request = max(ranked, key=lambda item: (item[0], item[1]))
        matches.append(
            MatchedQuote(
                order=order,
                request=request,
                tier="corroborated" if corroborated else "heuristic",
            )
        )

    counts: dict[str, dict[str, int]] = {}
    for match in matches:
        winner = match.request.recorded_winner
        if match.tier != "corroborated" or winner is None:
            continue
        by_address = counts.setdefault(winner.estimator, {})
        by_address[match.order.quote_solver] = (
            by_address.get(match.order.quote_solver, 0) + 1
        )
    estimator_map = {
        estimator: max(addresses.items(), key=lambda item: item[1])[0]
        for estimator, addresses in counts.items()
    }
    tiers = sum(1 for m in matches if m.tier == "corroborated")
    emit(
        f"matched {len(matches)} of {len(orders)} orders "
        f"({tiers} corroborated, {len(matches) - tiers} heuristic); "
        f"{len(estimator_map)} estimators mapped to solver addresses"
    )
    return matches, estimator_map


def price_suspect_orders(
    orders: Sequence[RecordedQuote], auction_prices: Mapping[str, float]
) -> frozenset[str]:
    """Orders whose two native price feeds for the sell token disagree past
    `PRICE_IMBALANCE_THRESHOLD`.

    `auction_prices` is the autopilot's feed scaled by 1e18 (`primitives.price_in_eth`
    divides by it), so the comparison divides it back down. A token the window's auctions
    never priced cannot be tested and is left alone — the guard excludes what it can prove
    wrong, never what it cannot check, which is D14's rule.
    """
    suspect: set[str] = set()
    for order in orders:
        auction = auction_prices.get(order.sell_token)
        api = order.pricing.sell_token_price
        if not auction or api <= 0:
            continue
        ratio = (auction / ONE_ETH) / api
        if ratio > PRICE_IMBALANCE_THRESHOLD or ratio < 1 / PRICE_IMBALANCE_THRESHOLD:
            suspect.add(order.order_uid)
    return frozenset(suspect)


def load_token_prices(
    conn: Connection, auction_ids: Sequence[int], tokens: Sequence[str]
) -> dict[str, float]:
    """Median native price per token over the window's auctions, for the `autopilot`
    ranking's out-token side. Absent tokens stay absent."""
    prices: dict[str, float] = {}
    if not auction_ids or not tokens:
        return prices
    raw = [bytes.fromhex(token) for token in sorted(set(tokens))]
    for part in chunked(raw, 5000):
        rows = fetch(
            conn,
            TOKEN_PRICES_SQL,
            {"ids": list(auction_ids), "tokens": list(part)},
        )
        for row in rows:
            if row["price"]:
                prices[row["token"]] = float(row["price"])
    return prices


# --- the counterfactual -------------------------------------------------------------

Leaf = Literal[
    "unaffected", "no_quote", "class_flip", "degraded", "unpriced", "nonpositive",
    "price_suspect", "implausible",
]

IMPLAUSIBLE_QUOTE_FACTOR = PRICE_IMBALANCE_THRESHOLD
"""A replacement quote more than this factor away from the recorded quote's rate — in
either direction — is a broken estimate, not a price change. The ladder holds such
estimates: an estimator that mis-scaled a token's decimals answers a buy request with a
cost of a few atoms, and once the winner is removed that answer *wins*, because the
pick maximises. Read as a price change it is millions of basis points of improvement;
charged against as a fee reference it is nonsense of the same size. The same 2x that
D14 uses to call a native price broken is used here, so the two guards say "broken" at
the same point."""


@dataclass(frozen=True)
class QuoteDiff:
    """One matched order under one removal scenario."""

    order_uid: str
    leaf: Leaf
    is_sell: bool
    tier: MatchTier
    winner: str
    replacement: str | None
    effective_base: float
    effective_loo: float
    """The recorded and the counterfactual effective quote, on the request's *out*
    side: buy atoms the user receives on a sell order, sell atoms the user pays on a
    buy one. Both are stored as they are — `delta_bps` orients the comparison."""
    volume_native: int
    partially_fillable: bool
    quote_loo: Quote | None
    """The counterfactual quote the fee path should charge against, or `None` where
    there is nothing to charge — an unaffected order keeps its recorded quote and an
    order that would never exist has no fee at all."""

    @property
    def delta_bps(self) -> float:
        """How much worse the quote is, as the change in the **rate** the user was shown
        — out per in — in basis points. Negative is worse for the user under both order
        kinds, and the measure is the same one for both: a sell order's rate is the
        effective buy amount over the fixed sell amount, a buy order's the fixed buy
        amount over the effective cost, so its ratio is `base / loo`. A rate can fall by
        at most 100%, so one order's weighted contribution is bounded by its own volume;
        the cost-increase reading (`loo / base − 1`) is not, and a replacement quote
        thirty times the recorded cost — the ladder holds such estimates — would then
        carry thirty times its volume into the window figure."""
        if (
            not self.effective_base
            or not self.effective_loo
            or self.leaf in ("unaffected", "no_quote")
        ):
            return 0.0
        ratio = (
            self.effective_loo / self.effective_base
            if self.is_sell
            else self.effective_base / self.effective_loo
        )
        return (ratio - 1.0) * 10_000

    @property
    def delta_volume_native(self) -> float:
        """The bps change carried on this order's volume — the numerator of the
        window-level figure, so that one sum over orders gives it."""
        return self.delta_bps / 10_000 * self.volume_native


def diff_quote(
    match: MatchedQuote,
    removed_estimators: frozenset[str],
    *,
    ranking: QuoteRanking = "effective-sell",
    buy_token_prices: Mapping[str, float] | None = None,
    price_suspects: frozenset[str] = frozenset(),
) -> QuoteDiff:
    """Re-run one request's pick without `removed_estimators` and diff the quotes.

    The **recorded** quote is the baseline, always — never the ladder's winner, even
    where the two agree to the atom (Q-D5). The ladder decides only who replaces it.

    Three leaves exist to keep a ratio from being formed where it means nothing, and
    all are reached only *after* the removal is known to have moved this quote — a
    guard leaf is an excluded win, never a win the removed solver did not have.
    `implausible` is a replacement quote more than `IMPLAUSIBLE_QUOTE_FACTOR` off the
    recorded quote's rate in either direction: a broken estimate promoted to winner.
    `price_suspect` is an order whose two native price feeds disagree past
    `PRICE_IMBALANCE_THRESHOLD`: the same price sets its weight and its gas cost, so
    neither its bps nor its fee can be trusted. `nonpositive` is an order where the gas
    cost swallows the input on one side of the comparison — the protocol clamps such an
    estimate to zero value (`effective_eth_out`, quote.rs:285) and a ratio against it is
    not a price change; the replacement quote is withheld from the fee path too, since
    the same arithmetic is what `fees._adjust_quote_to_order_limits` would refuse.
    """
    order, request = match.order, match.request
    pricing = order.pricing
    if ranking == "autopilot" and order.is_sell:
        price = (buy_token_prices or {}).get(order.buy_token)
        pricing = Pricing(pricing.gas_price, pricing.sell_token_price, price)

    base_effective = _recorded_effective(order)
    # The template every other leaf is a `replace` of, so the fields a leaf does not
    # change are stated once.
    unaffected = QuoteDiff(
        order_uid=order.order_uid,
        leaf="unaffected",
        is_sell=order.is_sell,
        tier=match.tier,
        winner=request.winner,
        replacement=None,
        effective_base=base_effective,
        effective_loo=base_effective,
        volume_native=order.volume_native,
        partially_fillable=order.partially_fillable,
        quote_loo=None,
    )
    if request.winner and request.winner not in removed_estimators:
        # Removing a loser cannot move the winner, and cannot change the winner's
        # verified class either: the class is a property of the estimates that remain,
        # and the winner is in the preferred one by definition.
        return unaffected
    if not any(e.estimator in removed_estimators for e in request.reasonable):
        return unaffected
    if order.order_uid in price_suspects:
        return replace(unaffected, leaf="price_suspect", volume_native=0)

    try:
        replacement = leave_one_out(
            request, removed_estimators, pricing=pricing, ranking=ranking
        )
    except UnpricedRequestError:
        return replace(unaffected, leaf="unpriced")

    if replacement is None:
        return replace(unaffected, leaf="no_quote", effective_loo=0.0, quote_loo=None)

    loo_effective = effective_sell_amount(
        replacement,
        is_sell=order.is_sell,
        in_amount=request.in_amount,
        pricing=order.pricing,
    )
    if base_effective <= 0 or loo_effective <= 0:
        return replace(
            unaffected, leaf="nonpositive", replacement=replacement.estimator,
            effective_loo=loo_effective,
        )
    ratio = (
        loo_effective / base_effective if order.is_sell else base_effective / loo_effective
    )
    if ratio > IMPLAUSIBLE_QUOTE_FACTOR or ratio < 1 / IMPLAUSIBLE_QUOTE_FACTOR:
        return replace(
            unaffected, leaf="implausible", replacement=replacement.estimator,
            effective_loo=loo_effective,
        )
    winner = request.recorded_winner
    flip = bool(winner and winner.verified and not replacement.verified)
    return replace(
        unaffected,
        leaf="class_flip" if flip else "degraded",
        replacement=replacement.estimator,
        effective_loo=loo_effective,
        quote_loo=counterfactual_quote(
            replacement,
            is_sell=order.is_sell,
            in_amount=request.in_amount,
            pricing=order.pricing,
        ),
    )


def _recorded_effective(order: RecordedQuote) -> float:
    """The recorded quote's effective amount, from Postgres alone.

    `Quote.fee` is already the gas cost in sell atoms, so this is the same netting as
    `effective_sell_amount` with the recorded numbers in place of a ladder row.
    """
    if order.is_sell:
        sell = order.quote.sell_amount
        if sell <= 0:
            return 0.0
        return (sell - order.quote.fee) / sell * order.quote.buy_amount
    return float(order.quote.sell_amount + order.quote.fee)


# --- the limit price the order would have carried -----------------------------------


@dataclass(frozen=True)
class ShiftedLimit:
    """What a replacement quote does to the order's limit price, against the recorded
    execution — the one channel besides the fee through which a quote reaches the solve
    side, and the reason it can be held fixed there (PLAN.md D32, D34).

    A user signs a limit derived from the quote they were shown, so a different quote
    means a different limit: the same *wedge* — the user's slippage tolerance, revealed
    as the ratio of the signed limit rate to the recorded effective quote rate — applied
    to the replacement quote's rate instead. That is the whole model, and it is the
    only assumption here.
    """

    clears: bool
    """Would the recorded execution still have satisfied the shifted limit? True by
    construction whenever the replacement quote is worse — a weaker limit is a weaker
    constraint — so this can only be false on a class flip, where the nominal amount
    may rise and the limit tighten."""
    surplus_shift_atoms: int
    """Change in the surplus the mechanism would have *measured* on this execution, in
    the surplus token (buy for a sell order, sell for a buy one): positive when the
    limit weakens. Not a user gain — the user's tokens are unchanged, only their
    reservation moved — which is why it is used to test the ranking (D34) and never
    reported as Δsurplus."""


def _effective_rate(quote: Quote, is_sell: bool) -> Fraction:
    """Buy atoms per sell atom of the effective quote — what the user was shown."""
    if is_sell:
        if quote.sell_amount <= 0:
            return Fraction(0)
        return Fraction(quote.buy_amount * (quote.sell_amount - quote.fee), quote.sell_amount**2)
    cost = quote.sell_amount + quote.fee
    return Fraction(quote.buy_amount, cost) if cost > 0 else Fraction(0)


def shifted_limit(order: Order, recorded: Quote, replacement: Quote) -> ShiftedLimit | None:
    """The order's limit under `replacement`, tested against what it actually executed.

    `None` when there is nothing to test: the order did not execute on this side, or
    one of the quotes has no positive effective rate to form a wedge from.
    """
    is_sell = order.side == "sell"
    rate_recorded = _effective_rate(recorded, is_sell)
    rate_replacement = _effective_rate(replacement, is_sell)
    if (
        rate_recorded <= 0
        or rate_replacement <= 0
        or order.executed_sell <= 0
        or order.executed_buy <= 0
        or order.sell_amount <= 0
        or order.buy_amount <= 0
    ):
        return None
    ratio = rate_replacement / rate_recorded
    limit_rate = Fraction(order.buy_amount, order.sell_amount) * ratio
    executed_rate = Fraction(order.executed_buy, order.executed_sell)
    if is_sell:
        # The limit buy amount scaled to the executed sell amount, before and after.
        required_before = Fraction(order.buy_amount * order.executed_sell, order.sell_amount)
        required_after = required_before * ratio
        shift = required_before - required_after
    else:
        # The limit sell amount scaled to the executed buy amount, before and after.
        allowed_before = Fraction(order.sell_amount * order.executed_buy, order.buy_amount)
        allowed_after = allowed_before / ratio
        shift = allowed_after - allowed_before
    return ShiftedLimit(
        clears=executed_rate >= limit_rate,
        surplus_shift_atoms=int(shift),
    )


# --- per-window aggregation ---------------------------------------------------------


@dataclass
class QuoteAnalysis:
    """The quote side of one removal scenario over one window.

    Three figures lead, one per question the analysis exists to answer: how much worse
    the quotes would be, how often there would be no quote at all, and what the fee
    consequence is. The fee consequence is deliberately *not* a field here — it flows
    into the existing protocol-fee delta through the counterfactual quotes this class
    hands out, so the report carries one fee number rather than two that have to be
    reconciled.

    Two rules shape what the leading figures are summed over, and both were put there by
    a measurement rather than by taste:

    - **Corroborated matches only.** Every sum has an `_all` twin over both match tiers
      (Q-D6), but the headline is the tier whose ladder is confirmed by the winning
      estimate's own amount. On a heuristic match the recorded quote is not the ladder's
      winner, so a runner-up can beat the baseline and the "degradation" comes out
      positive — measured on 2 of 82 wins in a one-day test.
    - **Class flips are outside the bps figures.** Removing the only *verified*
      estimator leaves a quote that may read better in bps while being worse in kind.
      One such flip supplied the whole conditional figure of that same test (+70 bps on
      5 ETH against 81 genuinely degraded orders), which is what a count is for and a
      basis-point average is not.
    """

    solver: str
    ranking: QuoteRanking
    window_orders: int = 0
    """Orders created in the window that carry a recorded quote — the denominator of
    `no_quote_share`, over the whole window rather than the matched part of it: an
    unmatched order contributes zero to every numerator, exactly as an unmoved one
    does (D23's rule). `matched` is what says how much of the window was measured."""
    window_volume_native: int = 0
    """The window's **executed** quoted volume: the denominator of `window_bps`. See
    `RecordedQuote.volume_native` for why executed rather than quoted."""
    window_quoted_volume_native: int = 0
    """What was quoted, executed or not — reported beside the denominator so the gap is
    visible rather than implied."""
    matched: int = 0
    matched_corroborated: int = 0
    quote_wins: int = 0
    quote_wins_corroborated: int = 0
    no_quote: int = 0
    no_quote_all: int = 0
    class_flips: int = 0
    degraded: int = 0
    degraded_all: int = 0
    unpriced: int = 0
    nonpositive: int = 0
    price_suspect: int = 0
    implausible: int = 0
    implausible_volume_native: int = 0
    """Wins — orders whose quote the removed solver did win — excluded from every figure
    because the gas cost swallowed the input, because the order's two native price feeds
    disagree (`PRICE_IMBALANCE_THRESHOLD`), or because the replacement quote is more than
    `IMPLAUSIBLE_QUOTE_FACTOR` off the recorded one (a broken estimate promoted to
    winner). None of these leaves is reached for an order the removed solver merely
    lost, so they are subsets of `quote_wins`; the implausible volume is kept so the
    excluded slice can be sized."""
    delta_volume_native: float = 0.0
    delta_volume_native_all: float = 0.0
    """Σ (Δbps / 10,000 x order volume) over degraded orders — the numerator of
    `window_bps`, so the window figure is one division."""
    degraded_volume_native: int = 0
    degraded_volume_native_all: int = 0
    """Volume of the orders that degraded, the denominator of the conditional figure."""
    flip_volume_native: int = 0
    """Volume behind `class_flips`, so the excluded slice can be sized rather than
    guessed at."""
    diffs: list[QuoteDiff] = field(default_factory=list[QuoteDiff])

    def add(self, diff: QuoteDiff) -> None:
        self.matched += 1
        corroborated = diff.tier == "corroborated"
        if corroborated:
            self.matched_corroborated += 1
        if diff.leaf == "unaffected":
            return
        self.quote_wins += 1
        self.quote_wins_corroborated += corroborated
        self.diffs.append(diff)
        if diff.leaf == "unpriced":
            self.unpriced += 1
            return
        if diff.leaf == "nonpositive":
            self.nonpositive += 1
            return
        if diff.leaf == "price_suspect":
            self.price_suspect += 1
            return
        if diff.leaf == "implausible":
            self.implausible += 1
            self.implausible_volume_native += diff.volume_native
            return
        if diff.leaf == "no_quote":
            self.no_quote_all += 1
            self.no_quote += corroborated
            return
        if diff.leaf == "class_flip":
            self.class_flips += 1
            self.flip_volume_native += diff.volume_native
            return
        self.degraded_all += 1
        self.delta_volume_native_all += diff.delta_volume_native
        self.degraded_volume_native_all += diff.volume_native
        if corroborated:
            self.degraded += 1
            self.delta_volume_native += diff.delta_volume_native
            self.degraded_volume_native += diff.volume_native

    @property
    def window_bps(self) -> float | None:
        """The headline: Δ effective quote spread over the window's whole quoted
        volume, the quote-side analogue of D23. `None` when the window quoted nothing.
        """
        if not self.window_volume_native:
            return None
        return self.delta_volume_native / self.window_volume_native * 10_000

    @property
    def window_bps_all_tiers(self) -> float | None:
        """`window_bps` with heuristic matches in — the Q-D6 companion. A gap between
        the two is a finding about the match, not a range to quote from."""
        if not self.window_volume_native:
            return None
        return self.delta_volume_native_all / self.window_volume_native * 10_000

    @property
    def conditional_bps(self) -> float | None:
        """The same movement over the volume of the orders that actually moved — the
        conditional reading, labelled beside the window one and never instead of it."""
        if not self.degraded_volume_native:
            return None
        return self.delta_volume_native / self.degraded_volume_native * 10_000

    @property
    def no_quote_share(self) -> float | None:
        """Orders that would not have been created, over every order the window
        created."""
        if not self.window_orders:
            return None
        return self.no_quote / self.window_orders

    @property
    def counterfactual_quotes(self) -> dict[str, Quote]:
        """`order_uid -> quote` for the fee path: the counterfactual quote of every
        order whose quote moved, restricted to **corroborated** matches. On a heuristic
        match the ladder's winner is not the recorded quote, so charging a fee against
        that ladder's runner-up would measure the match error as revenue.

        Class flips are included: the fee is charged against whatever quote the order
        would have carried, and an unverified replacement still priced the order. Only
        the *bps* figures leave them out, where a signed average would mislead.
        """
        return {
            d.order_uid: d.quote_loo
            for d in self.diffs
            if d.quote_loo is not None and d.tier == "corroborated"
        }


# --- validation: reproduce the recorded pick ----------------------------------------


@dataclass
class PickValidation:
    """Does our pick return the estimator the competition recorded?

    The quote-side equivalent of M1's gate. It runs on matched orders because the
    ranking needs the order's `gas_price` and `sell_token_price`, and it is reported as
    a rate with its exceptions named rather than asserted: a window where the rate drops
    means the rule changed, and every quote number downstream is then suspect.
    """

    ranking: QuoteRanking
    requests: int = 0
    with_recorded_winner: int = 0
    """Requests whose recorded winner survives `is_reasonable` — the only ones there is
    anything to reproduce on."""
    reproduced: int = 0
    mixed_class: int = 0
    mixed_class_correct: int = 0
    """Requests carrying both verified and unverified estimates, and how many of those
    the verified-first rule got right. The rule is only *tested* by these."""
    unpriced: int = 0
    tied: int = 0
    """Requests where another estimate shares the winner's key to the last float bit, so
    the pick was decided by input order rather than by the rule. In prod the tie goes to
    whichever estimator answered first (`quote.rs:80`, a stable sort over completion
    order), which the ladder does not record."""
    price_suspect: int = 0
    """Matched orders left out because their recorded `sell_token_price` failed the
    cross-check: the gas weight the ranking needs is the very number that is wrong."""
    disagreements: list[tuple[str, str, str]] = field(
        default_factory=list[tuple[str, str, str]]
    )
    """`(request_id, recorded, ours)` for the first few, so a failure can be opened."""

    @property
    def rate(self) -> float | None:
        if not self.with_recorded_winner:
            return None
        return self.reproduced / self.with_recorded_winner


def validate_picks(
    matches: Sequence[MatchedQuote],
    *,
    ranking: QuoteRanking = "effective-sell",
    buy_token_prices: Mapping[str, float] | None = None,
    price_suspects: frozenset[str] = frozenset(),
    keep_disagreements: int = 20,
) -> PickValidation:
    """Re-pick every matched request and compare against the recorded winner."""
    result = PickValidation(ranking=ranking)
    for match in matches:
        order, request = match.order, match.request
        if order.order_uid in price_suspects:
            result.price_suspect += 1
            continue
        result.requests += 1
        winner = request.recorded_winner
        if winner is None:
            continue
        result.with_recorded_winner += 1
        usable = request.reasonable
        if any(e.verified for e in usable) and not all(e.verified for e in usable):
            result.mixed_class += 1
            if winner.verified:
                result.mixed_class_correct += 1
        pricing = order.pricing
        if ranking == "autopilot" and order.is_sell:
            pricing = Pricing(
                pricing.gas_price,
                pricing.sell_token_price,
                (buy_token_prices or {}).get(order.buy_token),
            )
        try:
            ours = pick(
                usable,
                is_sell=request.is_sell,
                in_amount=request.in_amount,
                pricing=pricing,
                ranking=ranking,
            )
        except UnpricedRequestError:
            result.unpriced += 1
            continue
        if ours is None:
            continue
        if ours.estimator == winner.estimator:
            result.reproduced += 1
            key = rank_key(
                winner,
                is_sell=request.is_sell,
                in_amount=request.in_amount,
                pricing=pricing,
                ranking=ranking,
            )
            preferred = [e for e in usable if e.verified] if winner.verified else usable
            if any(
                e.estimator != winner.estimator
                and rank_key(
                    e,
                    is_sell=request.is_sell,
                    in_amount=request.in_amount,
                    pricing=pricing,
                    ranking=ranking,
                )
                == key
                for e in preferred
            ):
                result.tied += 1
        elif len(result.disagreements) < keep_disagreements:
            result.disagreements.append(
                (request.request_id, winner.estimator, ours.estimator)
            )
    return result


# --- one window, loaded once and shared by every scenario ---------------------------


@dataclass
class QuoteWindow:
    """The quote side of one window, loaded once for every removal scenario.

    Loading is the whole cost here as it is on the solve side: the ClickHouse ladder and
    the match are scenario-independent, so N solvers cost one load.
    """

    network: str
    start: str
    """The window the quote side actually covers, clamped forward to the first day of
    recorded quote data. Equal to the run's start unless the backfill begins later."""
    end: str
    ranking: QuoteRanking
    orders: list[RecordedQuote]
    matches: list[MatchedQuote]
    estimator_map: dict[str, str]
    """`estimator -> solver address`, derived from corroborated matches on this chain."""
    buy_token_prices: dict[str, float]
    validation: PickValidation
    coverage_start: str
    """The first prod request ClickHouse holds for this network. An order created before
    it can never be matched, so this is reported rather than left to look like a poor
    match rate."""
    price_suspects: frozenset[str] = frozenset()
    """Orders whose two native price feeds disagree past `PRICE_IMBALANCE_THRESHOLD`.
    Out of every quote figure, on both sides of every ratio, and counted."""
    orders_dropped: int = 0
    orders_before_coverage: int = 0

    @property
    def matched_raw(self) -> int:
        """Matches whose competition the gold tables do not hold."""
        return sum(1 for m in self.matches if m.request.source == "raw")

    @property
    def clean_orders(self) -> list[RecordedQuote]:
        return [o for o in self.orders if o.order_uid not in self.price_suspects]

    @property
    def window_volume_native(self) -> int:
        return sum(o.volume_native for o in self.clean_orders)

    @property
    def window_quoted_volume_native(self) -> int:
        return sum(o.quoted_volume_native for o in self.clean_orders)

    def coverage_by_app(self) -> list[tuple[str, int, int]]:
        """`(app_code, orders, matched)`, busiest first.

        The only honest unit for coverage: on 2026-07-25 mainnet it ran from 99.3% for
        CoW Swap and 100% for the aggregator integrations down to 1.8% for Trust Wallet
        and 0% for `defibot`, so a single window-level rate describes nobody.
        """
        matched = {m.order.order_uid for m in self.matches}
        totals: dict[str, list[int]] = {}
        for order in self.orders:
            entry = totals.setdefault(order.app_code, [0, 0])
            entry[0] += 1
            entry[1] += order.order_uid in matched
        return sorted(
            ((app, n, m) for app, (n, m) in totals.items()),
            key=lambda row: -row[1],
        )

    @property
    def matched_share(self) -> float | None:
        if not self.orders:
            return None
        return len(self.matches) / len(self.orders)

    def estimators_of(self, addresses: frozenset[str]) -> frozenset[str]:
        """The estimator names belonging to a removal scenario's solver addresses.

        Empty means the scenario never won a quote we could attribute, which is a
        finding (a solve-only solver), not an error.
        """
        return frozenset(
            estimator
            for estimator, address in self.estimator_map.items()
            if address in addresses
        )

    def analyse(self, solver: str, addresses: frozenset[str]) -> QuoteAnalysis:
        """Diff every matched order for one removal scenario."""
        analysis = QuoteAnalysis(
            solver=solver,
            ranking=self.ranking,
            window_orders=len(self.clean_orders),
            window_volume_native=self.window_volume_native,
            window_quoted_volume_native=self.window_quoted_volume_native,
        )
        removed = self.estimators_of(addresses)
        if not removed:
            return analysis
        for match in self.matches:
            analysis.add(
                diff_quote(
                    match,
                    removed,
                    ranking=self.ranking,
                    buy_token_prices=self.buy_token_prices,
                    price_suspects=self.price_suspects,
                )
            )
        return analysis


def load_quote_window(
    client: ch.Client,
    conn: Connection,
    network: str,
    start: str,
    end: str,
    *,
    ranking: QuoteRanking = "effective-sell",
    auction_ids: Sequence[int] = (),
    log: Callable[[str], None] | None = None,
) -> QuoteWindow:
    """Load the quote competition for `[start, end)` on one network.

    Candidates come from the gold request table first and, for the orders that leaves
    unplaced, from the raw estimates (`load_raw_candidates`); ladders follow the same
    split. Measured on 2026-07-25 mainnet the two together place 2,935 of 2,937 orders,
    and every raw-layer match is corroborated by the winning estimate's own amount.

    Raises `KeyError` for a network with no quote data at all: reporting empty quote
    columns would read as "this solver quoted nothing" rather than "there is no quote
    data here" (Q-D10).
    """
    emit: Callable[[str], None] = log or (lambda message: None)
    ch.chain_id(network)  # refuse an unsupported network before any query runs
    coverage = client.coverage(network)
    if coverage is None:
        raise ch.NoQuoteDataError(f"no prod quote requests recorded for {network!r}")
    coverage_start = coverage[0]
    emit(f"quote data for {network}: {coverage[0]} .. {coverage[1]}")

    # An order created before the quote data begins can never be matched, so it is
    # excluded from the population rather than counted as unmatched: leaving it in would
    # deflate every quote share by however much of the window predates the backfill —
    # two thirds of a July window, at the time of writing.
    quote_start = max(start, coverage_start[:10])
    all_orders, dropped = load_recorded_quotes(conn, quote_start, end)
    orders = [o for o in all_orders if o.created >= coverage_start]
    before = len(all_orders) - len(orders)
    emit(
        f"{len(orders)} orders created in [{quote_start}, {end}) carry a recorded quote "
        f"({dropped} dropped as unevaluable, {before} created before the quote data "
        f"begins)"
        + (
            f"; the quote side covers [{quote_start}, {end}) of the run's "
            f"[{start}, {end})"
            if quote_start != start
            else ""
        )
    )
    requests = load_candidate_requests(client, network, orders, log=log)
    viable = viable_requests(orders, requests)
    unplaced = [o for o in orders if o.order_uid not in viable]
    emit(
        f"{len(viable)} of {len(orders)} orders have a candidate in the gold request "
        f"table; asking the raw layer for the other {len(unplaced)}"
    )
    if unplaced:
        raw_requests = load_raw_candidates(client, network, unplaced, log=log)
        viable.update(viable_requests(unplaced, raw_requests))
    needed = {r.request_id: r for rs in viable.values() for r in rs}
    emit(
        f"{len(needed)} requests survive the time filter for {len(viable)} of "
        f"{len(orders)} orders — ladders are loaded for those only"
    )
    ladders = load_ladders(client, network, list(needed.values()))
    with_ladders = {
        uid: [with_ladder(request, ladders.get(request.request_id, ())) for request in rs]
        for uid, rs in viable.items()
    }
    emit(f"{sum(len(v) for v in ladders.values())} estimates loaded")
    matches, estimator_map = match_orders(orders, with_ladders, log=log)
    emit(
        f"{sum(1 for m in matches if m.request.source == 'raw')} matches rest on "
        f"competitions the gold tables do not hold"
    )

    # The cross-check runs on every ranking: it is a data guard, not a ranking input.
    # Sell tokens for the guard, buy tokens too when the autopilot rule needs them.
    guard_tokens = sorted({o.sell_token for o in orders})
    wanted = guard_tokens + (
        sorted({m.order.buy_token for m in matches}) if ranking == "autopilot" else []
    )
    auction_prices = load_token_prices(conn, auction_ids, wanted)
    buy_token_prices = auction_prices if ranking == "autopilot" else {}
    suspects = price_suspect_orders(orders, auction_prices)
    unpriced_tokens = sum(1 for token in guard_tokens if token not in auction_prices)
    emit(
        f"native price cross-check: {len(suspects)} of {len(orders)} orders excluded "
        f"(the api's sell_token_price and the auction price disagree past "
        f"{PRICE_IMBALANCE_THRESHOLD}x); {unpriced_tokens} of {len(guard_tokens)} sell "
        f"tokens the window's auctions never priced, which the guard cannot test"
    )

    validation = validate_picks(
        matches,
        ranking=ranking,
        buy_token_prices=buy_token_prices,
        price_suspects=suspects,
    )
    rate = validation.rate
    emit(
        f"pick reproduces the recorded winner on "
        f"{validation.reproduced}/{validation.with_recorded_winner}"
        + (f" ({rate:.3%})" if rate is not None else "")
        + f"; verified-class rule tested on {validation.mixed_class} requests, correct "
        f"on {validation.mixed_class_correct}; {validation.price_suspect} price-suspect "
        f"matches left out of the check"
    )
    return QuoteWindow(
        network=network,
        start=quote_start,
        end=end,
        ranking=ranking,
        orders=orders,
        matches=matches,
        estimator_map=estimator_map,
        buy_token_prices=buy_token_prices,
        validation=validation,
        coverage_start=coverage_start,
        price_suspects=suspects,
        orders_dropped=dropped,
        orders_before_coverage=before,
    )
