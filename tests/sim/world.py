"""The world generator: one seeded `random.Random` in, one synthetic chain window out.

The world is the *truth*. It holds every order, every bid with its executions, every fee
policy and quote, and the competition's outcome as the protocol would have recorded it —
computed here with `oracle`, never with `loo`. `simdb` and `simch` serve it in the shapes
the analytics DB and ClickHouse return; everything the pipeline knows comes through them.

Edge regions are sampled on purpose rather than left to chance: tied scores, zero-score
bids that trade at exactly the limit, single- and multi-pair bids, pairs whose fee is
unrecorded, the native-token sentinel next to its wrapped token, `max_winners`
saturation, dust amounts at the ceil-division boundary, partner fees declared beyond the
policy list, late and reverted settlements, missing penalty caps, streamed and re-quoted
quote competitions, price-suspect tokens and fast-path auctions.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction

from . import oracle
from .oracle import Answer, Bid, FeePolicy, Pair, QuoteTerms, Refused, Trade, WinRecord

ONE = 10**18
NATIVE = "ee" * 20
ZERO_ADDRESS = "00" * 20
SOLANA = "solana"
NATIVE_SOL = "00" * 32
"""The System Program id: the buy token of an order paid out in native SOL."""
LAMPORTS = oracle.LAMPORTS

# Copied, not imported: a mutated constant in `loo` must not move the world with it.
WRAPPED = {
    "mainnet": "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "xdai": "e91d153e0b41518a2ce8dd3d7944fa863463a97d",
    "base": "4200000000000000000000000000000000000006",
    "arbitrum-one": "82af49447d8a07e3bd95bd0d56f35241523fbab1",
    "bnb": "bb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    "polygon": "0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
    "sepolia": "fff9976782d46cc05630d1f6ebab18b2324d6b14",
    "avalanche": "b31f66aa3c1e785363f0875a1b74e27b85fd66c7",
    "ink": "4200000000000000000000000000000000000006",
    "linea": "e5d7c2a44ffddf6b295a15c148167daaaf5cf34f",
    "plasma": "6100e367285b01f48d07953803a2d8dca5d19873",
    SOLANA: "069b8857feab8184fb687f634618c035dac439dc1aeb3b5598a0f00000000001",
}
STABLES = {
    "mainnet": {"a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,
                "dac17f958d2ee523a2206206994597c13d831ec7": 6,
                "6b175474e89094c44da98b954eedeac495271d0f": 18},
    "xdai": {"2a22f9c3b484c3629090feed35f17ff8f88f76f0": 6,
             "4ecaba5870353805a9f068101a40e0f32ed605c6": 6},
    "base": {"833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,
             "50c5725949a6f0c72e6c4a641f24049a917db0cb": 18},
    "arbitrum-one": {"af88d065e77c8cc2239327c5edb3a432268e5831": 6,
                     "fd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6},
    "bnb": {"55d398326f99059ff775485246999027b3197955": 18,
            "8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18},
    "polygon": {"3c499c542cef5e3811e1192ce70d8cc03d5c3359": 6,
                "c2132d05d31c914a87c6611c10748aeb04b58e8f": 6},
    "sepolia": {},
    "avalanche": {"b97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": 6,
                  "d586e7f844cea2f87f50152665bcbc2c279d8d70": 18},
    "ink": {"2d270e6886d130d724215a266106e6832161eaed": 6,
            "99cbf1ff4527675ed3301671105c9f7748fb8a04": 6},
    "linea": {"176211869ca2b568f2a7d4ee941e073a821ee1ff": 6,
              "a219439258ca9da29e9cc4ce5596924745e12b93": 6},
    "plasma": {"b8ce59fc3717ada4c02eadf9682a9e934f625ebb": 6,
               "5d3a1ff2b6bab83b63cd9ad0787074081a52ef34": 18},
    SOLANA: {"c6fa7af3bedbad3a3d65f36aabc97431b1bbe4c2d2f6e0e47ca60203452f5d61": 6,
             "ce010e60afedb22717bd63192f54145a3f965a33bb83134e31593825f29c16eb": 6},
}
CHAIN_IDS = {"mainnet": 1, "xdai": 100, "base": 8453, "arbitrum-one": 42161, "bnb": 56,
             "polygon": 137, "avalanche": 43114, "ink": 57073, "linea": 59144,
             "plasma": 9745}
QUOTE_NETWORKS = tuple(CHAIN_IDS)

SOLVER_NAMES = ("Arc", "Arctic", "Baseline", "Gnosis_0x", "Quasi", "ExtQuasimodo",
                "Sector", "Horadrim")


def _hex(rng: random.Random, nbytes: int) -> str:
    return "".join(f"{rng.randrange(256):02x}" for _ in range(nbytes))


# --- the world's entities -------------------------------------------------------------


@dataclass
class Solver:
    name: str
    """The catalogue name; "Uncatalogued" is the catalogue's bucket for addresses its
    seed does not name, and such a solver is named by address."""
    addresses: list[str]
    environment: str = "prod"
    active: bool = True
    estimator: str | None = None
    """Its name in the quote competition, if it quotes."""
    bids: bool = True
    """False for a catalogued solver that never bid in the window."""

    @property
    def label(self) -> str:
        """What `--solver` and `--all-solvers` call it."""
        return self.addresses[0] if self.name == "Uncatalogued" else self.name


@dataclass
class OrderQuote:
    """The order's `order_quotes` row."""

    solver: str
    sell_amount: int
    buy_amount: int
    gas_amount: float
    gas_price: float
    sell_token_price: float
    verified: bool
    quote_id: int | None


@dataclass
class Order:
    uid: str
    sell_token: str
    buy_token: str
    sell_amount: int
    buy_amount: int
    kind: str
    partially_fillable: bool
    created: datetime
    partners: int = 0
    policies: tuple[FeePolicy, ...] = ()
    quote: OrderQuote | None = None
    app_code: str = "CoW Swap"
    market: Fraction = Fraction(1)
    """Buy atoms per sell atom at true prices."""

    def quote_terms(self) -> QuoteTerms | None:
        if self.quote is None:
            return None
        try:
            fee = oracle.quote_fee(self.quote.gas_amount, self.quote.gas_price,
                                   self.quote.sell_token_price)
        except Refused:
            return None
        return QuoteTerms(self.quote.sell_amount, self.quote.buy_amount, fee)


@dataclass
class Solution:
    uid: int
    solver: str
    trades: list[Trade]
    score: int
    pair_scores: dict[Pair, int]
    claims: frozenset[Pair]
    order_scores: dict[str, oracle.OrderScore]
    is_winner: bool = False
    filtered_out: bool = False


