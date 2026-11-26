"""Per-order surplus and per-solution valuation in native wei (`arbitrator.rs`).

Invariant: `surplus_native + fee_native == compute_order_score(order)` to the wei.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Literal, NamedTuple

from .fees import EMPTY_FEE_CONTEXT, FeeContext, FeeError, order_fees
from .primitives import Pair, as_erc20, ceil_div, price_in_eth
from .winner_selection import Solution

if TYPE_CHECKING:  # `extract` imports this module, so the runtime import would cycle.
    from .extract import Bid

Side = Literal["sell", "buy"]
Mode = Literal["score", "surplus"]


class ValuationError(Exception):
    """Any order failing discards the whole solution (`arbitrator.rs:155`)."""


@dataclass(frozen=True)
class Order:
    """An order as executed inside one solution (`solution.rs:89`)."""

    uid: str
    sell_token: str
    buy_token: str
    sell_amount: int
    """Limit amount."""
    buy_amount: int
    """Limit amount."""
    executed_sell: int
    executed_buy: int
    side: Side
    partially_fillable: bool = False


class ClearingPrices(NamedTuple):
    sell: int
    buy: int


def custom_prices_from_executed(order: Order) -> ClearingPrices:
    """`arbitrator.rs:505`. The swap is deliberate, as in the Rust."""
    return ClearingPrices(sell=order.executed_buy, buy=order.executed_sell)


def surplus_over(
    order: Order, prices: ClearingPrices, limit_sell: int, limit_buy: int
) -> int:
    """Surplus in the surplus token (`arbitrator.rs:337`). Sell side ceils, buy side
    floors; negative raises `ValuationError`."""
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
    """User surplus in atoms of the surplus token (buy token for sells, sell token for buys)."""
    return surplus_over_limit_price(order, custom_prices_from_executed(order))


def to_native(order: Order, amount_in_surplus_token: int, native_prices: dict[str, int]) -> int:
    """Surplus-token atoms to native wei (`arbitrator.rs:208`); buy orders go through
    their limit ratio first."""
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
    return to_native(order, order_surplus(order), native_prices)


def order_volume_native(order: Order, native_prices: dict[str, int]) -> int:
    """`executed_buy` in native wei, at the same buy-token price as the surplus, so a
    wrong price cancels in surplus/volume."""
    price = native_prices.get(order.buy_token)
    if price is None:
        raise ValuationError(f"order {order.uid}: missing native price for buy token")
    return price_in_eth(price, order.executed_buy)


class OrderFees(NamedTuple):
    """`SolutionValuation`'s fee half."""

    order_fee_native: dict[str, int]
    order_partner_fee_native: dict[str, int]
    pair_fee_native: dict[Pair, int]
    pairs_fees_unknown: frozenset[Pair]
    fees_unknown: frozenset[str]


class FeeInputs(NamedTuple):
    valued: tuple[tuple[Order, int, int], ...]
    """Per order in bid order: the order, its surplus in atoms and in native wei."""
    native_prices: dict[str, int]
    fee_context: FeeContext


@dataclass(frozen=True)
class SolutionValuation:
    pair_surplus: dict[Pair, int]
    """Keyed by raw directed pair."""
    order_surplus_native: dict[str, int]
    winner_pairs: frozenset[Pair]
    """`as_erc20`-normalised: what `pick_winners` claims."""
    order_uids: frozenset[str]
    order_volume_native: dict[str, int] = field(default_factory=dict[str, int])
    fee_inputs: FeeInputs | None = field(default=None, compare=False, repr=False)
    """What the fees are computed from, on first use: only winners and multi-pair score
    splits read them, a small share of the bids. None: nothing to price."""

    @cached_property
    def fees(self) -> OrderFees:
        """Can neither fail nor raise where the surplus did not: an uncomputable fee is
        `fees_unknown`, and `to_native` checks nothing the surplus did not."""
        fee_native: dict[str, int] = {}
        partner_fee_native: dict[str, int] = {}
        pair_fee_native: dict[Pair, int] = {}
        pairs_fees_unknown: set[Pair] = set()
        fees_unknown: set[str] = set()
        valued, prices, context = self.fee_inputs or ((), {}, EMPTY_FEE_CONTEXT)
        for order, surplus_atoms, surplus_native in valued:
            pair = (order.sell_token, order.buy_token)
            try:
                split = order_fees(order, context)
            except FeeError:
                split = None
            if split is None:
                fees_unknown.add(order.uid)
                pairs_fees_unknown.add(pair)
                continue
            # Convert as differences of conversions, so surplus + fee equals the score to
            # the wei and 0 <= dao <= fee (to_native is monotone but floors).
            score_native = to_native(order, surplus_atoms + split.total, prices)
            protocol_native = (
                to_native(order, surplus_atoms + split.protocol, prices) - surplus_native
            )
            fee_native[order.uid] = score_native - surplus_native
            partner_fee_native[order.uid] = score_native - surplus_native - protocol_native
            pair_fee_native[pair] = pair_fee_native.get(pair, 0) + fee_native[order.uid]
        return OrderFees(
            fee_native,
            partner_fee_native,
            pair_fee_native,
            frozenset(pairs_fees_unknown),
            frozenset(fees_unknown),
        )

    @property
    def order_fee_native(self) -> dict[str, int]:
        """Protocol fee including partner fees; orders in `fees_unknown` are absent."""
        return self.fees.order_fee_native

    @property
    def order_partner_fee_native(self) -> dict[str, int]:
        return self.fees.order_partner_fee_native

    @property
    def pair_fee_native(self) -> dict[Pair, int]:
        """Short by an unknown amount on `pairs_fees_unknown`."""
        return self.fees.pair_fee_native

    @property
    def pairs_fees_unknown(self) -> frozenset[Pair]:
        return self.fees.pairs_fees_unknown

    @property
    def fees_unknown(self) -> frozenset[str]:
        """Orders with no recorded policies; empty for every solution the arbitrator kept."""
        return self.fees.fees_unknown

    @property
    def total(self) -> int:
        return sum(self.pair_surplus.values())

    @property
    def fee_native(self) -> int:
        return sum(self.order_fee_native.values())

    @property
    def partner_fee_native(self) -> int:
        return sum(self.order_partner_fee_native.values())

    @property
    def fees_complete(self) -> bool:
        return not self.fees_unknown


