"""Per-order surplus and per-solution totals, in native wei.

Mirrors the surplus half of `arbitrator.rs`. The fee half is `fees.py`, and passing a
`FeeContext` here adds the per-order protocol-fee decomposition alongside surplus — the
two are exactly additive, `surplus_native + fee_native == compute_order_score(order)`.

Solution *totals* still come from `proposed_solutions.score` rather than from
`surplus + fees`: the recorded score needs no reconstruction, and the fee path has a
narrow coverage gap the score does not. What the fee decomposition *does* buy is the
per-pair **score** split the fairness filter compares — `pair_score_split`, M14. Where
that split cannot be determined the filter falls back to per-pair surplus, which is what
it compared everywhere before.

Integer arithmetic throughout, except where `fees.py` reproduces the Rust's own `f64`
detours.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, NamedTuple

from .fees import EMPTY_FEE_CONTEXT, FeeContext, FeeError, order_fees
from .primitives import Pair, as_erc20, ceil_div, price_in_eth
from .winner_selection import Solution

if TYPE_CHECKING:  # `extract` imports this module, so the runtime import would cycle.
    from .extract import Bid

Side = Literal["sell", "buy"]
Mode = Literal["score", "surplus"]


class ValuationError(Exception):
    """A per-order valuation failed.

    Mirrors `MathError` plus the missing-native-price case. In the Rust any such
    failure discards the **whole solution**, not just the order (`arbitrator.rs:155`),
    and callers here must do the same.

    Overflow has no Python analogue — ints are unbounded — so only division-by-zero and
    negative surplus can occur.
    """


@dataclass(frozen=True)
class Order:
    """An order as executed inside one solution (`solution.rs:89`)."""

    uid: str
    sell_token: str
    buy_token: str
    sell_amount: int
    """Limit sell amount, from the original order."""
    buy_amount: int
    """Limit buy amount, from the original order."""
    executed_sell: int
    executed_buy: int
    side: Side
    partially_fillable: bool = False
    """Not used by the algorithm; carried for discrepancy triage."""


@dataclass(frozen=True)
class ClearingPrices:
    sell: int
    buy: int


def custom_prices_from_executed(order: Order) -> ClearingPrices:
    """`arbitrator.rs:505`. The swap is in the Rust and is not a typo: the price *of*
    the sell token is expressed in buy-token units."""
    return ClearingPrices(sell=order.executed_buy, buy=order.executed_sell)


def surplus_over(
    order: Order, prices: ClearingPrices, limit_sell: int, limit_buy: int
) -> int:
    """Surplus in the surplus token over arbitrary price limits (`arbitrator.rs:337`).

    Limits are scaled by the executed amount so partial fills work. The sell side uses
    **ceiling** division on both the scaled limit and the bought amount; the buy side
    floors. That asymmetry is the expected source of off-by-one differences.

    Raises `ValuationError` when the result would be negative — the Rust's
    `MathError::Negative`, which propagates up and discards the solution.
    """
    executed = order.executed_buy if order.side == "buy" else order.executed_sell

    if order.side == "buy":
        if limit_buy == 0:
            raise ValuationError(f"order {order.uid}: zero limit buy amount")
        if prices.sell == 0:
            raise ValuationError(f"order {order.uid}: zero sell price")
        scaled_limit_sell = executed * limit_sell // limit_buy
        sold = executed * prices.buy // prices.sell
        surplus = scaled_limit_sell - sold
    else:
        if limit_sell == 0:
            raise ValuationError(f"order {order.uid}: zero limit sell amount")
        if prices.buy == 0:
            raise ValuationError(f"order {order.uid}: zero buy price")
        scaled_limit_buy = ceil_div(executed * limit_buy, limit_sell)
        bought = ceil_div(executed * prices.sell, prices.buy)
        surplus = bought - scaled_limit_buy

    if surplus < 0:
        raise ValuationError(f"order {order.uid}: negative surplus ({surplus})")
    return surplus


def surplus_over_limit_price(order: Order, prices: ClearingPrices) -> int:
    """`arbitrator.rs:325`."""
    return surplus_over(order, prices, order.sell_amount, order.buy_amount)


def order_surplus(order: Order) -> int:
    """User surplus in the surplus token: buy token for sell orders, sell token for buy
    orders. Excludes protocol fees — this is the same quantity as
    `int_backend_data__proposed_solution_data.order_surplus_atoms_in_surplus_token`."""
    return surplus_over_limit_price(order, custom_prices_from_executed(order))


def to_native(order: Order, amount_in_surplus_token: int, native_prices: dict[str, int]) -> int:
    """Convert a surplus-token amount to native, following `compute_order_score`
    (`arbitrator.rs:208`).

    Sell orders are already denominated in the buy token. Buy orders are denominated in
    the sell token and are converted through the order's own limit ratio
    (`buy_amount / sell_amount`) before the native conversion — which is why buy orders
    need limit amounts and sell orders do not.
    """
    price = native_prices.get(order.buy_token)
    if price is None:
        raise ValuationError(f"order {order.uid}: missing native price for buy token")

    if order.side == "sell":
        return price_in_eth(price, amount_in_surplus_token)

    if order.sell_amount == 0:
        raise ValuationError(f"order {order.uid}: zero limit sell amount")
    in_buy_token = amount_in_surplus_token * order.buy_amount // order.sell_amount
    return price_in_eth(price, in_buy_token)


def order_surplus_native(order: Order, native_prices: dict[str, int]) -> int:
    """Per-order user surplus in native wei.

    This is `compute_order_score` with the protocol fee term set to 0.
    """
    return to_native(order, order_surplus(order), native_prices)


def order_volume_native(order: Order, native_prices: dict[str, int]) -> int:
    """Native value of the order's received leg: `executed_buy` at the buy-token price.

    The denominator for relative price statements (Δsurplus / volume). Deliberately
    valued through the same buy-token price `to_native` puts into the numerator, so the
    per-order ratio is exact even when that native price is wrong — a wrong price scales
    surplus and volume equally and cancels. Only the weighting *across* orders still
    trusts the price level, and price-suspect auctions are excluded upstream anyway.
    """
    price = native_prices.get(order.buy_token)
    if price is None:
        raise ValuationError(f"order {order.uid}: missing native price for buy token")
    return price_in_eth(price, order.executed_buy)


@dataclass(frozen=True)
class SolutionValuation:
    """Surplus decomposition of one solution."""

    pair_surplus: dict[Pair, int]
    """Native surplus per **raw** directed token pair, contributing orders only."""
    order_surplus_native: dict[str, int]
    """Native surplus per contributing order."""
    order_surplus_atoms: dict[str, int]
    """Surplus-token surplus per contributing order, for cross-checking against the DB."""
    winner_pairs: frozenset[Pair]
    """`as_erc20`-normalised pairs over **all** orders — what `pick_winners` claims."""
    order_uids: frozenset[str]
    order_volume_native: dict[str, int] = field(default_factory=dict[str, int])
    """Native value of each contributing order's received leg — the denominator for
    relative price statements. Same keys as `order_surplus_native`."""
    order_fee_native: dict[str, int] = field(default_factory=dict[str, int])
    """Native protocol fee per contributing order, **including** partner fees — the
    quantity `arbitrator.rs` adds to surplus to reach the score. Absent for orders whose
    policies are not recorded for this auction; those uids are in `fees_unknown`."""
    order_partner_fee_native: dict[str, int] = field(default_factory=dict[str, int])
    """The partners' share of `order_fee_native`. What the DAO keeps is the difference,
    which is non-negative because the native conversion is monotone."""
    pair_fee_native: dict[Pair, int] = field(default_factory=dict[Pair, int])
    """Native protocol fee per **raw** directed token pair, over the contributing orders
    whose fee could be computed. `pair_surplus + pair_fee_native` is the pair's score —
    on the pairs not in `pairs_fees_unknown`."""
    pairs_fees_unknown: frozenset[Pair] = frozenset()
    """Pairs carrying at least one contributing order with an unrecorded fee, so
    `pair_fee_native` is short for them by an unknown amount. Derived from
    `fees_unknown`; kept separately because the filter works per pair, not per order."""
    fees_unknown: frozenset[str] = frozenset()
    """Contributing orders whose fee could not be computed — their policies are not
    recorded for this auction. Every order of a solution the arbitrator *kept* is
    recorded, so this is only ever non-empty for solutions that were filtered out."""

    @property
    def total(self) -> int:
        return sum(self.pair_surplus.values())

    @property
    def fee_native(self) -> int:
        """Total protocol fee of the solution, partner fees included."""
        return sum(self.order_fee_native.values())

    @property
    def partner_fee_native(self) -> int:
        return sum(self.order_partner_fee_native.values())

    @property
    def protocol_fee_native_net(self) -> int:
        """What the protocol itself keeps."""
        return self.fee_native - self.partner_fee_native

    @property
    def fees_complete(self) -> bool:
        return not self.fees_unknown


def value_solution(
    orders: Iterable[Order],
    contributes: dict[str, bool],
    native_prices: dict[str, int],
    weth: str,
    fee_context: FeeContext = EMPTY_FEE_CONTEXT,
) -> SolutionValuation:
    """Value one solution (`score_by_token_pair`, `arbitrator.rs:176`).

    Orders that do not contribute to score are skipped for valuation but still claim
    their token pair in `winner_pairs` — `pick_winners` iterates every order in the
    solution, not just the contributing ones.

    `fee_context` is optional: with the default every fee reads as unrecorded, and the
    valuation is the surplus-only one this module has always produced. Passing a real
    one adds the protocol-fee decomposition without changing any surplus number.

    Raises `ValuationError` if any contributing order cannot be valued; the caller must
    then discard the entire solution. A fee that cannot be computed is **not** such a
    failure — `arbitrator.rs` would have discarded the solution, but here the total comes
    from the recorded score either way, so the order is recorded in `fees_unknown` and
    the surplus side stays intact.
    """
    pair_surplus: dict[Pair, int] = {}
    native: dict[str, int] = {}
    atoms: dict[str, int] = {}
    volumes: dict[str, int] = {}
    fee_native: dict[str, int] = {}
    partner_fee_native: dict[str, int] = {}
    pair_fee_native: dict[Pair, int] = {}
    pairs_fees_unknown: set[Pair] = set()
    fees_unknown: set[str] = set()
    winner_pairs: set[Pair] = set()
    uids: set[str] = set()

    for order in orders:
        uids.add(order.uid)
        winner_pairs.add(
            (as_erc20(order.sell_token, weth), as_erc20(order.buy_token, weth))
        )

        if not contributes.get(order.uid, False):
            continue

        surplus_atoms = order_surplus(order)
        surplus_native = to_native(order, surplus_atoms, native_prices)

        atoms[order.uid] = surplus_atoms
        native[order.uid] = surplus_native
        volumes[order.uid] = order_volume_native(order, native_prices)
        pair = (order.sell_token, order.buy_token)
        pair_surplus[pair] = pair_surplus.get(pair, 0) + surplus_native

        try:
            split = order_fees(order, fee_context)
        except FeeError:
            split = None
        if split is None:
            fees_unknown.add(order.uid)
            pairs_fees_unknown.add(pair)
        else:
            # Each share is converted as a *difference of conversions* rather than on
            # its own, so the decomposition is exactly additive:
            #
            #     surplus_native + fee_native == compute_order_score(order)
            #
            # to the wei, and `0 <= dao <= fee` follows from `to_native` being monotone.
            # Converting the fee directly instead loses up to one surplus-token atom per
            # order to the intermediate floor in `to_native` — on a six-decimal token
            # that is ~5e8 wei, small but not nothing, and it would put a systematic
            # gap between this decomposition and the recorded score.
            score_native = to_native(order, surplus_atoms + split.total, native_prices)
            protocol_native = (
                to_native(order, surplus_atoms + split.protocol, native_prices) - surplus_native
            )
            fee_native[order.uid] = score_native - surplus_native
            partner_fee_native[order.uid] = score_native - surplus_native - protocol_native
            pair_fee_native[pair] = pair_fee_native.get(pair, 0) + fee_native[order.uid]

    return SolutionValuation(
        pair_surplus=pair_surplus,
        order_surplus_native=native,
        order_surplus_atoms=atoms,
        winner_pairs=frozenset(winner_pairs),
        order_uids=frozenset(uids),
        order_volume_native=volumes,
        order_fee_native=fee_native,
        order_partner_fee_native=partner_fee_native,
        pair_fee_native=pair_fee_native,
        pairs_fees_unknown=frozenset(pairs_fees_unknown),
        fees_unknown=frozenset(fees_unknown),
    )


class PairScores(NamedTuple):
    """The outcome of splitting one solution's score across its pairs."""

    pairs: dict[Pair, int]
    """Score per pair, for the pairs where it is known. Possibly empty, possibly partial."""
    anchor_failed: bool
    """The recomputation contradicted the recorded score, so nothing was trusted. A
    different thing entirely from a pair's policies being unrecorded, and reported
    separately: this one says the fee path is wrong here, and if it ever becomes common on
    some chain it must not read as that chain having thin fee coverage."""