@dataclass
class Settlement:
    landed: bool
    in_time: bool


@dataclass
class Cap:
    upper: Decimal
    penalty: Decimal | None


@dataclass
class Auction:
    auction_id: int
    block: int
    time: datetime
    prices: dict[str, int]
    orders: list[str]
    solutions: list[Solution] = field(default_factory=list[Solution])
    references: dict[str, int] = field(default_factory=dict[str, int])
    reference_setters: dict[str, frozenset[str]] = field(
        default_factory=dict[str, frozenset[str]])
    settlements: dict[int, Settlement] = field(default_factory=dict[int, Settlement])
    caps: dict[int, Cap] = field(default_factory=dict[int, Cap])
    flat_lower: int | None = -(10**16)
    """None on Solana, whose mart has no flat floor."""
    excluded: bool = False
    fast_path: bool = False
    unpriced: bool = False
    mart: dict[str, oracle.RewardRow] = field(default_factory=dict[str, oracle.RewardRow])
    solana: bool = False

    @property
    def winners(self) -> list[Solution]:
        return [s for s in self.solutions if s.is_winner]

    @property
    def unit(self) -> int:
        """What a native price divides by: 1e18, or Solana's 1e9."""
        return LAMPORTS if self.solana else ONE


@dataclass
class Shadow:
    """An auction of the window's accounting periods that lies outside the window: only
    its reward rows exist, so the weekly marts have the rest of their periods."""

    auction_id: int
    time: datetime
    rewards: dict[str, tuple[Decimal, Decimal]]
    """`fct_solver_rewards_per_auction`: solver -> (upper cap, capped reward)."""
    won: dict[str, tuple[int, int]]
    """solver -> (won order executions, of them settled in time): the success rate."""


@dataclass
class Estimate:
    """One `price_estimates` row."""

    request_id: str | None
    """`None` for a re-quote the backend ran itself."""
    answer: Answer
    quote_id: int
    nats_seq: int
    timestamp: datetime
    solver: str
    """Settlement address as ClickHouse stores it (`0x`-prefixed, any case), or ''."""
    success: bool = True
    kind: str = "sell"
    sell_token: str = ""
    buy_token: str = ""
    in_amount: int = 0


@dataclass
class Request:
    request_id: str | None
    estimates: list[Estimate]
    winner: Estimate | None
    """The `winning_price_estimates` row; `None` for a stream the client dropped."""
    forwarded: list[Estimate] = field(default_factory=list[Estimate])
    """`quote_computed` rows, in order."""


@dataclass
class World:
    seed: int
    network: str
    start: str
    end: str
    max_winners: int
    tokens: dict[str, int]
    """True native price per token (wei per atom, scaled by 1e18)."""
    solvers: list[Solver]
    orders: dict[str, Order]
    auctions: list[Auction]
    requests: list[Request] = field(default_factory=list[Request])
    periods: list[tuple[int, int, float | None]] = field(
        default_factory=list[tuple[int, int, float | None]])
    extra_window_ids: list[int] = field(default_factory=list[int])
    """Auction ids the window query names that hold no bid at all."""
    surplus_model_off: set[tuple[int, int, str]] = field(
        default_factory=set[tuple[int, int, str]])
    surplus_model_null: set[tuple[int, int, str]] = field(
        default_factory=set[tuple[int, int, str]])
    mart_off: set[tuple[int, str]] = field(default_factory=set[tuple[int, str]])
    """(auction, solver) rows whose mart reward was perturbed: `validate-rewards` must
    find exactly these."""
    suspect_tokens: dict[int, str] = field(default_factory=dict[int, str])
    linkable: dict[str, str] = field(default_factory=dict[str, str])
    """order uid -> how the quote link should place it ('id', 'stream', 'requote') or
    why it should not (the unlinked reasons)."""
    quote_suspects: set[str] = field(default_factory=set[str])
    shadows: list[Shadow] = field(default_factory=list[Shadow])
    relative: dict[int, list[tuple[str, str, float]]] = field(
        default_factory=dict[int, list[tuple[str, str, float]]])
    """`int_backend_data__solver_relative_surplus_on_executed_orders`: auction ->
    (solver, order uid, relative surplus)."""
    shares: dict[int, list[tuple[str, str, float]]] = field(
        default_factory=dict[int, list[tuple[str, str, float]]])
    """`int_quote_data__quote_reward_shares`: auction -> (order uid, solver, share)."""
    quote_budget: dict[int, float] = field(default_factory=dict[int, float])
    """Σ quote budget contribution of each auction's trades."""
    consistency_mart: dict[str, dict[str, tuple[float, float]]] = field(
        default_factory=dict[str, dict[str, tuple[float, float]]])
    """period -> solver -> (consistency reward, total relative surplus)."""
    quote_mart: dict[str, dict[str, tuple[float, float, int]]] = field(
        default_factory=dict[str, dict[str, tuple[float, float, int]]])
    """period -> solver -> (quote reward, period budget, reward orders)."""
    fct_cut: int | None = None
    """Auctions above this id have no `fct_solver_rewards_per_auction` row yet."""

    @property
    def weth(self) -> str:
        return WRAPPED[self.network]

    @property
    def solana(self) -> bool:
        return self.network == SOLANA

    @property
    def native(self) -> str:
        """The native-token sentinel: 0xee…ee, or Solana's System Program id."""
        return NATIVE_SOL if self.solana else NATIVE

    @property
    def unit(self) -> int:
        """What a native price divides by; native amounts are in its smallest unit."""
        return LAMPORTS if self.solana else ONE

    @property
    def decimals(self) -> int:
        return 9 if self.solana else 18

    @property
    def symbol(self) -> str:
        """How `loo` labels native amounts: SOL, and ETH on every EVM network."""
        return "SOL" if self.solana else "ETH"

    @property
    def price_scale(self) -> int:
        """What `loo` multiplies a recorded price by: onto the 1e18 denominator."""
        return ONE // self.unit

    @property
    def chain_id(self) -> int | None:
        return CHAIN_IDS.get(self.network)

    def solver_of(self, address: str) -> Solver:
        return next(s for s in self.solvers if address in s.addresses)

    def bidding_addresses(self) -> set[str]:
        return {s.solver for a in self.auctions for s in a.solutions}


# --- generation -----------------------------------------------------------------------