def value_solution(
    orders: Iterable[Order],
    native_prices: dict[str, int],
    weth: str,
    fee_context: FeeContext = EMPTY_FEE_CONTEXT,
) -> SolutionValuation:
    """`score_by_token_pair`, `arbitrator.rs:176`.

    Raises `ValuationError`; the caller must discard the whole solution. An
    uncomputable fee is not an error: the order goes to `fees_unknown`. Fees are
    computed when first read (`SolutionValuation.fees`).
    """
    pair_surplus: dict[Pair, int] = {}
    native: dict[str, int] = {}
    volumes: dict[str, int] = {}
    winner_pairs: set[Pair] = set()
    uids: set[str] = set()
    valued: list[tuple[Order, int, int]] = []

    for order in orders:
        uids.add(order.uid)
        winner_pairs.add(
            (as_erc20(order.sell_token, weth), as_erc20(order.buy_token, weth))
        )

        surplus_atoms = order_surplus(order)
        surplus_native = to_native(order, surplus_atoms, native_prices)

        native[order.uid] = surplus_native
        volumes[order.uid] = order_volume_native(order, native_prices)
        pair = (order.sell_token, order.buy_token)
        pair_surplus[pair] = pair_surplus.get(pair, 0) + surplus_native
        valued.append((order, surplus_atoms, surplus_native))

    return SolutionValuation(
        pair_surplus=pair_surplus,
        order_surplus_native=native,
        winner_pairs=frozenset(winner_pairs),
        order_uids=frozenset(uids),
        order_volume_native=volumes,
        fee_inputs=FeeInputs(tuple(valued), native_prices, fee_context),
    )


class PairScores(NamedTuple):
    pairs: dict[Pair, int]
    """Possibly partial."""
    anchor_failed: bool
    """Recomputation contradicted the recorded score; distinct from unrecorded fees."""


def pair_score_split(valuation: SolutionValuation, db_score: int) -> PairScores:
    """Per-pair score (`arbitrator.rs:176`), anchored to the recorded `db_score`.

    Partial by design: the filter is per pair, so dark pairs fall back individually.
    One pair gets `db_score`; priced pairs get surplus + fee; a single dark pair gets the
    residual. Any contradiction with `db_score` returns no pairs and `anchor_failed`.
    """
    pairs = valuation.pair_surplus
    if len(pairs) == 1:
        ((pair, surplus),) = pairs.items()
        # Surplus above the recorded score: our order set disagrees with the autopilot's.
        if surplus > db_score:
            return PairScores({}, anchor_failed=True)
        return PairScores({pair: db_score}, anchor_failed=False)
    if not pairs:
        return PairScores({}, anchor_failed=False)

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

    # Fees >= 0, so priced pairs plus dark pairs' surplus cannot exceed the record.
    dark_floor = sum(pairs[pair] for pair in unknown)
    if priced_total + dark_floor > db_score:
        return PairScores({}, anchor_failed=True)
    if len(unknown) > 1:
        return PairScores(priced, anchor_failed=False)

    (pair,) = unknown
    return PairScores({**priced, pair: db_score - priced_total}, anchor_failed=False)


def solution_total(valuation: SolutionValuation, mode: Mode, db_score: int | None = None) -> int:
    """Score mode: the recorded `proposed_solutions.score`. Surplus mode: summed surplus."""
    if mode == "surplus":
        return valuation.total
    if db_score is None:
        raise ValueError("score mode needs the solution's DB score")
    return db_score


@dataclass(frozen=True)
class ValuedBid:
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
    """Value every bid; failures are left out, as in the Rust, and returned by uid.

    The DB stores only solutions that scored, so a failure is a defect here, never data.
    Bids must arrive in uid order, the arbitrator's tie-break.
    """
    valued: list[ValuedBid] = []
    failures: dict[int, str] = {}

    for bid in bids:
        try:
            valuation = value_solution(bid.orders, native_prices, weth, fee_context)
            total = solution_total(valuation, mode, bid.score)
        except ValuationError as error:
            failures[bid.uid] = str(error)
            continue

        # Surplus mode keeps every pair on the surplus scale: one currency throughout.
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
