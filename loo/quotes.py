"""The quote competition: reproduce the pick, re-run it without a solver, and hand the
counterfactual quote to the fee path (`fees.py`).

The pick, `services/crates/price-estimation/src/competition/quote.rs` (since 2026-09-02):

```
is_reasonable          drop any estimate with gas == 0 or out_amount == 0
compare_quote_result   a verified estimate beats an unverified one at any amount
compare_quote          otherwise rank on the effective out amount:
effective_out_amount   gas cost in the sell token = gas × gas_price / native(sell)
                       sell: out − cost × out/in, maximise;  buy: out + cost, minimise
                       clamped at zero, truncated to an integer
results.sort_by        stable, over the answers in arrival order — a tie goes first-come
```

The baseline quote and its fee always come from Postgres; the ClickHouse ladder supplies
only the counterfactual.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import TYPE_CHECKING, Literal

from . import ch
from .db import Connection, as_int, chunked, fetch, prefetch
from .extract import price_query
from .fees import FeeError, Quote, quote_fee
from .primitives import ONE_ETH, canonical_token, price_in_eth, wrapped_native_token

if TYPE_CHECKING:  # only `shifted_limit` needs the type; `valuation` imports `fees` too
    from .valuation import Order

QuoteRanking = Literal["effective-sell", "autopilot"]

RANKING: dict[QuoteRanking, str] = {
    "effective-sell": (
        "the effective quote netted in the sell token, at the order's recorded "
        "gas_price / sell_token_price, truncated to whole atoms — the protocol's own rule "
        "since 2026-09-02, the same quantity the price-improvement "
        "fee is charged against, and reproducible from the record alone"
    ),
    "autopilot": (
        "the protocol's rule before 2026-09-02: out_amount valued in wei at the out "
        "token's native price less gas x gas_price, with the buy token's native price "
        "taken from the window's auction prices for sell orders"
    ),
}

PRICE_IMBALANCE_THRESHOLD = 2.0
"""Disagreement factor past which `price_suspect_orders` or `imbalanced_orders` excludes
an order ([docs](../docs/quotes.md#two-price-checks-both-at-the-orders-own-auctions))."""


TRIVIAL_QUOTE_SOLVER = "00" * 20
"""`order_quotes.solver` of a wrapped-native unwrap: no competition, so not linked."""

PRE_CREATION_WINDOW_SECONDS = 30 * 60
POST_CREATION_WINDOW_SECONDS = 90
"""Estimate search window around order creation; the indexing re-quote lands just after."""


# --- the ladder ---------------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    estimator: str
    out_amount: float
    gas: float
    verified: bool
    is_winner: bool = False

    @property
    def reasonable(self) -> bool:
        """`is_reasonable` (`competition/quote.rs:191`)."""
        return self.out_amount > 0 and self.gas > 0


@dataclass(frozen=True)
class Pricing:
    """Ranking scalars, all wei per atom. `buy_token_price` is used only by `autopilot`
    on sell orders; `None` means unpriced."""

    gas_price: float
    sell_token_price: float
    buy_token_price: float | None = None

    def gas_cost_in_sell_atoms(self, gas: float) -> float:
        return gas * self.gas_price / self.sell_token_price


@dataclass(frozen=True)
class QuoteRequest:
    request_id: str
    kind: str
    sell_token: str
    buy_token: str
    in_amount: float
    winner: str
    """Empty when no winner was recorded."""
    estimates: tuple[Estimate, ...] = ()
    """In arrival order (`nats_seq`): prod breaks ties first-come (`quote.rs:80`)."""

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
    """The quote netted for its own gas in the sell token, on the out side — what
    `fees._adjust_quote_to_order_limits` charges against."""
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
    """Comparison key, larger is better for both kinds. Prod's arithmetic in prod's order,
    including the zero clamp and the truncation (`U256::saturating_from(v.trunc())`):
    both create ties that arrival order decides."""
    if ranking == "effective-sell":
        # `RankingContext::effective_out_amount`.
        cost = pricing.gas_cost_in_sell_atoms(estimate.gas)
        if is_sell:
            value = estimate.out_amount - cost * (estimate.out_amount / in_amount)
            return float(math.trunc(value)) if value > 0 else 0.0
        return -float(math.trunc(estimate.out_amount + cost))
    if ranking == "autopilot":
        # `effective_eth_out` before 2026-09-02, truncated in wei.
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
    """Verified first (`QuoteVerificationMode::Prefer`), then `rank_key`; ties keep the
    first in the given order. `None` if nothing is usable."""
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
    """Winner with `removed` absent; `None` means no quote, so no order."""
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
    """Estimators a stream forwards, in order: each answer beating the best so far
    (`estimate_stream`), less those dropped as `SellAmountDoesNotCoverFee`, which still
    set the bar. `estimates` must be in arrival order."""
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
    """Whether the quoter turns a forwarded answer into a quote (sell orders only)."""
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
    """The replacement a streamed order's user would have taken with `removed` absent:
    the best remaining answer by `take.horizon`, else the next to arrive. Also returns
    whether committing at `take.position` instead would change it."""
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
    """The `fees.Quote` an estimate would have put on the order. Rounded, not floored,
    so fee deltas are not biased."""
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

# The window's orders by creation, then their quotes a chunk at a time: staging has no
# index on `creation_timestamp`, so the window costs one scan of the orders table, and the
# per-order lookups after it run in parallel.
ORDER_UIDS_SQL = """
select encode(uid, 'hex') as order_uid
from dbt.stg_backend_data__orders
where creation_timestamp >= %(start)s and creation_timestamp < %(end)s
order by creation_timestamp, uid
"""

# Staging's `quote_id` is NULL only on rows built before ~2026-10-06, before any window
# `loo` replays, and equals raw's since (0 of 584k orders differ on mainnet, base,
# arbitrum-one and bnb, 2026-10-28..11-26). Raw's copy would cost a cold index read per
# order (1.7 ms on mainnet): the most of a first run's quote side.
ORDERS_SQL = """
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
    oq.quote_id
from dbt.stg_backend_data__orders o
join dbt.stg_backend_data__order_quotes oq on oq.order_uid = o.uid
left join dbt.stg_backend_data__app_data ad on ad.contract_app_data = o.app_data
where o.uid = any(%(uids)s)
"""

ORDERS_CHUNK = 500
"""Orders per `ORDERS_SQL` query: small, as each order is a few index lookups, at first
touch each a read from disk, and the chunks run in parallel."""

# Window median per token: a cross-check and `autopilot` input, never a valuation. Each
# query counts the prices of a chunk of auctions, and `load_token_prices` takes the median
# over the chunks: prices repeat from auction to auction (1.76M prices of a mainnet day
# held 44.6k distinct values, 2026-11-26), so the counts are small and the chunks run in
# parallel, where one median over the window ran alone for minutes.
TOKEN_PRICES_SQL = """
select encode(ap.token, 'hex') as token, ap.price, count(*) as n
from {auction_prices} ap
where ap.token = any(%(tokens)s)
  and ap.price is not null
group by ap.token, ap.price
"""

TOKEN_PRICES_CHUNK = 5000
"""Auctions per `TOKEN_PRICES_SQL` query."""


@dataclass(frozen=True)
class RecordedQuote:
    """An order created in the window with its recorded quote, exact from Postgres."""

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
    executed_sell: int = 0
    quote_id: int | None = None
    """`None`, or `0` for an ethflow order placed without one: nothing to join on."""

    @property
    def is_sell(self) -> bool:
        return self.kind == "sell"

    @property
    def trivial(self) -> bool:
        """No competition decided this quote (`TRIVIAL_QUOTE_SOLVER`)."""
        return self.quote_solver == TRIVIAL_QUOTE_SOLVER

    @property
    def in_atoms(self) -> int:
        """The request's in amount: sell side of a sell order, buy side of a buy one."""
        return self.quote.sell_amount if self.is_sell else self.quote.buy_amount

    @property
    def out_atoms(self) -> int:
        """What the linked estimate must equal to the atom."""
        return self.quote.buy_amount if self.is_sell else self.quote.sell_amount

    @property
    def in_amount(self) -> float:
        return float(self.quote.sell_amount if self.is_sell else self.quote.buy_amount)

    @property
    def out_amount(self) -> float:
        return float(self.quote.buy_amount if self.is_sell else self.quote.sell_amount)

    @property
    def volume_native(self) -> int:
        """Executed (not quoted: untraded probe orders would dominate) sell-side value
        in native wei at the quote's sell price — the weight of every quote figure."""
        return round(self.executed_sell * self.pricing.sell_token_price)

    @property
    def quoted_volume_native(self) -> int:
        return round(self.quote.sell_amount * self.pricing.sell_token_price)

    @property
    def traded(self) -> bool:
        return self.executed_sell > 0


def window_queries(start: str, end: str) -> list[tuple[str, dict[str, str]]]:
    """The quote side's first reads, which need only the window, for `db.prefetch`."""
    return [order_trades_query(start, end), order_uid_query(start, end)]


def order_uid_query(start: str, end: str) -> tuple[str, dict[str, str]]:
    return ORDER_UIDS_SQL, {"start": start, "end": end}


def load_recorded_quotes(
    conn: Connection, start: str, end: str, *, executed: Mapping[str, int]
) -> tuple[list[RecordedQuote], int]:
    """Orders created in `[start, end)` with a recorded quote, in creation order, and how
    many were dropped as unevaluable. `executed` is each order's executed sell amount
    over all its trades (`load_order_trades`)."""
    orders: list[RecordedQuote] = []
    dropped = 0
    created = [row["order_uid"] for row in fetch(conn, *order_uid_query(start, end))]
    queries = [
        (ORDERS_SQL, {"uids": [bytes.fromhex(uid) for uid in part]})
        for part in chunked(created, ORDERS_CHUNK)
    ]
    prefetch(conn, queries, first=True)
    by_uid = {row["order_uid"]: row for query in queries for row in fetch(conn, *query)}
    for row in (by_uid[uid] for uid in created if uid in by_uid):
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
                executed_sell=executed.get(row["order_uid"], 0),
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
    from datetime import datetime, timedelta

    first = datetime.fromisoformat(start) - timedelta(
        seconds=PRE_CREATION_WINDOW_SECONDS
    )
    last = datetime.fromisoformat(end) + timedelta(seconds=POST_CREATION_WINDOW_SECONDS)
    return first.strftime("%Y-%m-%d %H:%M:%S"), last.strftime("%Y-%m-%d %H:%M:%S")


Arrival = int
"""An estimate's `nats_seq`."""


def _keep_earliest(
    rows: dict[tuple[str, str], tuple[Arrival, Estimate]],
    request_id: str,
    arrival: Arrival,
    estimate: Estimate,
) -> None:
    """Keep the earliest per `(request_id, estimator)`: a `ReplacingMergeTree` can hold
    duplicates until it merges."""
    key = (request_id, estimate.estimator)
    if key not in rows or arrival < rows[key][0]:
        rows[key] = (arrival, estimate)


def _ladders(
    rows: Mapping[tuple[str, str], tuple[Arrival, Estimate]],
) -> dict[str, tuple[Estimate, ...]]:
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
    order: RecordedQuote
    request: QuoteRequest
    """Its linked estimate reproduces the order's stored amount to the atom."""
    stream: StreamTake | None = None
    """Set for a streamed quote: the counterfactual follows the stream."""


@dataclass(frozen=True)
class StreamTake:
    """Which forwarded answer of a streamed competition an order took.
    The commit is unrecorded, so it is placed as late as the record allows (`horizon`)
    ([docs](../docs/quotes.md#streamed-quotes-the-order-takes-what-the-stream-showed-not-the-winner)).
    """

    taken: str
    position: int
    """Index in the arrival-ordered ladder."""
    horizon: int
    """Answers `[0, horizon)` may have arrived before the commit."""
    complete: bool
    """A winner was recorded, so every answer the stream received is in the ladder."""
    forwarded: tuple[str, ...] = ()
    """Estimators of the request's `quote_computed` rows, in order."""


# --- the quote-id link --------------------------------------------------------------


@dataclass(frozen=True)
class LinkStats:
    orders: int
    linked: int
    unlinked: Mapping[str, int] = field(default_factory=dict[str, int])
    requoted: int = 0
    """Of `linked`, through `_link_requotes`."""
    streamed: int = 0
    streamed_early: int = 0
    """Of `streamed`, those whose answer is not the stream's recorded final best."""

    @property
    def rate(self) -> float | None:
        return self.linked / self.orders if self.orders else None


@dataclass(frozen=True)
class IdLink:
    matches: list[MatchedQuote]
    estimator_map: dict[str, str]
    """`estimator -> settlement address`, read from `price_estimates.solver`."""
    stats: LinkStats


ID_LADDER_COLUMNS = (
    "request_id, estimator, solver, quote_id, kind, lower(sell_token) as sell_, "
    "lower(buy_token) as buy_, in_amount, out_amount, gas, verified, nats_seq, "
    "toString(event_timestamp) as ts"
)


def _exact(value: object) -> float:
    """A raw-layer decimal string as an exact `int` (typed `float`, as `Estimate` is)."""
    text = str(value)
    try:
        return int(text)
    except ValueError:
        return float(text)


LINK_CHUNK = 100_000
"""Quote ids per unit: a day's in one."""

UNIT_IDS_PER_QUERY = 40_000
"""Quote ids per link query, about four mainnet days: each query reads the table's parts
its days fall in once (a part holds about a week), so fewer queries read less. Bounded
by the response, ~90 MB of JSON at this size; the request body is ~440 kB."""

UNIT_KEYS_PER_QUERY = 5_000
"""Request keys per re-quote query, ~650 kB of text: the proxy in front of ClickHouse
refuses a request body much over 1 MB (`413 Request Entity Too Large`)."""


@dataclass(frozen=True)
class _Unit:
    """One day (and chunk) of a link: the per-day query's time window and the rest of its
    condition. A unit-tagged query reads several units in one pass, and the rows of a unit
    are exactly what that unit's own query would return."""

    window: str
    condition: str
    size: int


def _unit_groups(units: Sequence[_Unit], budget: int) -> list[list[tuple[int, _Unit]]]:
    """Consecutive units, numbered from 1, in groups of at most `budget` (or one unit)."""
    groups: list[list[tuple[int, _Unit]]] = []
    total = 0
    for number, unit in enumerate(units, 1):
        if not groups or total + unit.size > budget:
            groups.append([])
            total = 0
        groups[-1].append((number, unit))
        total += unit.size
    return groups


def _tagged(group: Sequence[tuple[int, _Unit]]) -> tuple[str, str]:
    """`units`, the numbers of the units a row meets, as a select column, and the windows
    as a prefilter: it lets ClickHouse skip the parts outside them."""
    tags = ", ".join(
        f"if({unit.window} and {unit.condition}, {number}, 0)" for number, unit in group
    )
    windows = " or ".join(f"({unit.window})" for _, unit in group)
    return f"arrayFilter(k -> k > 0, [{tags}]) as units", f"({windows})"


def _by_unit(rows: Iterable[ch.Row]) -> dict[int, list[ch.Row]]:
    """Rows by unit; a row in two units' windows is in both, as with a query per unit."""
    grouped: dict[int, list[ch.Row]] = {}
    for row in rows:
        for number in row["units"]:
            grouped.setdefault(int(number), []).append(row)
    return grouped


def link_by_quote_id(
    client: ch.Client,
    network: str,
    orders: Sequence[RecordedQuote],
    *,
    chunk: int = LINK_CHUNK,
    log: Callable[[str], None] | None = None,
) -> IdLink:
    """Link each order to its competition by quote id, through the raw layer only (the
    gold tables miss competitions). The linked estimate must reproduce the order's stored
    amount to the atom. Ethflow re-quotes fall to `_link_requotes`; an order from a stream
    gets a `StreamTake`. The day bound keeps an id from matching another day's estimate."""
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
    # A unit per creation day (and chunk of its quote ids): the requests its ids name.
    units: list[_Unit] = []
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
            units.append(_Unit(window, f"request_id in ({linked})", len(part)))
    # (winners, forwarded answers, ladder) per group of units, read ahead with the
    # re-quotes of the orders placed without an id, which need no link first.
    queries: list[tuple[str, str, str]] = []
    for group in _unit_groups(units, UNIT_IDS_PER_QUERY):
        tag, windows = _tagged(group)
        queries.append(
            (
                f"select request_id, estimator, {tag} "
                f"from {ch.RAW_DATABASE}.winning_price_estimates "
                f"where chain_id = {chain} and {windows} and notEmpty(units)",
                # One `quote_computed` row per forwarded answer.
                f"select request_id, quote_id, nats_seq, {tag} "
                f"from {ch.RAW_DATABASE}.quote_computed "
                f"where chain_id = {chain} and {windows} and quote_id is not null "
                f"and notEmpty(units)",
                f"select {ID_LADDER_COLUMNS}, {tag} from {ch.RAW_DATABASE}.price_estimates "
                f"where chain_id = {chain} and success and {windows} and notEmpty(units)",
            )
        )
    without_id = [o for o in orders if not o.quote_id]
    client.prefetch(
        [*(sql for group in queries for sql in group), *_requote_queries(chain, without_id)]
    )
    winners_of: dict[int, list[ch.Row]] = {}
    computed_of: dict[int, list[ch.Row]] = {}
    ladder_of: dict[int, list[ch.Row]] = {}
    for winners_sql, computed_sql, ladder_sql in queries:
        winners_of.update(_by_unit(client.fetch(winners_sql)))
        computed_of.update(_by_unit(client.fetch(computed_sql)))
        ladder_of.update(_by_unit(client.fetch(ladder_sql)))
    for number in range(1, len(units) + 1):
        for row in winners_of.get(number, ()):
            winners.setdefault(row["request_id"], set()).add(row["estimator"])
        for row in computed_of.get(number, ()):
            computed.setdefault(row["request_id"], {})[int(row["quote_id"])] = int(
                row["nats_seq"]
            )
        for row in ladder_of.get(number, ()):
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
    # With the reason that stands if no re-quote places them.
    requote_candidates: list[tuple[RecordedQuote, str]] = [
        (o, "no quote id") for o in without_id
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
    # Those without an id were read with the link; those it failed are read now. A
    # re-quote depends only on the rows of the order's own request key and day, so the
    # two rounds place the same orders one round would.
    failed = [o for o, why in requote_candidates if why != "no quote id"]
    requoted, requote_addresses = _link_requotes(client, chain, without_id)
    if failed:
        more, more_addresses = _link_requotes(client, chain, failed)
        requoted.extend(more)
        for estimator, seen in more_addresses.items():
            mine = requote_addresses.setdefault(estimator, {})
            for address, n in seen.items():
                mine[address] = mine.get(address, 0) + n
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
    """The horizon runs to the next forwarded answer, or for the last one to creation."""
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
"""Where an ethflow re-quote lands relative to order creation (block time), generously."""
REQUOTE_SIBLING_SECONDS = 5
REQUOTE_ID_SPAN = 64
"""A re-quote's siblings lie within both bounds (seconds, quote ids) of its winner."""

REQUOTE_COLUMNS = (
    "quote_id, estimator, solver, lower(sell_token) as sell_, lower(buy_token) as buy_, "
    "kind, in_amount, out_amount, gas, verified, nats_seq, "
    "toString(event_timestamp) as ts"
)


REQUOTE_CHUNK = 100_000
"""Orders per re-quote unit: a day's in one."""


def _requote_units(
    orders: Sequence[RecordedQuote], chunk: int = REQUOTE_CHUNK
) -> tuple[list[_Unit], list[Sequence[RecordedQuote]]]:
    """A unit per creation day (and chunk) of orders: their request keys, and the orders."""
    from datetime import date, datetime, timedelta

    by_day: dict[str, list[RecordedQuote]] = {}
    for order in orders:
        if order.in_atoms > 0 and order.out_atoms > 0:
            by_day.setdefault(order.created[:10], []).append(order)
    units: list[_Unit] = []
    parts: list[Sequence[RecordedQuote]] = []
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
            condition = f"(lower(sell_token), lower(buy_token), kind, in_amount) in ({tuples})"
            units.append(_Unit(window, condition, len(keys)))
            parts.append(part)
    return units, parts


def _requote_queries(chain: int, orders: Sequence[RecordedQuote]) -> list[str]:
    """(winning quote ids, estimates) per group of units: what `_link_requotes` reads."""
    units, _ = _requote_units(orders)
    queries: list[str] = []
    for group in _unit_groups(units, UNIT_KEYS_PER_QUERY):
        tag, windows = _tagged(group)
        # `prewhere`: request-less rows are rare, so reading the null map first spares
        # reading the key columns of every row (0.3 s and 0.8 GB a day, not 4.8 s, 21 GB).
        request_less = (
            f"prewhere request_id is null where chain_id = {chain} "
            f"and quote_id is not null and {windows} and notEmpty(units)"
        )
        queries.append(
            f"select quote_id, {tag} from {ch.RAW_DATABASE}.winning_price_estimates "
            f"{request_less}"
        )
        queries.append(
            f"select {REQUOTE_COLUMNS}, {tag} from {ch.RAW_DATABASE}.price_estimates "
            f"{request_less} and success"
        )
    return queries


def _link_requotes(
    client: ch.Client, chain: int, orders: Sequence[RecordedQuote]
) -> tuple[list[MatchedQuote], dict[str, dict[str, int]]]:
    """Link ethflow orders to the request-less competition the backend ran when indexing
    them, found by the stored amount; also returns the settlement addresses seen
    ([docs](../docs/quotes.md#an-ethflow-orders-stored-quote-can-come-from-the-backends-own-re-quote)).
    """
    from datetime import datetime

    def when(stamp: str) -> datetime:
        return datetime.fromisoformat(stamp)

    matches: list[MatchedQuote] = []
    addresses: dict[str, dict[str, int]] = {}
    _, parts = _requote_units(orders)
    queries = _requote_queries(chain, orders)
    client.prefetch(queries)
    winning_of: dict[int, list[ch.Row]] = {}
    rows_of: dict[int, list[ch.Row]] = {}
    for winning_sql, rows_sql in zip(queries[::2], queries[1::2], strict=True):
        winning_of.update(_by_unit(client.fetch(winning_sql)))
        rows_of.update(_by_unit(client.fetch(rows_sql)))
    for number, part in enumerate(parts, 1):
        winning = {int(row["quote_id"]) for row in winning_of.get(number, ())}
        rows = rows_of.get(number, [])
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
    """Orders whose api and auction prices for the sell token disagree past the threshold.
    The auction price is where the order traded (a window median fails tokens that
    moved); the median serves only untraded orders. Unpriced tokens are left alone."""
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


# A trade belongs to the first settlement after it in its block, whose `Settlement` event
# carries the auction id (the `int_backend_data__trade_with_tx_hash` rule). A trade with
# no settlement recorded has none; it still counts towards its order's executed amount.
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
left join lateral (
    select s.auction_id
    from dbt.stg_backend_data__settlements s
    where s.block_number = wt.block_number and s.log_index > wt.log_index
    order by s.log_index
    limit 1
) s on true
"""

# Each (auction, token) by its position in that auction's price arrays.
TRADE_PRICES_SQL = """
select w.auction_id, encode(w.token, 'hex') as token, ca.price_values[p.position] as price
from unnest(%(pair_auctions)s::bigint[], %(pair_tokens)s::bytea[]) as w (auction_id, token)
join dbt.stg_backend_data__competition_auctions ca on ca.auction_id = w.auction_id
-- `offset 0` keeps the position computed once, not once per use
cross join lateral (
    select array_position(ca.price_tokens, w.token) as position offset 0
) as p
where p.position is not null
"""


def order_trades_query(start: str, end: str) -> tuple[str, dict[str, str]]:
    return ORDER_TRADES_SQL, {"start": start, "end": end}


def load_order_trades(
    conn: Connection, start: str, end: str
) -> tuple[list[OrderTrade], dict[str, int]]:
    """Every trade, with its auction, of orders created in `[start, end)` — including
    trades after `end` — and each order's executed sell amount over all its trades. Trades
    with an unrecorded settlement are left out of the list, not out of the amount."""
    trades: list[OrderTrade] = []
    executed: dict[str, int] = {}
    for row in fetch(conn, *order_trades_query(start, end)):
        sold = as_int(row["sell_amount"])
        executed[row["order_uid"]] = executed.get(row["order_uid"], 0) + sold
        if row["auction_id"] is None:
            continue
        trades.append(
            OrderTrade(
                order_uid=row["order_uid"],
                auction_id=row["auction_id"],
                sell_amount=sold,
                buy_amount=as_int(row["buy_amount"]),
            )
        )
    return trades, executed


def _priced_token(token: str, weth: str) -> str:
    """The native sentinel is priced as its wrapped ERC20."""
    return canonical_token(token, weth)


def trade_price_queries(
    orders: Sequence[RecordedQuote],
    trades: Sequence[OrderTrade],
    *,
    weth: str,
    chunk: int = 2000,
) -> list[tuple[str, dict[str, list[int] | list[bytes]]]]:
    """The queries `load_trade_prices` makes, for `db.prefetch`."""
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
    auction_ids = sorted({auction_id for auction_id, _ in pairs})
    queries: list[tuple[str, dict[str, list[int] | list[bytes]]]] = []
    for part in chunked(auction_ids, chunk):
        ids = set(part)
        wanted = [(a, t) for a, t in pairs if a in ids]
        queries.append(
            (
                TRADE_PRICES_SQL,
                {
                    "pair_auctions": [a for a, _ in wanted],
                    "pair_tokens": [bytes.fromhex(t) for _, t in wanted],
                },
            )
        )
    return queries


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
    queries = trade_price_queries(orders, trades, weth=weth, chunk=chunk)
    prefetch(conn, queries, first=True)
    prices: dict[tuple[int, str], int] = {}
    for sql, params in queries:
        for row in fetch(conn, sql, params):
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
    """Orders whose executed sell and buy values disagree past the threshold, each trade
    at its own auction's prices (`counterfactual.price_imbalanced`, per order). Blind to
    a pair mispriced by the same factor."""
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
    """`order_uid ->` sell-amount-weighted sell-token auction price, 1e18-scaled."""
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


def token_price_queries(
    auction_ids: Sequence[int], tokens: Sequence[str]
) -> list[tuple[str, dict[str, list[int] | list[bytes]]]]:
    """The queries `load_token_prices` makes, for `db.prefetch`."""
    if not auction_ids or not tokens:
        return []
    raw = [bytes.fromhex(token) for token in sorted(set(tokens))]
    return [
        (price_query(TOKEN_PRICES_SQL), {"ids": list(ids), "tokens": list(part)})
        for part in chunked(raw, 5000)
        for ids in chunked(list(auction_ids), TOKEN_PRICES_CHUNK)
    ]


def load_token_prices(
    conn: Connection, auction_ids: Sequence[int], tokens: Sequence[str]
) -> dict[str, float]:
    """Median native price per token over the window's auctions, for the `autopilot`
    ranking's out-token side. Absent tokens stay absent."""
    queries = token_price_queries(auction_ids, tokens)
    prefetch(conn, queries, first=True)
    counts: dict[str, dict[float, int]] = {}
    for sql, params in queries:
        for row in fetch(conn, sql, params):
            # As `percentile_cont` takes them: the numeric cast to double precision.
            by_value = counts.setdefault(row["token"], {})
            value = float(row["price"])
            by_value[value] = by_value.get(value, 0) + int(row["n"])
    prices: dict[str, float] = {}
    for token, by_value in counts.items():
        median = percentile_cont_half(by_value)
        if median:
            prices[token] = median
    return prices


def percentile_cont_half(counts: Mapping[float, int]) -> float:
    """`percentile_cont(0.5)` of a multiset given as value -> count, to the bit as
    Postgres computes it: the two middle values of the sorted rows, interpolated in
    double precision."""
    position = 0.5 * (sum(counts.values()) - 1)
    first, second = math.floor(position), math.ceil(position)
    low = high = None
    seen = 0
    for value in sorted(counts):
        seen += counts[value]
        if low is None and seen > first:
            low = value
        if seen > second:
            high = value
            break
    assert low is not None and high is not None, "an empty multiset has no median"
    if first == second:
        return low
    return low + (position - first) * (high - low)


# --- the counterfactual -------------------------------------------------------------

Leaf = Literal[
    "unaffected", "no_quote", "class_flip", "degraded", "unpriced", "nonpositive",
    "price_suspect", "implausible", "unrecorded",
]

IMPLAUSIBLE_QUOTE_FACTOR = PRICE_IMBALANCE_THRESHOLD
"""A replacement this far off the recorded rate, either way, is a broken estimate (e.g.
mis-scaled decimals) promoted to winner, not a price change."""


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
    """Effective quotes on the out side, unoriented; `delta_bps` orients them."""
    volume_native: int
    partially_fillable: bool
    quote_loo: Quote | None
    """For the fee path; `None` when unaffected or when no order would exist."""
    streamed: bool = False
    commit_dependent: bool = False

    @property
    def delta_bps(self) -> float:
        """Change in the shown rate (out per in), bps; negative is worse. A rate, not a
        cost ratio, so one order's contribution is bounded by its volume."""
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
        return self.delta_bps / 10_000 * self.volume_native


def diff_quote(
    match: MatchedQuote,
    removed_estimators: frozenset[str],
    *,
    ranking: QuoteRanking = "effective-sell",
    buy_token_prices: Mapping[str, float] | None = None,
    price_suspects: frozenset[str] = frozenset(),
) -> QuoteDiff:
    """Re-pick one request without `removed_estimators` and diff against the **recorded**
    quote, never the ladder's winner. Guard leaves are reached only after the removal is
    known to have moved the quote, so each is an excluded win."""
    order, request, take = match.order, match.request, match.stream
    pricing = order.pricing
    if ranking == "autopilot" and order.is_sell:
        price = (buy_token_prices or {}).get(order.buy_token)
        pricing = Pricing(pricing.gas_price, pricing.sell_token_price, price)

    base_effective = _recorded_effective(order)
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
        # Removing a loser moves neither the winner nor its verified class.
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
    """`effective_sell_amount` of the recorded quote; `Quote.fee` is gas in sell atoms."""
    if order.is_sell:
        sell = order.quote.sell_amount
        if sell <= 0:
            return 0.0
        return (sell - order.quote.fee) / sell * order.quote.buy_amount
    return float(order.quote.sell_amount + order.quote.fee)


# --- the limit price the order would have carried -----------------------------------


@dataclass(frozen=True)
class ShiftedLimit:
    """The user's limit wedge (signed limit rate over shown quote rate) applied to a
    replacement quote, tested against the recorded execution, which the solve side holds
    fixed ([docs](../docs/quotes.md#what-is-held-fixed-what-is-recomputed-and-why-the-user-effect-is-not-a-surplus)).
    """

    clears: bool
    """Only a class flip can make this false."""
    surplus_shift_atoms: int
    """Measured-surplus change in surplus-token atoms, positive when the limit weakens.
    A ranking test, not a user gain: never reported as Δsurplus."""


def _effective_rate(quote: Quote, is_sell: bool) -> Fraction:
    """Buy atoms per sell atom of the effective quote."""
    if is_sell:
        if quote.sell_amount <= 0:
            return Fraction(0)
        return Fraction(quote.buy_amount * (quote.sell_amount - quote.fee), quote.sell_amount**2)
    cost = quote.sell_amount + quote.fee
    return Fraction(quote.buy_amount, cost) if cost > 0 else Fraction(0)


def shifted_limit(order: Order, recorded: Quote, replacement: Quote) -> ShiftedLimit | None:
    """`None` when the order did not execute or a quote has no positive rate."""
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
    """The quote side of one removal scenario over one window. The fee effect flows
    through `counterfactual_quotes` into the protocol-fee delta. Class flips are counted
    but kept out of the bps figures: better in bps, worse in kind."""

    solver: str
    ranking: QuoteRanking
    window_orders: int = 0
    """Whole window, not just matched orders: an unmatched order adds zero, like an
    unmoved one."""
    window_volume_native: int = 0
    window_quoted_volume_native: int = 0
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
    """Guard leaves: wins excluded from every figure, so subsets of `quote_wins`."""
    stream_wins: int = 0
    commit_dependent: int = 0
    delta_volume_native: float = 0.0
    degraded_volume_native: int = 0
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
        """Δ effective quote rate, bps, over the window's whole executed volume."""
        if not self.window_volume_native:
            return None
        return self.delta_volume_native / self.window_volume_native * 10_000

    @property
    def conditional_bps(self) -> float | None:
        """Over the volume of the orders that moved; shown beside `window_bps`, never
        instead."""
        if not self.degraded_volume_native:
            return None
        return self.delta_volume_native / self.degraded_volume_native * 10_000

    @property
    def no_quote_share(self) -> float | None:
        if not self.window_orders:
            return None
        return self.no_quote / self.window_orders

    @property
    def counterfactual_quotes(self) -> dict[str, Quote]:
        """`order_uid -> quote` for the fee path; class flips included."""
        return {
            d.order_uid: d.quote_loo
            for d in self.diffs
            if d.quote_loo is not None
        }


# --- validation: reproduce the recorded pick ----------------------------------------


@dataclass
class PickValidation:
    """Does our pick return the recorded winner? A falling rate means the rule changed."""

    ranking: QuoteRanking
    requests: int = 0
    with_recorded_winner: int = 0
    reproduced: int = 0
    mixed_class: int = 0
    mixed_class_correct: int = 0
    """Requests with both verified classes: the only test of the verified-first rule."""
    unpriced: int = 0
    tied: int = 0
    """Another estimate shares the winner's key, so arrival order decided."""
    tie_mismatches: int = 0
    """Ties resolved to an equivalent estimator; not disagreements."""
    price_suspect: int = 0
    disagreements: list[tuple[str, str, str]] = field(
        default_factory=list[tuple[str, str, str]]
    )
    """`(request_id, recorded, ours)`, first few."""
    streams: int = 0
    streams_reproduced: int = 0
    """Streams with two or more forwarded answers that `replay_stream` reproduces."""
    stream_disagreements: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = field(
        default_factory=list[tuple[str, tuple[str, ...], tuple[str, ...]]]
    )
    """`(request_id, recorded, replayed)`, first few."""

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
    """Re-pick every match against its recorded winner; replay every stream."""
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
    """The quote side of one window, loaded once for every removal scenario."""

    network: str
    start: str
    end: str
    ranking: QuoteRanking
    orders: list[RecordedQuote]
    matches: list[MatchedQuote]
    estimator_map: dict[str, str]
    buy_token_prices: dict[str, float]
    validation: PickValidation
    link: LinkStats
    price_suspects: frozenset[str] = frozenset()
    """Orders failing either price check; out of every quote figure."""
    one_sided_suspects: int = 0
    two_sided_suspects: int = 0
    """An order can fail both checks."""
    orders_dropped: int = 0

    @property
    def trivial_orders(self) -> int:
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
        return sum(
            o.volume_native for o in self.orders if o.order_uid in self.price_suspects
        )

    def coverage_by_app(self) -> list[tuple[str, int, int]]:
        """`(app_code, orders, matched)`, busiest first: coverage varies too much by app
        for one rate."""
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
        """Empty means a solve-only solver, not an error."""
        return frozenset(
            estimator
            for estimator, address in self.estimator_map.items()
            if address in addresses
        )

    def analyse(self, solver: str, addresses: frozenset[str]) -> QuoteAnalysis:
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
    """Load the quote competition for `[start, end)` on one network. Raises
    `ch.NoQuoteDataError` where there is no quote data: empty columns would read as "no
    value" rather than "no data"."""
    emit: Callable[[str], None] = log or (lambda message: None)
    ch.chain_id(network)  # refuse an unsupported network before any query runs

    # Postgres reads ahead of everything else the run has queued: the quote side is used
    # first. Each step queues the next as soon as its inputs are known.
    prefetch(conn, window_queries(start, end), first=True)
    trades, executed = load_order_trades(conn, start, end)
    orders, dropped = load_recorded_quotes(conn, start, end, executed=executed)
    linkable = [o for o in orders if not o.trivial]
    emit(
        f"{len(orders)} orders created in [{start}, {end}) carry a recorded quote "
        f"({dropped} dropped as unevaluable); {len(orders) - len(linkable)} trivial "
        f"unwraps (quote solver 0x0) had no competition and are not linked"
    )
    weth = wrapped_native_token(network)
    # Data guards, not ranking inputs: they run under every ranking.
    guard_tokens = sorted({o.sell_token for o in orders})
    prefetch(
        conn,
        [
            *trade_price_queries(orders, trades, weth=weth),
            *(token_price_queries(auction_ids, guard_tokens) if ranking != "autopilot" else []),
        ],
        first=True,
    )
    id_link = link_by_quote_id(client, network, linkable, log=log)
    matches = id_link.matches
    estimator_map = id_link.estimator_map

    wanted = guard_tokens + (
        sorted({m.order.buy_token for m in matches}) if ranking == "autopilot" else []
    )
    auction_prices = load_token_prices(conn, auction_ids, wanted)
    # The auction feed is 1e18-scaled; `Pricing` is wei per atom.
    buy_token_prices = (
        {token: price / ONE_ETH for token, price in auction_prices.items()}
        if ranking == "autopilot"
        else {}
    )
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
