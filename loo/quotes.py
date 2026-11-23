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
is_reasonable          drop any estimate with gas == 0 or out_amount == 0
compare_quote_result   a verified estimate beats an unverified one at any amount
compare_quote          otherwise rank on the effective out amount:
effective_out_amount   gas cost in the sell token = gas × gas_price / native(sell)
                       sell: out − cost × out/in, maximise;  buy: out + cost, minimise
                       clamped at zero, truncated to an integer
results.sort_by        stable, over the answers in arrival order — a tie goes first-come
```

That is the rule since services#4846 (merged 2026-09-02); before it, `effective_eth_out`
valued the out amount at the *out* token's native price and netted gas in wei. Three facts
make it reproducible from recorded data, all measured (docs/quotes.md):

1. The **verified class is a hard first key.** Over 5,041 requests carrying both
   verified and unverified estimates, a verified one won all 5,041.
2. `gas_price` and `native(sell)` are the order's own `order_quotes.gas_price` and
   `sell_token_price`, so the rule needs nothing that is not recorded per order. Even
   before #4846 they entered only through a ratio common to every estimator of one
   request, and the sell-token netting reproduced the recorded winner on **99.6%** of
   mainnet and **100.0%** of bnb order-creating requests (2026-08-10..16); the gross
   `out_amount` alone reproduces 61.5% / 90.2%.
3. **Arrival order is recorded** — `nats_seq` on the raw layer — so the clamp and the
   truncation, which make ties common on dust requests, are reproducible too.

So there are two ranking rules, and `RANKING` names them:

- `effective-sell` (default) — the effective quote netted in the sell token. The
  protocol's own rule since #4846, and the same quantity the price-improvement fee is
  charged against, so pick and fee cannot disagree.
- `autopilot` — the out-token rule from before #4846, which needs the *buy* token's
  native price for sell orders. Kept as the cross-check: it measures how far the two
  differ, which is exactly the quote's own pricing error, since `in × sell_price ≈ out ×
  buy_price` is what a quote asserts. For a **buy** order the out token *is* the sell
  token, so the two rules differ there only in the unit they truncate in.

## Linking an order to its competition

By quote id (`link_by_quote_id`): the order records the id of the quote it was placed
with, every estimate records its own id and its estimator's settlement address, so the
link is a join and the estimator map is read rather than derived. Per-estimate ids exist
on every chain from 2026-09-29 17:16 UTC, well before the earliest window the solve side
accepts (`extract.MECHANISM_START`).

An order whose recorded quote solver is the zero address had no competition at all — a
wrapped-to-native unwrap, answered identically by every estimator — and is not linked
(`RecordedQuote.trivial`).

## What is exact and what is not

The raw layer stores amounts as exact decimal strings, and the link reads only the raw
layer, so its ladders are exact ints. The
recorded quote, its fee and every solve-side number stay exact, because they come from
Postgres: the ladder supplies only the *counterfactual*, never the baseline (Q-D5). A link
failure therefore degrades the counterfactual and can be counted; it can never corrupt a
recorded number.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import TYPE_CHECKING, Literal

from . import ch
from .db import Connection, as_int, chunked, fetch, raw_schema
from .extract import fetch_prices
from .fees import FeeError, Quote, quote_fee
from .primitives import ONE_ETH, as_erc20, price_in_eth, wrapped_native_token

if TYPE_CHECKING:  # only `shifted_limit` needs the type; `valuation` imports `fees` too
    from .valuation import Order

QuoteRanking = Literal["effective-sell", "autopilot"]

RANKING: dict[QuoteRanking, str] = {
    "effective-sell": (
        "the effective quote netted in the sell token, at the order's recorded "
        "gas_price / sell_token_price, truncated to whole atoms — the protocol's own rule "
        "since services#4846 (merged 2026-09-02), the same quantity the price-improvement "
        "fee is charged against, and reproducible from the record alone"
    ),
    "autopilot": (
        "the protocol's rule before services#4846: out_amount valued in wei at the out "
        "token's native price less gas x gas_price, with the buy token's native price "
        "taken from the window's auction prices for sell orders"
    ),
}

PRICE_IMBALANCE_THRESHOLD = 2.0
"""D14 on the quote side, applied by two checks that catch different breakages.

**One-sided** (`price_suspect_orders`): two independent feeds price the order's sell
token — the api's, recorded per order as `order_quotes.sell_token_price` (wei per atom),
and the autopilot's, in the auction's own price vector (the same quantity scaled by
1e18). They agree to four decimals on real tokens — measured 1.0000 for WETH, 0.9999 for
USDC, 1.0004 for USDT — and disagree by factors of 1e7 to 1e18 where one of them is
broken. The same wrong price sets both the order's weight and its gas cost: one such
order carried 6.9 M ETH of apparent executed volume, 90% of a corrected window.

**Two-sided** (`imbalanced_orders`): when both feeds carry the *same* wrong price the
first check passes, so the order's sold value is also compared with its bought value,
trade by trade at the prices of the auction each trade settled in — the solve side's
`counterfactual.price_imbalanced`, per order. On bnb 2026-10-05..12, 26 orders priced
that way passed the one-sided check and carried 28.68 M of the 28.70 M BNB of executed
volume it let through (the solve side's whole week was 16,988 BNB).

An order failing either is excluded from every quote figure and counted."""


TRIVIAL_QUOTE_SOLVER = "00" * 20
"""`order_quotes.solver` of an order whose quote no competition decided: every one seen
is a wrapped-native-to-native unwrap (WETH→ETH, WBNB→BNB) — 10 on mainnet 2026-07-25, 29
on 2026-10-08, 22 on bnb that day, no other pair. Every estimator answers such a request
identically and arrival order picks the winner, so it is not linked."""

PRE_CREATION_WINDOW_SECONDS = 30 * 60
POST_CREATION_WINDOW_SECONDS = 90
"""How far around an order's creation day the estimates are searched: a quote can
precede its order by up to half an hour, and the backend's re-quote at indexing lands
just after creation."""


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
    All three are wei per atom; the auction feed's 1e18 scaling is divided out first.
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
    kind: str
    sell_token: str
    buy_token: str
    in_amount: float
    winner: str
    """The estimator `winning_price_estimates` recorded. Empty when the request has no
    recorded winner, which `pick` then has nothing to be checked against."""
    estimates: tuple[Estimate, ...] = ()
    """In **arrival order** (`nats_seq`). Prod breaks an exact tie by arrival (a stable sort over
    the answers in completion order, `quote.rs:80`) and `pick` keeps the first of equal
    keys, so the order here is what makes a tie reproducible."""

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
    every caller can use plain `max`.

    Each rule is prod's arithmetic, in prod's order of operations, down to its two
    discontinuities, because both decide winners in practice:

    - **The clamp.** A sell quote whose gas exceeds its value is worth zero, so every
      underwater estimate ties and arrival order decides. On bnb 2026-10-05..12 the
      unclamped key named another winner on dust requests — 0.006 USDT against ~330 k
      gas — where prod had taken the first answer.
    - **The truncation.** The value is truncated to an integer before comparing
      (`U256::saturating_from(v.trunc())`) — atoms of the out token under
      `effective-sell`, wei under `autopilot`. On an out amount of 35 atoms, 32.79 and
      32.24 are both 32, so again arrival order decides: six bnb requests that week, all
      of them reproduced once the key truncates.

    The counterfactual is unaffected by either: a replacement under water lands in the
    `nonpositive` leaf whichever underwater estimate it is, and a tie is between
    estimates prod itself could not tell apart."""
    if ranking == "effective-sell":
        # `RankingContext::effective_out_amount` (services#4846, merged 2026-09-02).
        cost = pricing.gas_cost_in_sell_atoms(estimate.gas)
        if is_sell:
            value = estimate.out_amount - cost * (estimate.out_amount / in_amount)
            return float(math.trunc(value)) if value > 0 else 0.0
        return -float(math.trunc(estimate.out_amount + cost))
    if ranking == "autopilot":
        # `effective_eth_out` before services#4846: the out amount valued in wei at the
        # out token's native price, gas at the gas price, truncated in wei.
        price = pricing.sell_token_price if not is_sell else pricing.buy_token_price
        if not price:
            raise UnpricedRequestError(
                "the autopilot ranking needs the out token's native price"
            )
        out_wei = estimate.out_amount * price
        gas_wei = estimate.gas * pricing.gas_price
        if is_sell:
            value = out_wei - gas_wei
            return float(math.trunc(value)) if value > 0 else 0.0
        return -float(math.trunc(out_wei + gas_wei))
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
    estimate in the given order — arrival order for a loaded ladder
    (`QuoteRequest.estimates`), which is prod's own tie-break; `PickValidation.tied`
    counts the requests where it mattered.
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


def replay_stream(
    estimates: Sequence[Estimate],
    *,
    is_sell: bool,
    in_amount: float,
    pricing: Pricing,
    ranking: QuoteRanking = "effective-sell",
) -> list[str]:
    """The estimators a stream sends to the client, in order: every reasonable answer
    that strictly beats the best one so far, on the same verified-first key as `pick`
    (`estimate_stream`, `beats_best = compare_quote_result(…).is_gt()`) — less the ones
    the quoter then drops because their gas cost does not fit the sell amount
    (`SellAmountDoesNotCoverFee` in `OrderQuoter::calculate_quote_stream`). A dropped
    answer still sets the bar the next one must beat, though nobody saw it. `estimates`
    must be in arrival order."""
    forwarded: list[str] = []
    best: tuple[bool, float] | None = None
    for estimate in estimates:
        if not estimate.reasonable:
            continue
        key = (
            estimate.verified,
            rank_key(
                estimate, is_sell=is_sell, in_amount=in_amount, pricing=pricing,
                ranking=ranking,
            ),
        )
        if best is None or key > best:
            best = key
            if _sendable(estimate, is_sell=is_sell, in_amount=in_amount, pricing=pricing):
                forwarded.append(estimate.estimator)
    return forwarded


def _sendable(
    estimate: Estimate, *, is_sell: bool, in_amount: float, pricing: Pricing
) -> bool:
    """Whether the quoter turns a forwarded answer into a quote at all. On bnb
    2026-10-05..12 the record lacked a forwarded answer in 15 of 11,523 streams, and in
    every one its gas cost was 1.02–1.70x the sell amount. Only sell orders are checked:
    no buy-order case has been seen."""
    return not is_sell or pricing.gas_cost_in_sell_atoms(estimate.gas) < in_amount


class UnrecordedStreamError(RuntimeError):
    """The stream was dropped before the answer the user would have taken arrived."""


def stream_replacement(
    request: QuoteRequest,
    take: StreamTake,
    removed: frozenset[str],
    *,
    pricing: Pricing,
    ranking: QuoteRanking = "effective-sell",
) -> tuple[Estimate | None, bool]:
    """What the user of a streamed order would have committed to with `removed` absent,
    given that the answer they took is among them.

    The best remaining answer that had arrived by the commit (`take.horizon`). If none
    had, the user waits for the stream's next remaining answer and takes that: in a
    finished stream it is in the ladder; in a dropped one it was cancelled unrecorded,
    which raises `UnrecordedStreamError`. `None` means the stream never had another
    usable answer, so no quote at all.

    Also returns whether the commit time mattered: whether a commit the moment the
    taken answer was forwarded — the earliest the record allows — would have shown a
    different estimator. A count for the reader, not a second scenario.
    """
    remaining = [e for e in request.estimates if e.estimator not in removed]

    def best(upto: int) -> Estimate | None:
        arrived = [
            e for e in request.estimates[:upto] if e.estimator not in removed
        ]
        return pick(
            arrived, is_sell=request.is_sell, in_amount=request.in_amount,
            pricing=pricing, ranking=ranking,
        )

    def waited(after: int) -> Estimate | None:
        later = [
            e for e in request.estimates[after:]
            if e.estimator not in removed
            and e.reasonable
            and _sendable(
                e, is_sell=request.is_sell, in_amount=request.in_amount, pricing=pricing
            )
        ]
        if not later:
            if not take.complete:
                raise UnrecordedStreamError(
                    "the stream was dropped before another answer arrived"
                )
            return None
        # The first remaining answer to arrive is what the stream would have sent.
        return later[0]

    if not any(e.reasonable for e in remaining):
        if not take.complete:
            raise UnrecordedStreamError("no other answer was recorded")
        return None, False
    late = best(take.horizon) or waited(take.horizon)
    try:
        early = best(take.position + 1) or waited(take.position + 1)
    except UnrecordedStreamError:
        early = None
    moved = (late.estimator if late else None) != (early.estimator if early else None)
    return late, moved


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

# `quote_id` is read from the backend's own `order_quotes` mirror, not the staging model:
# `stg_backend_data__order_quotes` is incremental with `append_new_columns`, so its
# `quote_id` is NULL on every row built before the column was added (~2026-10-06), while
# the raw table has it from 2026-09-22.
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
    encode(oq.solver, 'hex')            as quote_solver,
    oq.sell_amount                      as quote_sell_amount,
    oq.buy_amount                       as quote_buy_amount,
    oq.gas_amount,
    oq.gas_price,
    oq.sell_token_price,
    oq.verified                         as quote_verified,
    coalesce(ad.app_code, '(none)')     as app_code,
    coalesce(t.executed_sell, 0)        as executed_sell,
    rq.quote_id
from dbt.stg_backend_data__orders o
join dbt.stg_backend_data__order_quotes oq on oq.order_uid = o.uid
left join {raw_schema}.order_quotes rq on rq.order_uid = o.uid
left join dbt.stg_backend_data__app_data ad on ad.contract_app_data = o.app_data
left join traded t on t.order_uid = o.uid
where o.creation_timestamp >= %(start)s and o.creation_timestamp < %(end)s
order by o.creation_timestamp
"""

# The autopilot's native price per token: the median over the window's auctions. Crude by
# design — it is a cross-check and the `autopilot` ranking's input, never a valuation —
# and a token the window never priced stays absent rather than being guessed. Prices are
# read through `extract.fetch_prices`; the source is described above `extract.PRICES_SQL`.
TOKEN_PRICES_SQL = """
select encode(ap.token, 'hex') as token, percentile_cont(0.5) within group (
           order by ap.price
       ) as price
from {auction_prices} ap
where ap.token = any(%(tokens)s)
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
    quote_id: int | None = None
    """The id of the quote the order was placed with, as the backend's `order_quotes`
    records it — the key of the quote-id link. `None` before 2026-09-22 and `0` on an
    ethflow order placed without one; both mean there is nothing to join on."""

    @property
    def is_sell(self) -> bool:
        return self.kind == "sell"

    @property
    def trivial(self) -> bool:
        """No competition decided this quote (`TRIVIAL_QUOTE_SOLVER`)."""
        return self.quote_solver == TRIVIAL_QUOTE_SOLVER

    @property
    def in_atoms(self) -> int:
        """`in_amount` as the exact integer the order stores — the request's key."""
        return self.quote.sell_amount if self.is_sell else self.quote.buy_amount

    @property
    def out_atoms(self) -> int:
        """`out_amount` as the exact integer the order stores — what the winning estimate
        of the order's competition must equal to the atom."""
        return self.quote.buy_amount if self.is_sell else self.quote.sell_amount

    @property
    def in_amount(self) -> float:
        return float(self.quote.sell_amount if self.is_sell else self.quote.buy_amount)

    @property
    def out_amount(self) -> float:
        """The amount the linked estimate must have returned for the link to hold."""
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
    conn: Connection, start: str, end: str, *, network: str
) -> tuple[list[RecordedQuote], int]:
    """Orders created in `[start, end)` on `network` that carry a recorded quote.

    Returns the orders and the number of rows dropped because their quote could not be
    evaluated — a zero `sell_token_price` makes the gas cost undefined, and an order
    whose own quote cannot be netted cannot have a counterfactual either.
    """
    orders: list[RecordedQuote] = []
    dropped = 0
    sql = ORDERS_SQL.format(raw_schema=raw_schema(network))
    for row in fetch(conn, sql, {"start": start, "end": end}):
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
                quote_id=row["quote_id"],
            )
        )
    return orders, dropped


# --- ClickHouse: ladders ------------------------------------------------------------


def _time_bounds(start: str, end: str) -> tuple[str, str]:
    """The ClickHouse window for orders created in `[start, end)`: wide enough for a
    quote that precedes its order and for a re-quote just after the last one."""
    from datetime import datetime, timedelta

    first = datetime.fromisoformat(start) - timedelta(
        seconds=PRE_CREATION_WINDOW_SECONDS
    )
    last = datetime.fromisoformat(end) + timedelta(seconds=POST_CREATION_WINDOW_SECONDS)
    return first.strftime("%Y-%m-%d %H:%M:%S"), last.strftime("%Y-%m-%d %H:%M:%S")


Arrival = int
"""When an estimate arrived, as a sort key: its `nats_seq`."""


def _keep_earliest(
    rows: dict[tuple[str, str], tuple[Arrival, Estimate]],
    request_id: str,
    arrival: Arrival,
    estimate: Estimate,
) -> None:
    """One estimate per `(request_id, estimator)` — a `ReplacingMergeTree` can hold a row
    twice until it merges — keeping the earliest arrival."""
    key = (request_id, estimate.estimator)
    if key not in rows or arrival < rows[key][0]:
        rows[key] = (arrival, estimate)


def _ladders(
    rows: Mapping[tuple[str, str], tuple[Arrival, Estimate]],
) -> dict[str, tuple[Estimate, ...]]:
    """Group deduplicated estimates into ladders, each in arrival order."""
    ladders: dict[str, list[tuple[Arrival, Estimate]]] = {}
    for (request_id, _), entry in rows.items():
        ladders.setdefault(request_id, []).append(entry)
    return {
        rid: tuple(e for _, e in sorted(items, key=lambda item: item[0]))
        for rid, items in ladders.items()
    }


# --- matching -----------------------------------------------------------------------


@dataclass(frozen=True)
class MatchedQuote:
    """One order joined to the request that priced it."""

    order: RecordedQuote
    request: QuoteRequest
    """The linked estimate reproduces the amount stored on the order to the atom, or
    there is no match (`link_by_quote_id`)."""
    stream: StreamTake | None = None
    """Set when the order was placed from a streamed quote (`StreamTake`); the
    counterfactual then follows what the stream would have shown, not the full ladder."""


@dataclass(frozen=True)
class StreamTake:
    """Which answer of a streamed competition an order took, and what had arrived by the
    time its user committed.

    `/api/v1/quote/stream` forwards every answer that beats the best one sent so far and,
    since services#4957, records each forwarded answer as a `quote_computed` row and the
    final best as the winner. The user commits to whichever forwarded answer is on screen;
    the order carries its quote id. The commit itself is not recorded, so it is placed at
    the latest moment the record allows: just before the next forwarded answer arrived,
    or, for the last one, before the order was created (`horizon`).
    """

    taken: str
    """The estimator whose forwarded answer the order carries."""
    position: int
    """Its index in the arrival-ordered ladder."""
    horizon: int
    """Answers `[0, horizon)` of the ladder may have arrived before the user committed."""
    complete: bool
    """The stream reported a winner, so it ran to the end and every answer it received is
    in the ladder. A stream the client dropped early has no winner, and the answers it
    cancelled were never recorded."""
    forwarded: tuple[str, ...] = ()
    """The estimators of the request's `quote_computed` rows, in order — what the stream
    forwarded, as recorded; `validate_picks` replays the stream against it."""


# --- the quote-id link --------------------------------------------------------------


@dataclass(frozen=True)
class LinkStats:
    """How the quote-id link fared on the orders it was asked to place."""

    orders: int
    """Non-trivial orders handed to the link — the base its rate is over."""
    linked: int
    unlinked: Mapping[str, int] = field(default_factory=dict[str, int])
    """Why the rest were not linked, by reason."""
    requoted: int = 0
    """Of `linked`, the orders linked to the backend's own re-quote rather than through
    their quote id (`_link_requotes`) — ethflow orders whose stored quote was recomputed
    when the order was indexed."""
    streamed: int = 0
    streamed_early: int = 0
    """Of `linked`, the orders placed from a streamed quote (`StreamTake`), and of those
    the ones whose answer is not the stream's recorded final best — taken before the
    stream finished, or from a stream dropped before it reported one."""

    @property
    def rate(self) -> float | None:
        return self.linked / self.orders if self.orders else None


@dataclass(frozen=True)
class IdLink:
    matches: list[MatchedQuote]
    estimator_map: dict[str, str]
    """`estimator -> settlement address` as `price_estimates.solver` records it — read,
    not derived, so it covers every estimator in the loaded ladders, including ones that
    never won."""
    stats: LinkStats


ID_LADDER_COLUMNS = (
    "request_id, estimator, solver, quote_id, kind, lower(sell_token) as sell_, "
    "lower(buy_token) as buy_, in_amount, out_amount, gas, verified, nats_seq, "
    "toString(event_timestamp) as ts"
)


def _exact(value: object) -> float:
    """A raw-layer amount — an exact decimal string — as the `int` it is. Typed `float`
    because an `Estimate` holds either; an int compares and rounds exactly."""
    text = str(value)
    try:
        return int(text)
    except ValueError:
        return float(text)


def link_by_quote_id(
    client: ch.Client,
    network: str,
    orders: Sequence[RecordedQuote],
    *,
    chunk: int = 2000,
    log: Callable[[str], None] | None = None,
) -> IdLink:
    """Link each order to its competition by the id of the quote it was placed with.

    `order_quotes.quote_id` → the `raw_quotes.price_estimates` row carrying that id → its
    `request_id` → every estimate of that request, with its winner from
    `winning_price_estimates`. One query per creation day and chunk of ids for each of
    the two, the ids resolved to request ids by a subquery so the ladder comes back in
    the same round trip. The raw tables partition by month, so the day bound prunes
    little; it is what keeps an id from matching an estimate of another day.

    The amounts are exact decimal strings and are kept as ints, so the check that makes a
    link a link is exact: the linked estimate must be its competition's recorded winner
    and reproduce the amount stored on the order to the atom. Two kinds of order fail
    it, and each is handled for what it is:

    - **An ethflow order whose stored quote was recomputed when it was indexed**, or that
      was placed with `quote_id = 0`: the id names the UI's quote, or nothing, while the
      stored one came from a competition the backend ran itself, with no request id. A
      second pass finds that competition by the stored amount (`_link_requotes`).
    - **An order placed from a streamed quote**: the id is right, but the answer it names
      need not be the competition's recorded winner — the stream forwards every
      improvement and records only its final best, or none if the client dropped it.
      Such an order is linked with a `StreamTake`, read from the request's
      `quote_computed` rows, and its counterfactual follows the stream
      (`stream_replacement`).
    """
    from datetime import date, timedelta

    emit: Callable[[str], None] = log or (lambda message: None)
    chain = ch.chain_id(network)
    with_id = [o for o in orders if o.quote_id]
    by_day: dict[str, list[RecordedQuote]] = {}
    for order in with_id:
        by_day.setdefault(order.created[:10], []).append(order)

    rows: dict[tuple[str, str], tuple[Arrival, Estimate]] = {}
    fields_of: dict[str, ch.Row] = {}
    by_quote_id: dict[int, tuple[str, str]] = {}
    computed: dict[str, dict[int, int]] = {}
    ids_of: dict[str, dict[int, str]] = {}
    arrived_at: dict[tuple[str, str], str] = {}
    winners: dict[str, set[str]] = {}
    addresses: dict[str, dict[str, int]] = {}
    for day in sorted(by_day):
        after = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        first, last = _time_bounds(day, after)
        window = f"event_timestamp >= '{first}' and event_timestamp < '{last}'"
        ids_of_day = sorted({o.quote_id for o in by_day[day] if o.quote_id})
        for part in chunked(ids_of_day, chunk):
            ids = ", ".join(str(int(quote_id)) for quote_id in part)
            linked = (
                f"select request_id from {ch.RAW_DATABASE}.price_estimates "
                f"where chain_id = {chain} and {window} and quote_id in ({ids})"
            )
            for row in client.fetch(
                f"select request_id, estimator from "
                f"{ch.RAW_DATABASE}.winning_price_estimates "
                f"where chain_id = {chain} and {window} and request_id in ({linked})"
            ):
                winners.setdefault(row["request_id"], set()).add(row["estimator"])
            # What a stream forwarded: one `quote_computed` row per forwarded answer,
            # where a one-shot request has a single one (services#4957).
            for row in client.fetch(
                f"select request_id, quote_id, nats_seq from "
                f"{ch.RAW_DATABASE}.quote_computed "
                f"where chain_id = {chain} and {window} and request_id in ({linked}) "
                f"and quote_id is not null"
            ):
                computed.setdefault(row["request_id"], {})[int(row["quote_id"])] = int(
                    row["nats_seq"]
                )
            for row in client.fetch(
                f"select {ID_LADDER_COLUMNS} from {ch.RAW_DATABASE}.price_estimates "
                f"where chain_id = {chain} and success and {window} "
                f"and request_id in ({linked})"
            ):
                request_id: str = row["request_id"]
                estimator: str = row["estimator"]
                arrived_at.setdefault((request_id, estimator), str(row["ts"]))
                if row["quote_id"] is not None:
                    by_quote_id[int(row["quote_id"])] = (request_id, estimator)
                    ids_of.setdefault(request_id, {})[int(row["quote_id"])] = estimator
                if row["solver"]:
                    address = str(row["solver"]).lower().removeprefix("0x")
                    seen = addresses.setdefault(estimator, {})
                    seen[address] = seen.get(address, 0) + 1
                fields_of.setdefault(request_id, row)
                _keep_earliest(
                    rows,
                    request_id,
                    int(row["nats_seq"]),
                    Estimate(
                        estimator=estimator,
                        out_amount=_exact(row["out_amount"]),
                        gas=_exact(row["gas"]),
                        verified=bool(row["verified"]),
                        is_winner=estimator in winners.get(request_id, ()),
                    ),
                )

    requests: dict[str, QuoteRequest] = {}
    for request_id, ladder in _ladders(rows).items():
        row = fields_of[request_id]
        named = winners.get(request_id, set())
        requests[request_id] = QuoteRequest(
            request_id=request_id,
            kind=row["kind"],
            sell_token=str(row["sell_"]).removeprefix("0x"),
            buy_token=str(row["buy_"]).removeprefix("0x"),
            in_amount=_exact(row["in_amount"]),
            winner=next(iter(named)) if len(named) == 1 else "",
            estimates=ladder,
        )

    matches: list[MatchedQuote] = []
    unlinked = {
        "no quote id": 0,
        "id not in the competition record": 0,
        "not a forwarded answer of its stream": 0,
        "stored quote not reproduced": 0,
    }
    # Orders the id cannot place but a re-quote might have priced, with the reason that
    # stands if none did.
    requote_candidates: list[tuple[RecordedQuote, str]] = [
        (o, "no quote id") for o in orders if not o.quote_id
    ]
    for order in with_id:
        found = by_quote_id.get(order.quote_id or 0)
        request = requests.get(found[0]) if found else None
        estimate = (
            next((e for e in request.estimates if e.estimator == found[1]), None)
            if found and request
            else None
        )
        if found is None or request is None or estimate is None:
            requote_candidates.append((order, "id not in the competition record"))
            continue
        if estimate.out_amount != order.out_atoms:
            requote_candidates.append((order, "stored quote not reproduced"))
            continue
        estimator_of = ids_of.get(request.request_id, {})
        forwarded = tuple(
            estimator_of[qid]
            for qid, _ in sorted(
                computed.get(request.request_id, {}).items(), key=lambda item: item[1]
            )
            if qid in estimator_of
        )
        streamed = len(forwarded) >= 2 or request.winner != found[1]
        if not streamed:
            matches.append(MatchedQuote(order=order, request=request))
            continue
        if forwarded and found[1] not in forwarded:
            unlinked["not a forwarded answer of its stream"] += 1
            continue
        matches.append(
            MatchedQuote(
                order=order,
                request=request,
                stream=_stream_take(order, request, found[1], forwarded, arrived_at),
            )
        )

    streamed = [m for m in matches if m.stream is not None]
    streamed_early = sum(
        1 for m in streamed if m.stream and m.stream.taken != m.request.winner
    )
    requoted, requote_addresses = _link_requotes(
        client, chain, [o for o, _ in requote_candidates]
    )
    matches.extend(requoted)
    placed = {m.order.order_uid for m in requoted}
    for order, why in requote_candidates:
        if order.order_uid not in placed:
            unlinked[why] += 1
    for estimator, seen in requote_addresses.items():
        mine = addresses.setdefault(estimator, {})
        for address, n in seen.items():
            mine[address] = mine.get(address, 0) + n

    estimator_map = {
        estimator: max(seen.items(), key=lambda item: item[1])[0]
        for estimator, seen in addresses.items()
    }
    ambiguous = sorted(e for e, seen in addresses.items() if len(seen) > 1)
    agree = sum(
        1
        for m in matches
        if estimator_map.get(m.stream.taken if m.stream else m.request.winner)
        == m.order.quote_solver
    )
    emit(
        f"quote-id link: {len(matches)} of {len(orders)} orders linked, {len(requoted)} of "
        f"them to the backend's re-quote at indexing, {len(streamed)} through a stream "
        f"({streamed_early} of those before it finished) "
        f"({', '.join(f'{n} {why}' for why, n in unlinked.items() if n) or 'none missed'}); "
        f"{len(requests)} competitions, {sum(len(r.estimates) for r in requests.values())} "
        f"estimates; {len(estimator_map)} estimators carry a settlement address"
        + (f" (several for {', '.join(ambiguous)})" if ambiguous else "")
        + f"; the address of the answer taken is the order's quote solver on {agree} of "
        f"{len(matches)}"
    )
    return IdLink(
        matches=matches,
        estimator_map=estimator_map,
        stats=LinkStats(
            orders=len(orders),
            linked=len(matches),
            unlinked={why: n for why, n in unlinked.items() if n},
            requoted=len(requoted),
            streamed=len(streamed),
            streamed_early=streamed_early,
        ),
    )


def _stream_take(
    order: RecordedQuote,
    request: QuoteRequest,
    taken: str,
    forwarded: tuple[str, ...],
    arrived_at: Mapping[tuple[str, str], str],
) -> StreamTake:
    """Where in its stream an order's answer sits, and how far the stream can have got
    before the user committed: up to the next forwarded answer, which they did not take,
    or — when theirs was the last one forwarded — up to the order's creation."""
    order_of = [e.estimator for e in request.estimates]
    position = order_of.index(taken)
    later = [order_of.index(f) for f in forwarded if f in order_of]
    after = [p for p in later if p > position]
    if after:
        horizon = min(after)
    else:
        horizon = position + 1
        while horizon < len(order_of) and (
            arrived_at.get((request.request_id, order_of[horizon]), "") < order.created
        ):
            horizon += 1
    return StreamTake(
        taken=taken,
        position=position,
        horizon=horizon,
        complete=bool(request.winner),
        forwarded=forwarded,
    )


REQUOTE_AFTER_CREATION_SECONDS = (-60, 600)
"""Where the re-quote of an ethflow order lands relative to the order's creation (its
block time, whole seconds): 1–3 s after it on every case seen, bounded generously."""
REQUOTE_SIBLING_SECONDS = 5
REQUOTE_ID_SPAN = 64
"""A re-quote's estimates arrive within a second and carry quote ids from one block the
orderbook drew before quoting (1608345417–437 for one 11-estimate mainnet competition), so
siblings are request-less estimates of the same request key within both bounds of the
estimate that priced the order."""

REQUOTE_COLUMNS = (
    "quote_id, estimator, solver, lower(sell_token) as sell_, lower(buy_token) as buy_, "
    "kind, in_amount, out_amount, gas, verified, nats_seq, "
    "toString(event_timestamp) as ts"
)


def _link_requotes(
    client: ch.Client,
    chain: int,
    orders: Sequence[RecordedQuote],
    *,
    chunk: int = 300,
) -> tuple[list[MatchedQuote], dict[str, dict[str, int]]]:
    """Link orders to the competition the backend ran itself when it indexed them.

    An ethflow order is placed on chain with the UI quote's id (or `0`), and the backend,
    indexing it, re-quotes when that quote cannot be used: a full competition run
    internally, so with **no `request_id`** — invisible to
    `link_by_quote_id`'s request join — and stored on the order while `quote_id` keeps
    naming the UI's quote. Every ethflow order the id join missed in 2026-10-05..12 (25 on
    four chains) had one, its winner reproducing the stored amount to the atom 1–3 s after
    the order's block time.

    So the estimate that priced the order is found by its exact amount among request-less
    estimates of the order's request key, must be the recorded winner (the winner event
    carries the winning estimate's quote id), and its competition is the request-less
    estimates around it (`REQUOTE_SIBLING_SECONDS`, `REQUOTE_ID_SPAN`). Returns the
    matches and the settlement addresses seen, for the estimator map.
    """
    from datetime import date, datetime, timedelta

    def when(stamp: str) -> datetime:
        return datetime.fromisoformat(stamp)

    by_day: dict[str, list[RecordedQuote]] = {}
    for order in orders:
        if order.in_atoms > 0 and order.out_atoms > 0:
            by_day.setdefault(order.created[:10], []).append(order)
    matches: list[MatchedQuote] = []
    addresses: dict[str, dict[str, int]] = {}
    for day in sorted(by_day):
        first = datetime.fromisoformat(day) + timedelta(seconds=REQUOTE_AFTER_CREATION_SECONDS[0])
        last = datetime.fromisoformat(
            (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        ) + timedelta(seconds=REQUOTE_AFTER_CREATION_SECONDS[1])
        window = (
            f"event_timestamp >= '{first:%Y-%m-%d %H:%M:%S}' "
            f"and event_timestamp < '{last:%Y-%m-%d %H:%M:%S}'"
        )
        for part in chunked(by_day[day], chunk):
            keys = sorted({(o.sell_token, o.buy_token, o.kind, str(o.in_atoms)) for o in part})
            tuples = ", ".join(
                f"('0x{sell}', '0x{buy}', '{kind}', '{amount}')"
                for sell, buy, kind, amount in keys
            )
            key_filter = (
                f"chain_id = {chain} and request_id is null and {window} "
                f"and (lower(sell_token), lower(buy_token), kind, in_amount) in ({tuples})"
            )
            winning = {
                int(row["quote_id"])
                for row in client.fetch(
                    f"select quote_id from {ch.RAW_DATABASE}.winning_price_estimates "
                    f"where {key_filter} and quote_id is not null"
                )
            }
            rows = client.fetch(
                f"select {REQUOTE_COLUMNS} from {ch.RAW_DATABASE}.price_estimates "
                f"where success and quote_id is not null and {key_filter}"
            )
            by_key: dict[tuple[str, str, str, str], list[ch.Row]] = {}
            for row in rows:
                key = (
                    str(row["sell_"]).removeprefix("0x"),
                    str(row["buy_"]).removeprefix("0x"),
                    str(row["kind"]),
                    str(row["in_amount"]),
                )
                by_key.setdefault(key, []).append(row)
            for order in part:
                created = when(order.created)
                same_key = by_key.get(
                    (order.sell_token, order.buy_token, order.kind, str(order.in_atoms)), []
                )
                anchors = sorted(
                    (
                        row
                        for row in same_key
                        if str(row["out_amount"]) == str(order.out_atoms)
                        and int(row["quote_id"]) in winning
                        and REQUOTE_AFTER_CREATION_SECONDS[0]
                        <= (when(str(row["ts"])) - created).total_seconds()
                        <= REQUOTE_AFTER_CREATION_SECONDS[1]
                    ),
                    key=lambda row: abs((when(str(row["ts"])) - created).total_seconds()),
                )
                if not anchors:
                    continue
                anchor = anchors[0]
                anchor_id, anchor_at = int(anchor["quote_id"]), when(str(anchor["ts"]))
                siblings: dict[tuple[str, str], tuple[Arrival, Estimate]] = {}
                for row in same_key:
                    if (
                        abs(int(row["quote_id"]) - anchor_id) > REQUOTE_ID_SPAN
                        or abs((when(str(row["ts"])) - anchor_at).total_seconds())
                        > REQUOTE_SIBLING_SECONDS
                    ):
                        continue
                    _keep_earliest(
                        siblings,
                        str(anchor_id),
                        int(row["nats_seq"]),
                        Estimate(
                            estimator=row["estimator"],
                            out_amount=_exact(row["out_amount"]),
                            gas=_exact(row["gas"]),
                            verified=bool(row["verified"]),
                            is_winner=row["estimator"] == anchor["estimator"],
                        ),
                    )
                    if row["solver"]:
                        seen = addresses.setdefault(row["estimator"], {})
                        address = str(row["solver"]).lower().removeprefix("0x")
                        seen[address] = seen.get(address, 0) + 1
                ladder = _ladders(siblings).get(str(anchor_id), ())
                request = QuoteRequest(
                    request_id=f"requote-{anchor_id}",
                    kind=order.kind,
                    sell_token=order.sell_token,
                    buy_token=order.buy_token,
                    in_amount=order.in_atoms,
                    winner=str(anchor["estimator"]),
                    estimates=ladder,
                )
                matches.append(MatchedQuote(order=order, request=request))
    return matches, addresses


def price_suspect_orders(
    orders: Sequence[RecordedQuote],
    auction_prices: Mapping[str, float],
    traded_prices: Mapping[str, float] | None = None,
) -> frozenset[str]:
    """The **one-sided** check: orders whose two native price feeds for the sell token
    disagree past `PRICE_IMBALANCE_THRESHOLD`.

    The auction side is the order's own: `traded_prices[order_uid]`, the sell token's
    price in the auctions the order traded in (`traded_sell_prices`), where it has one,
    and the window's median `auction_prices[sell_token]` only for an order that never
    traded. A median is wrong for a token that moved during the window: on bnb
    2026-10-05..12 it excluded 238 orders of 0xfb4f81f5… and the like whose api price
    matches the per-auction one at 1.00x — the volatile token's price had tripled.
    Untraded orders weigh nothing in the bps figures, so the median only decides whether
    they count towards `no_quote` and the pick validation.

    Both feeds are scaled by 1e18 (`primitives.price_in_eth` divides by it), so the
    comparison divides them back down. A token never priced cannot be tested and is left
    alone — the guard excludes what it can prove wrong, never what it cannot check, which
    is D14's rule. Blind to a token both feeds price wrong by the same factor, which is
    what `imbalanced_orders` is for.
    """
    traded = traded_prices or {}
    suspect: set[str] = set()
    for order in orders:
        auction = traded.get(order.order_uid) or auction_prices.get(order.sell_token)
        api = order.pricing.sell_token_price
        if not auction or api <= 0:
            continue
        ratio = (auction / ONE_ETH) / api
        if ratio > PRICE_IMBALANCE_THRESHOLD or ratio < 1 / PRICE_IMBALANCE_THRESHOLD:
            suspect.add(order.order_uid)
    return frozenset(suspect)


@dataclass(frozen=True)
class OrderTrade:
    """One trade of a window order, with the auction whose settlement carried it."""

    order_uid: str
    auction_id: int
    sell_amount: int
    buy_amount: int


# A trade belongs to the first settlement after it in its block: the `Trade` events of a
# settlement precede its `Settlement` event, which is the only one carrying the auction
# id. That is the rule `int_backend_data__trade_with_tx_hash` applies; it is applied here
# directly so the result does not depend on the run windows of that incremental model.
# The lateral probe rides the `(block_number, log_index)` index on settlements: 0.4-0.7 s
# for a bnb week.
ORDER_TRADES_SQL = """
with window_trades as (
    select t.order_uid, t.block_number, t.log_index, t.sell_amount, t.buy_amount
    from dbt.stg_backend_data__trades t
    join dbt.stg_backend_data__orders o on o.uid = t.order_uid
    where o.creation_timestamp >= %(start)s and o.creation_timestamp < %(end)s
)
select encode(wt.order_uid, 'hex') as order_uid, s.auction_id,
       wt.sell_amount, wt.buy_amount
from window_trades wt
cross join lateral (
    select s.auction_id
    from dbt.stg_backend_data__settlements s
    where s.block_number = wt.block_number and s.log_index > wt.log_index
    order by s.log_index
    limit 1
) s
"""

# Native prices of exactly the (auction, token) pairs the trades need, through the same
# `{auction_prices}` relation the solve side reads (`extract.fetch_prices`).
TRADE_PRICES_SQL = """
select ap.auction_id, encode(ap.token, 'hex') as token, ap.price
from {auction_prices} ap
join unnest(%(pair_auctions)s::bigint[], %(pair_tokens)s::bytea[]) as w (auction_id, token)
  on w.auction_id = ap.auction_id and w.token = ap.token
"""


def load_order_trades(conn: Connection, start: str, end: str) -> list[OrderTrade]:
    """Every trade of the orders created in `[start, end)`, each with its auction —
    including trades after `end`, which `executed_sell` counts too. A trade whose
    settlement is not recorded yet has no auction and is left out: it cannot be priced."""
    return [
        OrderTrade(
            order_uid=row["order_uid"],
            auction_id=row["auction_id"],
            sell_amount=as_int(row["sell_amount"]),
            buy_amount=as_int(row["buy_amount"]),
        )
        for row in fetch(conn, ORDER_TRADES_SQL, {"start": start, "end": end})
    ]


def _priced_token(token: str, weth: str) -> str:
    """The token whose price an auction lists for `token`: the native sentinel is priced
    as its wrapped ERC20, as the solve side prices it (`primitives.as_erc20`)."""
    return as_erc20(token, weth)


def load_trade_prices(
    conn: Connection,
    orders: Sequence[RecordedQuote],
    trades: Sequence[OrderTrade],
    *,
    weth: str,
    chunk: int = 2000,
) -> dict[tuple[int, str], int]:
    """`(auction_id, token) -> native price` for both tokens of every trade, in the auction
    that trade settled in. Absent pairs stay absent."""
    tokens = {
        o.order_uid: (_priced_token(o.sell_token, weth), _priced_token(o.buy_token, weth))
        for o in orders
    }
    pairs = sorted(
        {
            (trade.auction_id, token)
            for trade in trades
            if trade.order_uid in tokens
            for token in tokens[trade.order_uid]
        }
    )
    prices: dict[tuple[int, str], int] = {}
    auction_ids = sorted({auction_id for auction_id, _ in pairs})
    if not auction_ids:
        return prices
    for part in chunked(auction_ids, chunk):
        ids = set(part)
        wanted = [(a, t) for a, t in pairs if a in ids]
        for row in fetch_prices(
            conn,
            TRADE_PRICES_SQL,
            {
                "ids": list(part),
                "pair_auctions": [a for a, _ in wanted],
                "pair_tokens": [bytes.fromhex(t) for _, t in wanted],
            },
        ):
            if row["price"]:
                prices[(row["auction_id"], row["token"])] = as_int(row["price"])
    return prices


def imbalanced_orders(
    orders: Sequence[RecordedQuote],
    trades: Iterable[OrderTrade],
    prices: Mapping[tuple[int, str], int],
    *,
    weth: str,
) -> frozenset[str]:
    """The **two-sided** check: orders whose executed sell value and executed buy value
    disagree past `PRICE_IMBALANCE_THRESHOLD`, each trade valued at the native prices of
    the auction it settled in — `counterfactual.price_imbalanced`, per order rather than
    per solution.

    Per auction and not at window medians, because a median two-sided check excludes a
    volatile token's honest trades: USDT→0xfb4f81f5… failed at bnb's week medians on 225
    orders whose per-auction values match the trade-implied rate at 1.00x. A trade
    missing either price is skipped, and an order with nothing checkable is left alone,
    as the solve side leaves a solution — D14's rule. Blind, like that check, to a pair
    whose two tokens are both mispriced by the same factor.
    """
    tokens = {
        o.order_uid: (_priced_token(o.sell_token, weth), _priced_token(o.buy_token, weth))
        for o in orders
    }
    values: dict[str, list[int]] = {}
    for trade in trades:
        pair = tokens.get(trade.order_uid)
        if pair is None:
            continue
        sell_price = prices.get((trade.auction_id, pair[0]))
        buy_price = prices.get((trade.auction_id, pair[1]))
        if sell_price is None or buy_price is None:
            continue
        value = values.setdefault(trade.order_uid, [0, 0])
        value[0] += price_in_eth(sell_price, trade.sell_amount)
        value[1] += price_in_eth(buy_price, trade.buy_amount)
    return frozenset(
        uid
        for uid, (sold, bought) in values.items()
        if sold
        and bought
        and (
            sold > PRICE_IMBALANCE_THRESHOLD * bought
            or bought > PRICE_IMBALANCE_THRESHOLD * sold
        )
    )


def traded_sell_prices(
    orders: Sequence[RecordedQuote],
    trades: Iterable[OrderTrade],
    prices: Mapping[tuple[int, str], int],
    *,
    weth: str,
) -> dict[str, float]:
    """`order_uid -> the sell token's auction price where the order traded`, weighted by
    each trade's sell amount and scaled by 1e18 like the feed — the one-sided check's
    reference for every order that traded (`price_suspect_orders`)."""
    sell_token = {o.order_uid: _priced_token(o.sell_token, weth) for o in orders}
    sums: dict[str, list[int]] = {}
    for trade in trades:
        token = sell_token.get(trade.order_uid)
        price = prices.get((trade.auction_id, token)) if token else None
        if price is None or trade.sell_amount <= 0:
            continue
        entry = sums.setdefault(trade.order_uid, [0, 0])
        entry[0] += price * trade.sell_amount
        entry[1] += trade.sell_amount
    return {uid: value / amount for uid, (value, amount) in sums.items()}


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
        rows = fetch_prices(
            conn, TOKEN_PRICES_SQL, {"ids": list(auction_ids), "tokens": list(part)}
        )
        for row in rows:
            if row["price"]:
                prices[row["token"]] = float(row["price"])
    return prices


# --- the counterfactual -------------------------------------------------------------

Leaf = Literal[
    "unaffected", "no_quote", "class_flip", "degraded", "unpriced", "nonpositive",
    "price_suspect", "implausible", "unrecorded",
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
    streamed: bool = False
    commit_dependent: bool = False
    """The order was placed from a streamed quote, and whether its replacement would
    differ had the user committed the moment their answer was forwarded rather than at
    the latest moment the record allows (`stream_replacement`)."""

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

    An order placed from a streamed quote (`match.stream`) is decided by the answer it
    took rather than the recorded winner, and its replacement is what the stream would
    have shown by the user's commit (`stream_replacement`). A replacement the record
    cannot tell — the stream was dropped before the answer the user would have waited
    for — is the `unrecorded` leaf.
    """
    order, request, take = match.order, match.request, match.stream
    pricing = order.pricing
    if ranking == "autopilot" and order.is_sell:
        price = (buy_token_prices or {}).get(order.buy_token)
        pricing = Pricing(pricing.gas_price, pricing.sell_token_price, price)

    base_effective = _recorded_effective(order)
    # The template every other leaf is a `replace` of, so the fields a leaf does not
    # change are stated once.
    # Who priced the order: the recorded winner, or the streamed answer it took.
    taken = take.taken if take else request.winner
    unaffected = QuoteDiff(
        order_uid=order.order_uid,
        leaf="unaffected",
        is_sell=order.is_sell,
        winner=taken,
        replacement=None,
        effective_base=base_effective,
        effective_loo=base_effective,
        volume_native=order.volume_native,
        partially_fillable=order.partially_fillable,
        quote_loo=None,
        streamed=take is not None,
    )
    if taken and taken not in removed_estimators:
        # Removing a loser cannot move the winner, and cannot change the winner's
        # verified class either: the class is a property of the estimates that remain,
        # and the winner is in the preferred one by definition. On a stream the same
        # holds for the answer taken: nothing that arrived after it before the commit
        # beat it, and a removal only takes candidates away.
        return unaffected
    if not any(e.estimator in removed_estimators for e in request.reasonable):
        return unaffected
    if order.order_uid in price_suspects:
        return replace(unaffected, leaf="price_suspect", volume_native=0)

    commit_dependent = False
    try:
        if take is None:
            replacement = leave_one_out(
                request, removed_estimators, pricing=pricing, ranking=ranking
            )
        else:
            replacement, commit_dependent = stream_replacement(
                request, take, removed_estimators, pricing=pricing, ranking=ranking
            )
    except UnpricedRequestError:
        return replace(unaffected, leaf="unpriced")
    except UnrecordedStreamError:
        return replace(unaffected, leaf="unrecorded")
    unaffected = replace(unaffected, commit_dependent=commit_dependent)

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
    priced_by = next(
        (e for e in request.reasonable if e.estimator == taken), None
    ) if take else request.recorded_winner
    flip = bool(priced_by and priced_by.verified and not replacement.verified)
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

    **Class flips are outside the bps figures.** Removing the only *verified* estimator
    leaves a quote that may read better in bps while being worse in kind. One such flip
    supplied the whole conditional figure of a one-day test (+70 bps on 5 ETH against 81
    genuinely degraded orders), which is what a count is for and a basis-point average
    is not.
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
    quote_wins: int = 0
    no_quote: int = 0
    class_flips: int = 0
    degraded: int = 0
    unpriced: int = 0
    nonpositive: int = 0
    price_suspect: int = 0
    implausible: int = 0
    unrecorded: int = 0
    """Wins on a stream the client dropped before the answer its user would have waited
    for arrived — cancelled, so never recorded (`stream_replacement`)."""
    stream_wins: int = 0
    commit_dependent: int = 0
    """Wins on orders placed from a streamed quote, and how many of those would have a
    different replacement had the user committed the moment their answer was forwarded
    rather than at the latest moment the record allows — the one assumption the stream
    rule makes, counted."""
    """Wins — orders whose quote the removed solver did win — excluded from every figure
    because the gas cost swallowed the input, because the order's two native price feeds
    disagree (`PRICE_IMBALANCE_THRESHOLD`), or because the replacement quote is more than
    `IMPLAUSIBLE_QUOTE_FACTOR` off the recorded one (a broken estimate promoted to
    winner). None of these leaves is reached for an order the removed solver merely
    lost, so they are subsets of `quote_wins`; the implausible volume is kept so the
    excluded slice can be sized."""
    delta_volume_native: float = 0.0
    """Σ (Δbps / 10,000 x order volume) over degraded orders — the numerator of
    `window_bps`, so the window figure is one division."""
    degraded_volume_native: int = 0
    """Volume of the orders that degraded, the denominator of the conditional figure."""
    diffs: list[QuoteDiff] = field(default_factory=list[QuoteDiff])

    def add(self, diff: QuoteDiff) -> None:
        self.matched += 1
        if diff.leaf == "unaffected":
            return
        self.quote_wins += 1
        self.stream_wins += diff.streamed
        self.commit_dependent += diff.commit_dependent
        self.diffs.append(diff)
        if diff.leaf == "unrecorded":
            self.unrecorded += 1
            return
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
            return
        if diff.leaf == "no_quote":
            self.no_quote += 1
            return
        if diff.leaf == "class_flip":
            self.class_flips += 1
            return
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
        order whose quote moved.

        Class flips are included: the fee is charged against whatever quote the order
        would have carried, and an unverified replacement still priced the order. Only
        the *bps* figures leave them out, where a signed average would mislead.
        """
        return {
            d.order_uid: d.quote_loo
            for d in self.diffs
            if d.quote_loo is not None
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
    """Requests where another estimate shares the recorded winner's key to the last float
    bit, so arrival order decided rather than the rule: prod sorts the answers stably in
    completion order (`quote.rs:80`), and the ladder is loaded in that order
    (`nats_seq`). Every sell estimate under water ties at zero (`rank_key`), so dust requests are ties
    too."""
    tie_mismatches: int = 0
    """Ties our arrival order resolved to another estimator — the rule picked an
    equivalent estimate, so these are not counted as disagreements; on the gold layer two
    answers in the same millisecond are the expected cause."""
    price_suspect: int = 0
    """Matched orders left out because their recorded `sell_token_price` failed the
    cross-check: the gas weight the ranking needs is the very number that is wrong."""
    disagreements: list[tuple[str, str, str]] = field(
        default_factory=list[tuple[str, str, str]]
    )
    """`(request_id, recorded, ours)` for the first few, so a failure can be opened."""
    streams: int = 0
    streams_reproduced: int = 0
    """Streamed competitions with two or more forwarded answers, and how many of them
    `replay_stream` reproduces exactly — the same answers forwarded, in the same order,
    as their `quote_computed` rows record. The stream rule rests on this as the pick
    rests on `rate`."""
    stream_disagreements: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = field(
        default_factory=list[tuple[str, tuple[str, ...], tuple[str, ...]]]
    )
    """`(request_id, recorded, replayed)` for the first few."""

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
    """Re-pick every matched request and compare against the recorded winner, and replay
    every streamed competition against the answers it recorded forwarding."""
    result = PickValidation(ranking=ranking)
    replayed: set[str] = set()
    for match in matches:
        order, request = match.order, match.request
        if order.order_uid in price_suspects:
            result.price_suspect += 1
            continue
        take = match.stream
        if (
            ranking == "effective-sell"
            and take is not None
            and len(take.forwarded) >= 2
            and request.request_id not in replayed
        ):
            replayed.add(request.request_id)
            ours = replay_stream(
                request.estimates, is_sell=request.is_sell,
                in_amount=request.in_amount, pricing=order.pricing,
            )
            result.streams += 1
            # A dropped stream recorded only what it forwarded before the drop.
            if tuple(ours[: len(take.forwarded)]) == take.forwarded and (
                not take.complete or len(ours) == len(take.forwarded)
            ):
                result.streams_reproduced += 1
            elif len(result.stream_disagreements) < keep_disagreements:
                result.stream_disagreements.append(
                    (request.request_id, take.forwarded, tuple(ours))
                )
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
        # `pick` returned without raising, so every usable estimate has a key.
        keys = {
            e.estimator: rank_key(
                e,
                is_sell=request.is_sell,
                in_amount=request.in_amount,
                pricing=pricing,
                ranking=ranking,
            )
            for e in usable
        }
        winner_key = keys[winner.estimator]
        preferred = [e for e in usable if e.verified] if winner.verified else usable
        tie = any(
            e.estimator != winner.estimator and keys[e.estimator] == winner_key
            for e in preferred
        )
        result.tied += tie
        if ours.estimator == winner.estimator:
            result.reproduced += 1
        elif tie and ours.verified == winner.verified and keys[ours.estimator] == winner_key:
            result.tie_mismatches += 1
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
    the link are scenario-independent, so N solvers cost one load.
    """

    network: str
    start: str
    end: str
    ranking: QuoteRanking
    orders: list[RecordedQuote]
    matches: list[MatchedQuote]
    estimator_map: dict[str, str]
    """`estimator -> solver address` on this chain, read from the linked estimates."""
    buy_token_prices: dict[str, float]
    validation: PickValidation
    link: LinkStats
    """How the quote-id link fared on the window's non-trivial orders."""
    price_suspects: frozenset[str] = frozenset()
    """Orders failing either price check (`PRICE_IMBALANCE_THRESHOLD`). Out of every
    quote figure, on both sides of every ratio, and counted."""
    one_sided_suspects: int = 0
    two_sided_suspects: int = 0
    """How many orders each check flagged. An order can fail both, so the two can sum
    past `price_suspects`."""
    orders_dropped: int = 0

    @property
    def trivial_orders(self) -> int:
        """Orders no competition priced (`RecordedQuote.trivial`): in the window's
        population, not linked."""
        return sum(1 for o in self.orders if o.trivial)

    @property
    def clean_orders(self) -> list[RecordedQuote]:
        return [o for o in self.orders if o.order_uid not in self.price_suspects]

    @property
    def window_volume_native(self) -> int:
        return sum(o.volume_native for o in self.clean_orders)

    @property
    def window_quoted_volume_native(self) -> int:
        return sum(o.quoted_volume_native for o in self.clean_orders)

    @property
    def suspect_volume_native(self) -> int:
        """Executed volume, at the api's price, that the price checks took out of the
        denominator — the size of what they caught."""
        return sum(
            o.volume_native for o in self.orders if o.order_uid in self.price_suspects
        )

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

    Orders are linked to their competitions by quote id (`link_by_quote_id`), trivial
    unwraps not at all. Both price checks then run over every order, and the pick is
    validated on what was linked.

    Raises `NoQuoteDataError` for a network with no quote data: reporting empty quote
    columns would read as "this solver quoted nothing" rather than "there is no quote
    data here" (Q-D10).
    """
    emit: Callable[[str], None] = log or (lambda message: None)
    ch.chain_id(network)  # refuse an unsupported network before any query runs

    orders, dropped = load_recorded_quotes(conn, start, end, network=network)
    linkable = [o for o in orders if not o.trivial]
    emit(
        f"{len(orders)} orders created in [{start}, {end}) carry a recorded quote "
        f"({dropped} dropped as unevaluable); {len(orders) - len(linkable)} trivial "
        f"unwraps (quote solver 0x0) had no competition and are not linked"
    )
    id_link = link_by_quote_id(client, network, linkable, log=log)
    matches = id_link.matches
    estimator_map = id_link.estimator_map

    # Both checks run on every ranking: they are data guards, not ranking inputs. The
    # one-sided check needs the window's median per sell token, and the autopilot rule
    # the buy tokens' too; the two-sided one prices each trade in its own auction.
    guard_tokens = sorted({o.sell_token for o in orders})
    wanted = guard_tokens + (
        sorted({m.order.buy_token for m in matches}) if ranking == "autopilot" else []
    )
    auction_prices = load_token_prices(conn, auction_ids, wanted)
    # The auction feed is scaled by 1e18; `Pricing` holds wei per atom, as the api's
    # `sell_token_price` does.
    buy_token_prices = (
        {token: price / ONE_ETH for token, price in auction_prices.items()}
        if ranking == "autopilot"
        else {}
    )
    weth = wrapped_native_token(network)
    trades = load_order_trades(conn, start, end)
    trade_prices = load_trade_prices(conn, orders, trades, weth=weth)
    one_sided = price_suspect_orders(
        orders,
        auction_prices,
        traded_sell_prices(orders, trades, trade_prices, weth=weth),
    )
    two_sided = imbalanced_orders(orders, trades, trade_prices, weth=weth)
    suspects = one_sided | two_sided
    unpriced_tokens = sum(1 for token in guard_tokens if token not in auction_prices)
    traded = {o.order_uid for o in orders if o.traded}
    checked = {
        t.order_uid
        for t in trades
        if t.order_uid in traded
    }
    emit(
        f"price checks: {len(suspects)} of {len(orders)} orders excluded — "
        f"{len(one_sided)} one-sided (the api's sell_token_price against the auction "
        f"price where the order traded, the window median where it did not; "
        f"{unpriced_tokens} of {len(guard_tokens)} sell tokens never in a median), "
        f"{len(two_sided)} two-sided (sold against bought value at each trade's auction "
        f"prices, over {len(trades)} trades of {len(checked)} of {len(traded)} traded "
        f"orders), both past {PRICE_IMBALANCE_THRESHOLD:g}x; "
        f"{sum(o.volume_native for o in orders if o.order_uid in suspects)} of "
        f"{sum(o.volume_native for o in orders)} wei of executed volume removed"
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
        + f", {validation.tied} decided by arrival order, {validation.tie_mismatches} "
        f"ties resolved to an equivalent estimate; verified-class rule tested on "
        f"{validation.mixed_class} requests, correct on {validation.mixed_class_correct}; "
        f"{validation.price_suspect} price-suspect matches left out of the check"
    )
    return QuoteWindow(
        network=network,
        start=start,
        end=end,
        ranking=ranking,
        orders=orders,
        matches=matches,
        estimator_map=estimator_map,
        buy_token_prices=buy_token_prices,
        validation=validation,
        link=id_link.stats,
        price_suspects=suspects,
        one_sided_suspects=len(one_sided),
        two_sided_suspects=len(two_sided),
        orders_dropped=dropped,
    )