@dataclass
class Knobs:
    """What one seed samples. Kept separate so pinned worlds can force a region."""

    network: str
    auctions: int
    orders: int
    max_winners: int
    dust: float
    partial: float
    tie: float
    zero_extra: float
    native_buy: float
    suspect: float
    fast_path: float
    partner_overflow: float
    no_penalty_caps: float
    no_penalty_window: float
    excluded: float
    late: float
    reverted: float
    stream: float
    requote: float
    multi_order: float


def sample_knobs(rng: random.Random) -> Knobs:
    return Knobs(
        network=rng.choice(("mainnet", "mainnet", "bnb", "base", "arbitrum-one", "xdai",
                            "polygon", "avalanche", "ink", "linea", "plasma")),
        auctions=rng.randint(3, 12),
        orders=rng.randint(4, 16),
        max_winners=rng.choice((1, 2, 3, 20, 20, 20)),
        dust=rng.choice((0.0, 0.1, 0.4)),
        partial=rng.choice((0.0, 0.2, 0.5)),
        tie=rng.choice((0.0, 0.2, 0.5)),
        zero_extra=rng.choice((0.0, 0.2, 0.5)),
        native_buy=rng.choice((0.0, 0.3)),
        suspect=rng.choice((0.0, 0.0, 0.15)),
        fast_path=rng.choice((0.0, 0.0, 0.2)),
        partner_overflow=rng.choice((0.0, 0.1)),
        no_penalty_caps=rng.choice((0.0, 0.2)),
        no_penalty_window=rng.choice((0.0, 0.0, 0.3)),
        excluded=rng.choice((0.0, 0.0, 0.2)),
        late=rng.choice((0.0, 0.1)),
        reverted=rng.choice((0.0, 0.15, 0.3)),
        stream=rng.choice((0.0, 0.3)),
        requote=rng.choice((0.0, 0.15)),
        multi_order=rng.choice((0.3, 0.6)),
    )


def solana_knobs(rng: random.Random) -> Knobs:
    """A Solana world: no fast path, no fee policy and no partner fee exist there."""
    return replace(sample_knobs(rng), network=SOLANA, fast_path=0.0, partner_overflow=0.0)


def generate(seed: int, knobs: Knobs | None = None) -> World:
    rng = random.Random(seed)
    k = knobs or sample_knobs(rng)
    day = datetime(2026, 11, rng.randint(1, 20))
    start, end = day, day + timedelta(days=rng.choice((1, 1, 2)))
    world = World(
        seed=seed,
        network=k.network,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        max_winners=k.max_winners,
        tokens=_tokens(rng, k.network),
        solvers=_solvers(rng, 32 if k.network == SOLANA else 20),
        orders={},
        auctions=[],
    )
    quote_ids = iter(range(1_600_000_000 + seed * 10_000, 1_700_000_000))
    nats = iter(range(1, 10**9))
    for _ in range(k.orders):
        _add_order(rng, world, k, start, end, quote_ids, nats)
    while len(world.orders) < 2:
        _add_order(rng, world, k, start, end, quote_ids, nats)
    if world.chain_id is not None and rng.random() < 0.5:
        _add_unwrap(rng, world, start)
    _add_auctions(rng, world, k, start, end)
    while not any(not a.fast_path for a in world.auctions):
        # Every bid refused, or every auction fast-path: a window with nothing to replay
        # tests nothing, so draw again from the same stream.
        _add_auctions(rng, world, replace(k, fast_path=0.0), start, end)
    _record_trades_and_periods(rng, world)
    # Its own stream: the world above stays what it was before the accounting tables.
    record_payouts(random.Random(seed * 7_919 + 13), world)
    return world


def _tokens(rng: random.Random, network: str) -> dict[str, int]:
    if network == SOLANA:
        # Lamports per atom × 1e9: SOL itself, wrapped or native, at the denominator.
        tokens = {WRAPPED[network]: LAMPORTS, NATIVE_SOL: LAMPORTS}
        for stable, decimals in STABLES[network].items():
            # $1 at ~70 USD per SOL.
            tokens[stable] = LAMPORTS * LAMPORTS // 10**decimals // rng.randint(60, 80)
        for _ in range(rng.randint(2, 5)):
            tokens[_hex(rng, 32)] = rng.randint(1, 9) * 10 ** rng.randint(0, 21)
        return tokens
    tokens = {WRAPPED[network]: ONE, NATIVE: ONE}
    for stable, decimals in STABLES[network].items():
        # $1 at ~3,000 USD per native token.
        tokens[stable] = 10 ** (36 - decimals) // rng.randint(2_900, 3_100)
    for _ in range(rng.randint(2, 5)):
        tokens[_hex(rng, 20)] = rng.randint(1, 9) * 10 ** rng.randint(6, 30)
    return tokens


def _solvers(rng: random.Random, nbytes: int = 20) -> list[Solver]:
    names = rng.sample(SOLVER_NAMES, rng.randint(3, 6))
    if rng.random() < 0.5 and "Arc" not in names:
        names.append("Arc")
    if rng.random() < 0.5 and "Arctic" not in names:
        names.append("Arctic")
    solvers: list[Solver] = []
    for position, name in enumerate(names):
        rotated = rng.random() < 0.25
        solvers.append(Solver(
            name=name,
            addresses=[_hex(rng, nbytes) for _ in range(2 if rotated else 1)],
            # Someone always quotes, or no order could ever have been placed.
            estimator=f"est-{name.lower()}" if position == 0 or rng.random() < 0.85 else None,
        ))
    for _ in range(rng.randint(0, 2)):
        solvers.append(Solver(name="Uncatalogued", addresses=[_hex(rng, nbytes)],
                              estimator=f"est-{_hex(rng, 3)}" if rng.random() < 0.5 else None))
    # Catalogued, never bid: resolves, then fails for want of solutions.
    solvers.append(Solver(name="Dormant", addresses=[_hex(rng, nbytes)], active=False,
                          bids=False))
    # The catalogue's bucket for unnamed addresses, never matched by name.
    solvers.append(Solver(name="Uncatalogued", addresses=[_hex(rng, nbytes)], bids=False))
    return solvers