def pair_score_split(valuation: SolutionValuation, db_score: int) -> PairScores:
    """The solution's score per directed token pair, for the pairs where it is known.

    This is the quantity the fairness filter compares (`score_by_token_pair`,
    `arbitrator.rs:176`), and the one thing the DB does not record: scores are stored per
    solution. Recomputing protocol fees supplies it — a pair's score is its user surplus
    plus the fees of the orders on it — and the recorded score anchors it, so what comes
    back is never a reconstruction floating free of the record.

    The result is **partial by design**. The fairness test is a conjunction of independent
    per-pair comparisons, so a pair needs an exact score only if it has a baseline to
    clear; knowing three pairs out of four is worth exactly as much as knowing four when
    the fourth has no baseline. Returning what is known, pair by pair, lets the filter
    decide each comparison as well as it can (`winner_selection.is_fair`) instead of
    discarding a decidable solution because some unrelated pair is dark.

    How each pair lands, in order of how much it leans on the recomputation:

    1. **One pair in the solution.** The whole score sits on it. Exact from the record
       alone, no fee needed. (Baselines do not come through here — they read `total`
       directly, `winner_selection.compute_baseline_scores` — so this case only keeps the
       `sum(pairs) == db_score` invariant true for every solution.)
    2. **A priced pair.** `surplus + fee`, both of which are the protocol's own
       per-order quantities. Where *every* pair is priced the sum must return the
       recorded score to the wei; it does for every solution the arbitrator kept, whose
       orders' policies are all recorded by construction (M12/M13).
    3. **The last unpriced pair.** With exactly one pair dark the residual
       `db_score - sum(priced pairs)` is its score exactly.

    Every branch is checked against the record, and every check fails the same way — an
    `anchor_failed` result with no pairs at all, because a solution whose arithmetic
    contradicts its recorded score has no trustworthy pair in it. Protocol fees are
    non-negative, so each check is some form of "the parts cannot exceed the whole, and no
    part can fall below its own surplus".
    """
    pairs = valuation.pair_surplus
    if len(pairs) <= 1:
        # Cheap, but not free: a single pair whose surplus already exceeds the recorded
        # score means our contributing-order set disagrees with the autopilot's, and this
        # is the one branch with an invariant to check rather than an arithmetic one.
        if any(surplus > db_score for surplus in pairs.values()):
            return PairScores({}, anchor_failed=True)
        return PairScores({pair: db_score for pair in pairs}, anchor_failed=False)

    unknown = valuation.pairs_fees_unknown
    priced = {
        pair: surplus + valuation.pair_fee_native.get(pair, 0)
        for pair, surplus in pairs.items()
        if pair not in unknown
    }
    priced_total = sum(priced.values())

    if not unknown:
        if priced_total != db_score:
            return PairScores({}, anchor_failed=True)
        return PairScores(priced, anchor_failed=False)

    # Fees are non-negative, so the priced pairs plus the dark pairs' own surplus cannot
    # exceed the recorded total. This is the residual bound with every dark pair held at
    # its floor, and it is the only check available once more than one pair is dark.
    dark_floor = sum(pairs[pair] for pair in unknown)
    if priced_total + dark_floor > db_score:
        return PairScores({}, anchor_failed=True)
    if len(unknown) > 1:
        return PairScores(priced, anchor_failed=False)

    (pair,) = unknown
    return PairScores({**priced, pair: db_score - priced_total}, anchor_failed=False)


def solution_total(valuation: SolutionValuation, mode: Mode, db_score: int | None = None) -> int:
    """The value that ranks a solution and feeds reference scores.

    Score mode takes it from `proposed_solutions.score`, so protocol fees are included
    without having to be recomputed. Surplus mode sums the surplus decomposition instead,
    answering the user-value question self-consistently.

    The per-pair values the fairness filter compares are `pair_score_split` in score
    mode and `valuation.pair_surplus` in surplus mode — see `build_solutions`.
    """
    if mode == "surplus":
        return valuation.total
    if db_score is None:
        raise ValueError("score mode needs the solution's DB score")
    return db_score


@dataclass(frozen=True)
class ValuedBid:
    """A bid, its valuation, and the `Solution` the arbitrator sees."""

    bid: Bid
    solution: Solution
    valuation: SolutionValuation


def build_solutions(
    bids: Sequence[Bid],
    native_prices: dict[str, int],
    weth: str,
    *,
    mode: Mode = "score",
    fee_context: FeeContext = EMPTY_FEE_CONTEXT,
) -> tuple[list[ValuedBid], dict[int, str]]:
    """Value every bid in an auction, in `bid` order.

    Returns the valued bids and the per-uid valuation failures. A bid whose valuation
    fails is left out entirely, which is what the Rust does (`arbitrator.rs:155`
    discards the whole solution when any contributing order cannot be scored). Because
    the DB only ever stores solutions whose score computation succeeded, any failure
    here is a defect in this reproduction rather than a property of the data, and every
    caller reports rather than swallows it.

    Bids arrive in `uid` order, which is best-to-worst, so ties in `total` break the way
    the autopilot recorded them — see docs/analytics-db.md.
    """
    valued: list[ValuedBid] = []
    failures: dict[int, str] = {}

    for bid in bids:
        try:
            valuation = value_solution(
                bid.orders, bid.contributes, native_prices, weth, fee_context
            )
            total = solution_total(valuation, mode, bid.score)
        except ValuationError as error:
            failures[bid.uid] = str(error)
            continue

        # Surplus mode deliberately gets no score split: its whole point is to answer
        # the user-value question in one currency, and a filter comparing scores inside
        # a surplus ranking would be neither of the two answers. An empty map leaves
        # every pair on the surplus scale, which is that answer.
        scores = (
            pair_score_split(valuation, bid.score)
            if mode == "score"
            else PairScores({}, anchor_failed=False)
        )

        valued.append(
            ValuedBid(
                bid=bid,
                solution=Solution(
                    solver=bid.solver,
                    solution_uid=bid.uid,
                    total=total,
                    pair_values=dict(valuation.pair_surplus),
                    pair_scores=scores.pairs,
                    score_anchor_failed=scores.anchor_failed,
                    order_uids=valuation.order_uids,
                    winner_pairs=valuation.winner_pairs,
                ),
                valuation=valuation,
            )
        )

    return valued, failures