def _amount(rng: random.Random, k: Knobs, price: int, unit: int = ONE) -> int:
    if rng.random() < k.dust:
        return rng.choice((1, 2, 3, rng.randint(1, 1000)))
    # Roughly 0.01 to 100 native tokens' worth.
    worth = rng.randint(unit // 100, unit * 100)
    return max(1, worth * unit // price)


def _answers(
    rng: random.Random, world: World, is_sell: bool, in_amount: int, market: Fraction,
) -> list[tuple[Answer, str]]:
    """A ladder in arrival order: (answer, settlement address or '')."""
    quoters = [s for s in world.solvers if s.estimator and s.bids]
    chosen = rng.sample(quoters, min(len(quoters), rng.randint(1, 5)))
    ladder: list[tuple[Answer, str]] = []
    tie_out: int | None = None
    for solver in chosen:
        drift = Fraction(rng.randint(-100, 20), 10_000)
        out = (math.floor(in_amount * market * (1 + drift)) if is_sell
               else math.ceil(in_amount / market * (1 - drift)))
        if tie_out is not None and rng.random() < 0.3:
            out = tie_out
        tie_out = out
        gas = rng.randint(50_000, 400_000)
        roll = rng.random()
        if roll < 0.05:
            gas = 0  # never entered the comparison
        elif roll < 0.1:
            out = max(1, out // 10)  # a broken estimate
        address = solver.addresses[0] if rng.random() < 0.9 else ""
        if address and rng.random() < 0.3:
            address = address.upper()
        assert solver.estimator is not None
        ladder.append((Answer(solver.estimator, max(out, 0), gas, rng.random() < 0.7),
                       ("0x" + address) if address else ""))
    return ladder


def _add_order(
    rng: random.Random,
    world: World,
    k: Knobs,
    start: datetime,
    end: datetime,
    quote_ids: Iterator[int],
    nats: Iterator[int],
) -> None:
    tokens = list(world.tokens)
    native = world.native
    sell_token, buy_token = rng.sample([t for t in tokens if t != native], 2)
    if rng.random() < k.native_buy:
        buy_token = native
        if sell_token == world.weth:
            sell_token = rng.choice([t for t in tokens if t not in (native, world.weth)])
    kind = "sell" if rng.random() < 0.7 else "buy"
    market = Fraction(world.tokens[sell_token], world.tokens[buy_token])
    created = start + timedelta(seconds=rng.randint(60, int((end - start).total_seconds()) - 60))
    if rng.random() < 0.1:
        created = start - timedelta(minutes=rng.randint(1, 20))  # before the quote window
    uid = _hex(rng, 32 if world.solana else 56)
    in_amount = _amount(rng, k, world.tokens[sell_token if kind == "sell" else buy_token],
                        world.unit)
    gas_price = float(rng.randint(1, 60) * 10**9)
    sell_token_price = world.tokens[sell_token] / 1e18
    if rng.random() < 0.05:
        world.quote_suspects.add(uid)
        sell_token_price *= 10_000.0
    ladder = _answers(rng, world, kind == "sell", in_amount, market)
    winner = oracle.quote_winner([a for a, _ in ladder], kind == "sell", float(in_amount),
                                 gas_price, sell_token_price)

    def next_id() -> int:
        return next(quote_ids)

    def next_seq() -> int:
        return next(nats)

    taken = winner
    request_id = f"req-{_hex(rng, 8)}"
    estimates = [
        Estimate(request_id, answer, next_id(), next_seq(),
                 created - timedelta(seconds=10 - i), address, kind=kind,
                 sell_token=sell_token, buy_token=buy_token, in_amount=in_amount)
        for i, (answer, address) in enumerate(ladder)
    ]
    by_answer = {id(e.answer): e for e in estimates}
    request = Request(request_id, estimates, by_answer[id(winner)] if winner else None)
    how = "id"
    streamed = world.chain_id is not None and rng.random() < k.stream
    if winner is not None:
        forwards = oracle.stream_forwards([a for a, _ in ladder], kind == "sell",
                                          float(in_amount), gas_price, sell_token_price)
        sent = [by_answer[id(a)] for a in forwards]
        if streamed and len(sent) >= 2:
            take = rng.randrange(len(sent))
            taken = sent[take].answer
            request.forwarded = sent
            how = "stream"
            if rng.random() < 0.3 and take < len(sent) - 1:
                # The client dropped the stream: no winner, and nothing after the next
                # forwarded answer was ever recorded.
                drop_after = estimates.index(sent[take + 1])
                request.estimates = estimates[: drop_after + 1]
                request.forwarded = sent[: take + 2]
                request.winner = None
        else:
            request.forwarded = [by_answer[id(winner)]] if winner else []
    if taken is None or rng.random() < 0.05:
        # Nothing usable answered: an order cannot exist without a quote.
        return
    fee = oracle.quote_fee(taken.gas, gas_price, sell_token_price)
    if kind == "sell":
        quote_sell, quote_buy = in_amount, taken.out_amount
        effective = Fraction(in_amount - fee, in_amount) * taken.out_amount
        slip = Fraction(rng.choice((0, 5, 10, 50)), 1000)
        sell_amount = in_amount
        buy_amount = max(1, math.floor(effective * (1 - slip)))
    else:
        quote_sell, quote_buy = taken.out_amount, in_amount
        slip = Fraction(rng.choice((0, 5, 10, 50)), 1000)
        buy_amount = in_amount
        sell_amount = max(1, math.ceil((taken.out_amount + fee) * (1 + slip)))
    quote_id: int | None = next(e for e in request.estimates if e.answer is taken).quote_id
    if world.chain_id is None:
        how = "no network"
    else:
        roll = rng.random()
        if roll < k.requote and in_amount >= 10**6:
            how = "requote" if rng.random() < 0.7 else "no quote id"
            quote_id = rng.choice((0, None)) if how == "no quote id" else rng.choice((0, 7))
        elif roll < k.requote + 0.05:
            how = "id not in the competition record"
            quote_id = 999_999_999_999
        elif roll < k.requote + 0.08 and how == "id":
            how = "stored quote not reproduced"
            if kind == "sell":
                quote_buy += 1
            else:
                quote_sell += 1
        elif roll < k.requote + 0.10 and how == "stream":
            # The order names an answer the stream never forwarded.
            unsent = [e for e in request.estimates
                      if e not in request.forwarded and e.answer.out_amount
                      == (quote_buy if kind == "sell" else quote_sell)]
            if unsent:
                quote_id = unsent[0].quote_id
                how = "not a forwarded answer of its stream"
    order = Order(
        uid=uid, sell_token=sell_token, buy_token=buy_token, sell_amount=sell_amount,
        buy_amount=buy_amount, kind=kind, partially_fillable=rng.random() < k.partial,
        created=created, market=market,
        app_code=rng.choice(("CoW Swap", "CoW Swap", "1inch", "(none)")),
    )
    order.quote = OrderQuote(
        solver=_address_of(world, taken),
        sell_amount=quote_sell, buy_amount=quote_buy, gas_amount=float(taken.gas),
        gas_price=gas_price, sell_token_price=sell_token_price, verified=taken.verified,
        quote_id=quote_id,
    )
    if not world.solana:
        _policies(rng, k, order)
    world.orders[uid] = order
    if created >= start:
        world.linkable[uid] = how
    if world.chain_id is None:
        return
    if how == "requote":
        world.requests.append(_requote(rng, world, order, next_id, next_seq))
    world.requests.append(request)


def _address_of(world: World, answer: Answer) -> str:
    for solver in world.solvers:
        if solver.estimator == answer.estimator:
            return solver.addresses[0]
    return ZERO_ADDRESS


def _requote(rng: random.Random, world: World, order: Order,
             next_id: Callable[[], int], next_seq: Callable[[], int]) -> Request:
    """The backend's own competition at indexing: request-less estimates of the order's
    request key, a second or two after creation, the winner reproducing the stored
    amount. The order's stored quote is rewritten to that winner."""
    assert order.quote is not None
    is_sell = order.kind == "sell"
    in_amount = order.quote.sell_amount if is_sell else order.quote.buy_amount
    ladder = _answers(rng, world, is_sell, in_amount, order.market)
    winner = oracle.quote_winner([a for a, _ in ladder], is_sell, float(in_amount),
                                 order.quote.gas_price, order.quote.sell_token_price)
    base = next_id()
    when = order.created + timedelta(seconds=rng.randint(1, 3))
    estimates = [
        Estimate(None, answer, base + i, next_seq(),
                 when + timedelta(milliseconds=50 * i), address, kind=order.kind,
                 sell_token=order.sell_token, buy_token=order.buy_token,
                 in_amount=in_amount)
        for i, (answer, address) in enumerate(ladder)
    ]
    # A sibling far from the anchor's quote ids is someone else's competition.
    for _ in range(len(estimates)):
        next_id()  # the block of ids the orderbook drew is the re-quote's alone
    if winner is None:
        world.linkable[order.uid] = (
            "id not in the competition record" if order.quote.quote_id else "no quote id"
        )
        return Request(None, estimates, None)
    chosen = next(e for e in estimates if e.answer is winner)
    if is_sell:
        order.quote.buy_amount = winner.out_amount
    else:
        order.quote.sell_amount = winner.out_amount
    order.quote.gas_amount = float(winner.gas)
    order.quote.verified = winner.verified
    order.quote.solver = _address_of(world, winner)
    _relimit(order)
    return Request(None, estimates, chosen)


def _relimit(order: Order) -> None:
    """Keep the order's limit satisfiable after its stored quote changed."""
    terms = order.quote_terms()
    if terms is None:
        return
    if order.kind == "sell":
        effective = Fraction(terms.sell_amount - terms.fee, terms.sell_amount) * terms.buy_amount
        order.buy_amount = max(1, min(order.buy_amount, math.floor(effective)))
    else:
        order.sell_amount = max(order.sell_amount, terms.sell_amount + terms.fee)


def _add_unwrap(rng: random.Random, world: World, start: datetime) -> None:
    """A wrapped-to-native unwrap: no competition priced it (quote solver 0x0)."""
    amount = rng.randint(10**15, 10**19)
    uid = _hex(rng, 56)
    world.orders[uid] = Order(
        uid=uid, sell_token=world.weth, buy_token=NATIVE, sell_amount=amount,
        buy_amount=amount, kind="sell", partially_fillable=False,
        created=start + timedelta(seconds=rng.randint(60, 3600)),
        quote=OrderQuote(ZERO_ADDRESS, amount, amount, 0.0, 1e9, 1.0, False, 0),
    )
    world.linkable[uid] = "trivial"


def _policies(rng: random.Random, k: Knobs, order: Order) -> None:
    roll = rng.random()
    dao: list[FeePolicy] = []
    if roll < 0.3:
        dao.append(FeePolicy("priceimprovement", rng.choice((0.5, 0.3)),
                             rng.choice((0.0098, 0.01))))
    elif roll < 0.5:
        dao.append(FeePolicy("surplus", rng.choice((0.5, 0.9)), rng.choice((0.01, 0.0005))))
    elif roll < 0.7:
        dao.append(FeePolicy("volume", rng.choice((3e-05, 0.0002, 0.001))))
    partners = rng.choice((0, 0, 1, 1, 2))
    partner = [FeePolicy("volume", rng.choice((0.003, 0.001, 1e-05))) for _ in range(partners)]
    order.policies = tuple(dao + partner)
    order.partners = partners
    if rng.random() < k.partner_overflow:
        order.partners = len(order.policies) + rng.randint(0, 1)


def _execute(rng: random.Random, k: Knobs, order: Order) -> Trade:
    """One execution of `order` at or above its limit."""
    if order.kind == "sell":
        executed_sell = order.sell_amount
        if order.partially_fillable and rng.random() < 0.6:
            executed_sell = rng.randint(1, order.sell_amount)
        floor_buy = math.ceil(Fraction(executed_sell * order.buy_amount, order.sell_amount))
        fair = math.floor(executed_sell * order.market)
        room = max(0, fair - floor_buy)
        extra = 0 if rng.random() < k.zero_extra else rng.randint(0, room)
        return Trade(order.uid, order.sell_token, order.buy_token, order.sell_amount,
                     order.buy_amount, executed_sell, floor_buy + extra, "sell",
                     order.partially_fillable)
    executed_buy = order.buy_amount
    if order.partially_fillable and rng.random() < 0.6:
        executed_buy = rng.randint(1, order.buy_amount)
    ceiling_sell = math.floor(Fraction(executed_buy * order.sell_amount, order.buy_amount))
    fair = math.ceil(executed_buy / order.market)
    room = max(0, ceiling_sell - fair)
    extra = 0 if rng.random() < k.zero_extra else rng.randint(0, room)
    return Trade(order.uid, order.sell_token, order.buy_token, order.sell_amount,
                 order.buy_amount, max(0, ceiling_sell - extra), executed_buy, "buy",
                 order.partially_fillable)


def canonical(token: str, world: World) -> str:
    """The native-token sentinel trades its wrapped token's pair."""
    return world.weth if token == world.native else token


def score_solution(
    world: World, trades: list[Trade], prices: dict[str, int]
) -> tuple[int, dict[Pair, int], dict[str, oracle.OrderScore]]:
    """The recorded score: Σ native(surplus + fees), per raw pair. Raises `Refused`."""
    pair_scores: dict[Pair, int] = {}
    per_order: dict[str, oracle.OrderScore] = {}
    for trade in trades:
        order = world.orders[trade.uid]
        scored = oracle.order_score(trade, order.policies, order.quote_terms(),
                                    order.partners, prices, world.unit)
        per_order[trade.uid] = scored
        pair = (trade.sell_token, trade.buy_token)
        pair_scores[pair] = pair_scores.get(pair, 0) + scored.score_native
    return sum(pair_scores.values()), pair_scores, per_order


def _add_auctions(rng: random.Random, world: World, k: Knobs, start: datetime,
                  end: datetime) -> None:
    auction_id = rng.randint(14_000_000, 14_500_000)
    block = rng.randint(23_000_000, 24_000_000)
    span = (end - start).total_seconds()
    bidders = [s for s in world.solvers if s.bids]
    order_uids = [u for u, o in world.orders.items() if o.quote is None or
                  o.quote.solver != ZERO_ADDRESS] or list(world.orders)
    times = sorted(start + timedelta(seconds=rng.uniform(0, span - 1)) for _ in range(k.auctions))
    for when in times:
        auction_id += rng.randint(1, 40)
        block += rng.randint(1, 30)
        if rng.random() < 0.15:
            world.extra_window_ids.append(auction_id)
            auction_id += rng.randint(1, 5)
        prices = {
            token: max(1, price * rng.randint(990, 1010) // 1000)
            for token, price in world.tokens.items()
        }
        members = rng.sample(order_uids, min(len(order_uids), rng.randint(1, 6)))
        auction = Auction(auction_id, block, when, prices, members, solana=world.solana)
        if world.solana:
            auction.flat_lower = None
        if rng.random() < k.fast_path:
            _fast_path(rng, world, auction)
        else:
            if rng.random() < k.suspect:
                token = rng.choice([world.orders[u].buy_token for u in members])
                prices[token] = prices[token] * 10_000
                world.suspect_tokens[auction_id] = token
            if rng.random() < k.no_penalty_window:
                auction.flat_lower = 0
            auction.excluded = rng.random() < k.excluded
            _bids(rng, world, k, auction, bidders)
        if auction.solutions:
            world.auctions.append(auction)


def _fast_path(rng: random.Random, world: World, auction: Auction) -> None:
    """One order settled from its quote competition: the winning quote fills it at the
    limit, the losing quotes are stored below it with score 0, filtered out."""
    auction.fast_path = True
    order = world.orders[auction.orders[0]]
    bidders = [s for s in world.solvers if s.bids]
    winner = rng.choice(bidders)
    if order.kind == "sell":
        at_limit = math.ceil(Fraction(order.sell_amount * order.buy_amount, order.sell_amount))
        fills = [(order.sell_amount, at_limit), (order.sell_amount, max(0, at_limit - 1))]
    else:
        at_limit = order.sell_amount
        fills = [(at_limit, order.buy_amount), (at_limit + 1, order.buy_amount)]
    for uid, ((sold, bought), solver) in enumerate(zip(fills, (winner, rng.choice(bidders)),
                                                       strict=True)):
        trade = Trade(order.uid, order.sell_token, order.buy_token, order.sell_amount,
                      order.buy_amount, sold, bought, order.kind)
        auction.solutions.append(Solution(
            uid=uid, solver=solver.addresses[0], trades=[trade], score=0, pair_scores={},
            claims=frozenset(), order_scores={}, is_winner=uid == 0, filtered_out=uid != 0))
    auction.settlements[0] = Settlement(True, True)
    auction.caps[0] = Cap(Decimal(0), Decimal(0))
    auction.references = {winner.addresses[0]: 0}
    record_mart(auction)


def _bids(rng: random.Random, world: World, k: Knobs, auction: Auction,
          bidders: list[Solver]) -> None:
    candidates: list[tuple[str, list[Trade], int, dict[Pair, int], dict[str, oracle.OrderScore]]] = []
    tie_scores: list[int] = []
    for _ in range(rng.randint(1, 8)):
        solver = rng.choice(bidders)
        address = rng.choice(solver.addresses)
        multi = len(auction.orders) >= 2 and rng.random() < k.multi_order
        n = rng.randint(2, min(4, len(auction.orders))) if multi else 1
        chosen = rng.sample(auction.orders, min(n, len(auction.orders)))
        trades = [_execute(rng, k, world.orders[u]) for u in chosen]
        if tie_scores and rng.random() < k.tie:
            # Copy another bid outright: same orders, same fills, same score.
            _, trades, _, _, _ = rng.choice(candidates)
            trades = list(trades)
        try:
            score, pairs, per_order = score_solution(world, trades, auction.prices)
        except Refused:
            continue
        tie_scores.append(score)
        candidates.append((address, trades, score, pairs, per_order))
    if not candidates:
        return
    # The autopilot shuffles before its stable sort: that shuffle is the tie-break.
    rng.shuffle(candidates)
    record_auction(world, auction, candidates)
    missing_caps = rng.random() < k.no_penalty_caps
    for solution in auction.winners:
        roll = rng.random()
        if roll < k.reverted:
            settlement = Settlement(False, False)
        elif roll < k.reverted + k.late:
            settlement = Settlement(True, False)
        else:
            settlement = Settlement(True, True)
        # A Postgres `numeric` of bounded scale, as the column is: sums of these stay
        # exact in the 78-place context `loo` sets.
        penalty = None if missing_caps else Decimal(
            rng.randint(0, 10**14) * 10**20 // rng.choice((1, 2, 3, 7))
        ).scaleb(-20)
        settle(auction, solution, settlement, penalty)
    record_mart(auction)


Candidate = tuple[str, list[Trade], int, dict[Pair, int], dict[str, oracle.OrderScore]]


def candidate(world: World, auction: Auction, address: str, trades: list[Trade]) -> Candidate:
    """One bid, scored the way the protocol scores it. Raises `Refused`."""
    score, pairs, per_order = score_solution(world, trades, auction.prices)
    return address, trades, score, pairs, per_order


def record_auction(world: World, auction: Auction, candidates: list[Candidate]) -> None:
    """Arbitrate `candidates` (in tie-break order) and store them as the autopilot
    would: uids best-to-worst, winners first, filtered-out last, reference scores."""
    bids = [
        Bid(i, address, score, pairs,
            frozenset((canonical(t.sell_token, world), canonical(t.buy_token, world))
                      for t in trades))
        for i, (address, trades, score, pairs, _) in enumerate(candidates)
    ]
    outcome = oracle.arbitrate(bids, world.max_winners)
    filtered_sorted = sorted(outcome.filtered, key=lambda b: (-b.total, b.uid))
    order = outcome.ranked + filtered_sorted
    uid_of = {bid.uid: new for new, bid in enumerate(order)}
    refs = oracle.references(outcome.ranked, outcome.winners, world.max_winners)
    auction.references = {solver: score for solver, (score, _) in refs.items()}
    auction.reference_setters = {solver: setters for solver, (_, setters) in refs.items()}
    filtered = {b.uid for b in outcome.filtered}
    for bid in order:
        address, trades, score, pairs, per_order = candidates[bid.uid]
        auction.solutions.append(Solution(
            uid=uid_of[bid.uid], solver=address, trades=trades, score=score, pair_scores=pairs,
            claims=bid.claims, order_scores=per_order, is_winner=bid.uid in outcome.winners,
            filtered_out=bid.uid in filtered))


def settle(auction: Auction, solution: Solution, settlement: Settlement,
           penalty: Decimal | None) -> None:
    """A recorded winner's on-chain outcome and its caps: the upper cap is half the
    protocol's own fee, realised only if the batch landed."""
    auction.settlements[solution.uid] = settlement
    protocol = sum(s.fee_native - (s.partner_native or 0) for s in solution.order_scores.values())
    upper = (Decimal(protocol) * Decimal("0.5")) if settlement.landed else Decimal(0)
    auction.caps[solution.uid] = Cap(upper, penalty)


def record_mart(auction: Auction) -> None:
    wins = [
        WinRecord(s.solver, s.score, auction.settlements[s.uid].in_time,
                  auction.caps[s.uid].upper, auction.caps[s.uid].penalty)
        for s in auction.winners
    ]
    auction.mart = oracle.rewards(wins, auction.references, auction.flat_lower, auction.excluded,
                                  capped=True)


def _record_trades_and_periods(rng: random.Random, world: World) -> None:
    blocks = sorted(a.block for a in world.auctions)
    if blocks:
        cut = rng.choice(blocks)
        first, last = blocks[0] - rng.randint(0, 50), blocks[-1] + rng.randint(0, 50)
        rate = rng.choice((0.0021, 3.3e-05, None))
        world.periods = [(first, cut, rng.choice((0.0021, 3.3e-05))), (cut + 1, last, rate)]
    # The model rounds the surplus quotient before taking its ceiling; perturb a few rows
    # so the cross-check has something to find.
    for auction in world.auctions:
        if auction.fast_path:
            continue
        for solution in auction.solutions:
            for trade in solution.trades:
                key = (auction.auction_id, solution.uid, trade.uid)
                roll = rng.random()
                if roll < 0.05:
                    world.surplus_model_off.add(key)
                elif roll < 0.08:
                    world.surplus_model_null.add(key)
        for solver in auction.mart:
            if rng.random() < 0.05:
                world.mart_off.add((auction.auction_id, solver))


# --- the accounting tables: the rewards a solver is paid ------------------------------


def period_start(moment: datetime) -> datetime:
    """The Tuesday 00:00 UTC opening the accounting period that holds `moment`."""
    day = datetime(moment.year, moment.month, moment.day)
    return day - timedelta(days=(day.weekday() - 1) % 7)


def period_text(begin: datetime) -> str:
    """`accounting_period` as the weekly marts spell it."""
    return f"{begin:%Y-%m-%d %H:%M:%S} - {begin + timedelta(days=7):%Y-%m-%d %H:%M:%S}"


def auction_times(world: World) -> dict[int, datetime]:
    """When each auction id's deadline fell: auctions, shadows, and the bidless ids the
    window query names, which take the time of the next auction."""
    times = {a.auction_id: a.time for a in world.auctions}
    times.update({s.auction_id: s.time for s in world.shadows})
    ordered = sorted((a.auction_id, a.time) for a in world.auctions)
    for extra in world.extra_window_ids:
        later = [when for auction_id, when in ordered if auction_id > extra]
        times[extra] = later[0] if later else ordered[-1][1]
    return times


def reward_rows(world: World) -> dict[int, dict[str, tuple[Decimal, Decimal]]]:
    """`fct_solver_rewards_per_auction` as served: auction -> solver -> (upper, capped)."""
    rows = {
        a.auction_id: {
            solver: (r.upper if r.upper is not None else Decimal(0),
                     r.capped if r.capped is not None else Decimal(0))
            for solver, r in a.mart.items()
        }
        for a in world.auctions
    }
    rows.update({s.auction_id: dict(s.rewards) for s in world.shadows})
    return rows


def quote_shares(errors: dict[str, float]) -> dict[str, float]:
    """One in-scope order's `quote_reward_share`s: the five smallest errors (ties by
    solver) share it, exact matches split it equally, otherwise by 1 / error²."""
    ranked = sorted(errors.items(), key=lambda item: (item[1], item[0]))[:5]
    shares = dict.fromkeys(errors, 0.0)
    exact = [solver for solver, error in ranked if error == 0]
    if exact:
        for solver in exact:
            shares[solver] = 1.0 / len(exact)
        return shares
    weights = sum(1 / error**2 for _, error in ranked)
    for solver, error in ranked:
        shares[solver] = 1 / error**2 / weights
    return shares


def _quote_errors(rng: random.Random, quoters: list[str]) -> dict[str, float]:
    return {q: 0.0 if rng.random() < 0.1 else round(rng.uniform(1e-4, 5e-2), 5)
            for q in quoters}


def _shadow(rng: random.Random, world: World, auction_id: int, when: datetime,
            bidders: list[str]) -> Shadow:
    winners = rng.sample(bidders, min(len(bidders), rng.randint(1, 2)))
    rewards: dict[str, tuple[Decimal, Decimal]] = {}
    won: dict[str, tuple[int, int]] = {}
    for solver in winners:
        upper = Decimal(rng.randint(0, 4 * 10**15)) * Decimal("0.5")
        roll = rng.random()
        capped = (upper if roll < 0.5 else Decimal(rng.randint(0, int(upper))) if roll < 0.8
                  else -Decimal(rng.randint(0, 10**14)))
        rewards[solver] = (upper, capped)
        executions = rng.randint(1, 4)
        won[solver] = (executions, rng.randint(0, executions))
    relative: list[tuple[str, str, float]] = []
    shares: list[tuple[str, str, float]] = []
    orders = [_hex(rng, 56) for _ in range(rng.randint(0, 3))]
    for uid in orders:
        in_scope = rng.random() < 0.85
        surplus = {s: rng.randint(0, 10**18)
                   for s in rng.sample(bidders, min(len(bidders), rng.randint(1, 4)))}
        total = sum(surplus.values())
        relative += [(s, uid, v / total if in_scope and v and total else 0.0)
                     for s, v in surplus.items()]
        quoters = rng.sample(bidders, min(len(bidders), rng.randint(1, 7)))
        split = (quote_shares(_quote_errors(rng, quoters)) if in_scope
                 else dict.fromkeys(quoters, 0.0))
        shares += [(uid, q, v) for q, v in split.items()]
    world.relative[auction_id] = relative
    world.shares[auction_id] = shares
    world.quote_budget[auction_id] = rng.uniform(0, 1e16) if orders else 0.0
    return Shadow(auction_id, when, rewards, won)


def record_payouts(rng: random.Random, world: World) -> None:
    """The accounting tables behind the rewards paid: per-auction relative surplus and
    quote reward shares — for the window's auctions and for shadow auctions filling the
    rest of its accounting periods and the period after — and the weekly marts computed
    from them. The window's last period and the one after may not be paid out yet."""
    if not world.auctions:
        return
    by_time = [a.auction_id for a in sorted(world.auctions, key=lambda a: a.time)]
    assert by_time == sorted(by_time), "auction ids must rise with time, as on chain"
    start, end = datetime.fromisoformat(world.start), datetime.fromisoformat(world.end)
    bidders = sorted(world.bidding_addresses())

    for auction in world.auctions:
        relative: list[tuple[str, str, float]] = []
        shares: list[tuple[str, str, float]] = []
        # `int_backend_data__trade_with_tx_hash`: the orders of every landed winner.
        executed = [t for s in auction.winners if auction.settlements[s.uid].landed
                    for t in s.trades]
        for trade in executed:
            in_scope = not world.orders[trade.uid].partially_fillable
            surplus: dict[str, int] = {}
            for solution in auction.solutions:
                for t in solution.trades:
                    if t.uid != trade.uid:
                        continue
                    try:
                        value = oracle.user_surplus(t)
                    except Refused:
                        continue
                    surplus[solution.solver] = max(surplus.get(solution.solver, value), value)
            total = sum(surplus.values())
            relative += [(s, trade.uid, v / total if in_scope and v and total else 0.0)
                         for s, v in surplus.items()]
            quoters = rng.sample(bidders, min(len(bidders), rng.randint(1, 7)))
            split = (quote_shares(_quote_errors(rng, quoters)) if in_scope
                     else dict.fromkeys(quoters, 0.0))
            shares += [(trade.uid, q, v) for q, v in split.items()]
        world.relative[auction.auction_id] = relative
        world.shares[auction.auction_id] = shares
        world.quote_budget[auction.auction_id] = rng.uniform(0, 1e16) if executed else 0.0

    after = period_start(end - timedelta(seconds=1)) + timedelta(days=7)
    before_times: list[datetime] = []
    after_times: list[datetime] = []
    begin = period_start(start)
    while begin <= after:
        finish = begin + timedelta(days=7)
        for low, high, into in ((begin, min(finish, start), before_times),
                                (max(begin, end), finish, after_times)):
            if low < high:
                span = (high - low).total_seconds()
                into.extend(low + timedelta(seconds=rng.uniform(0, span - 1))
                            for _ in range(rng.randint(0, 3)))
        begin = finish
    ids = [a.auction_id for a in world.auctions] + world.extra_window_ids
    for i, when in enumerate(sorted(before_times, reverse=True)):
        shadow_id = min(ids) - 100 * (i + 1) - rng.randint(0, 50)
        world.shadows.append(_shadow(rng, world, shadow_id, when, bidders))
    for i, when in enumerate(sorted(after_times)):
        shadow_id = max(ids) + 100 * (i + 1) + rng.randint(0, 50)
        world.shadows.append(_shadow(rng, world, shadow_id, when, bidders))
    world.shadows.sort(key=lambda s: s.auction_id)

    record_weekly_marts(world)
    last = period_text(period_start(end - timedelta(seconds=1)))
    for mart in (world.consistency_mart, world.quote_mart):
        if rng.random() < 0.25:
            mart.pop(last, None)
        if rng.random() < 0.5:
            mart.pop(period_text(after), None)


def record_weekly_marts(world: World) -> None:
    """`fct_consistency_rewards_per_solver_and_accounting_period` and
    `fct_quote_rewards_per_solver_and_accounting_period`, transcribed naively from the
    dbt models. A period with no row has no entry."""
    periods: dict[str, list[int]] = {}
    for auction_id, when in sorted(auction_times(world).items()):
        periods.setdefault(period_text(period_start(when)), []).append(auction_id)
    rewards = reward_rows(world)
    won: dict[int, dict[str, tuple[int, int]]] = {s.auction_id: s.won for s in world.shadows}
    for auction in world.auctions:
        mine: dict[str, tuple[int, int]] = {}
        for s in auction.winners:
            n, ok = mine.get(s.solver, (0, 0))
            settled = auction.settlements[s.uid].in_time
            mine[s.solver] = (n + len(s.trades), ok + (len(s.trades) if settled else 0))
        won[auction.auction_id] = mine
    world.consistency_mart.clear()
    world.quote_mart.clear()
    for label, ids in periods.items():
        budget = sum((upper - capped for a in ids
                      for upper, capped in rewards.get(a, {}).values()), Decimal(0))
        total: dict[str, float] = {}
        executions: dict[str, tuple[int, int]] = {}
        for a in ids:
            for solver, _, rel in world.relative.get(a, []):
                total[solver] = total.get(solver, 0.0) + rel
            for solver, (n, ok) in won.get(a, {}).items():
                had = executions.get(solver, (0, 0))
                executions[solver] = (had[0] + n, had[1] + ok)
        weighted: dict[str, float] = {}
        for solver, t in total.items():
            n, ok = executions.get(solver, (0, 0))
            weighted[solver] = t * (ok / n if n else 0.0)
        whole = sum(weighted.values())
        if total:
            world.consistency_mart[label] = {
                s: (float(budget) * weighted[s] / whole if whole else 0.0, total[s])
                for s in total
            }
        quote_budget = sum(world.quote_budget.get(a, 0.0) for a in ids)
        share: dict[str, float] = {}
        paying: set[str] = set()
        for a in ids:
            for uid, solver, value in world.shares.get(a, []):
                share[solver] = share.get(solver, 0.0) + value
                if value > 0:
                    paying.add(uid)
        if share:
            n_orders = len(paying)
            world.quote_mart[label] = {
                s: (quote_budget * v / n_orders if n_orders else 0.0, quote_budget, n_orders)
                for s, v in share.items()
            }
