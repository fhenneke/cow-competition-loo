"""Per-order surplus and per-solution totals, in native wei.

Mirrors the surplus half of `arbitrator.rs`. Protocol fees are deliberately *not*
reimplemented — they cannot be recomputed for orders that were never executed, because
`stg_backend_data__fee_policies` only has rows for executed orders (see
docs/analytics-db.md). Score therefore comes from `proposed_solutions.score`, and this
module supplies user surplus plus the per-pair decomposition the fairness filter needs.

Integer arithmetic throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from .primitives import Pair, as_erc20, ceil_div, price_in_eth

Side = Literal["sell", "buy"]
Mode = Literal["score", "surplus"]
PairProxy = Literal["surplus", "scaled", "raw"]


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

    @property
    def total(self) -> int:
        return sum(self.pair_surplus.values())


def value_solution(
    orders: Iterable[Order],
    contributes: dict[str, bool],
    native_prices: dict[str, int],
    weth: str,
) -> SolutionValuation:
    """Value one solution (`score_by_token_pair`, `arbitrator.rs:176`).

    Orders that do not contribute to score are skipped for valuation but still claim
    their token pair in `winner_pairs` — `pick_winners` iterates every order in the
    solution, not just the contributing ones.

    Raises `ValuationError` if any contributing order cannot be valued; the caller must
    then discard the entire solution.
    """
    pair_surplus: dict[Pair, int] = {}
    native: dict[str, int] = {}
    atoms: dict[str, int] = {}
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
        pair = (order.sell_token, order.buy_token)
        pair_surplus[pair] = pair_surplus.get(pair, 0) + surplus_native

    return SolutionValuation(
        pair_surplus=pair_surplus,
        order_surplus_native=native,
        order_surplus_atoms=atoms,
        winner_pairs=frozenset(winner_pairs),
        order_uids=frozenset(uids),
    )


@dataclass(frozen=True)
class PairValues:
    """Per-pair values handed to the fairness filter, plus how they were derived."""

    values: dict[Pair, int]
    total: int
    basis: str
    """One of `surplus-mode`, `surplus`, `exact`, `scaled`, `raw`, `empty`. See
    `pair_values_for_mode`."""


def pair_values_for_mode(
    valuation: SolutionValuation,
    mode: Mode,
    db_score: int | None = None,
    pair_proxy: PairProxy = "surplus",
) -> PairValues:
    """Turn a surplus decomposition into the `(total, pair_values)` the filter compares.

    `total` ranks solutions and feeds reference scores; `values` is used by nothing except
    the fairness filter. The two are allowed to disagree, and in score mode they
    deliberately do.

    Surplus mode is self-consistent: both come straight from the surplus decomposition.

    Score mode takes the total from `proposed_solutions.score`. The per-pair split it
    would need for the filter is not recorded anywhere — scores are stored per solution —
    so `pair_proxy` chooses what to compare instead:

    - **surplus** (default) — value every pair, on both sides of the comparison, by native
      user surplus. A batched solution's surplus on a pair is compared against the best
      surplus any single-pair solution achieved on that pair. Nothing is invented and both
      sides are in the same units; the cost is that it answers the fairness question about
      user surplus rather than about score.
    - **scaled** — split the score across pairs in proportion to surplus, leaving
      single-pair solutions exact. Both sides land on the score scale, so this is the
      closest available model of what the protocol actually compares, at the price of
      assuming fees follow surplus across a batch's pairs.
    - **raw** — surplus on the left, exact score baselines on the right, as PLAN.md §2
      originally specified. Mixes fee-exclusive and fee-inclusive quantities and so
      systematically over-filters; kept to measure exactly that cost.

    `scaled` and `raw` are *score-consistent*: their pair values are always a valid
    candidate split of the recorded score, which is what lets `validate.filter_bracket`
    treat any decisive disagreement as a bug. `surplus` is not, and its disagreements with
    the recorded filter are a modelling difference rather than a defect.
    """
    if mode == "surplus":
        return PairValues(dict(valuation.pair_surplus), valuation.total, "surplus-mode")

    if db_score is None:
        raise ValueError("score mode needs the solution's DB score")

    if pair_proxy == "surplus":
        # Deliberately ahead of the single-pair shortcut: baselines come only from
        # single-pair solutions, so they have to be surplus too for both sides of the
        # comparison to be in surplus terms.
        return PairValues(dict(valuation.pair_surplus), db_score, "surplus")

    if not valuation.pair_surplus:
        # No contributing orders, yet the DB recorded a positive score. The Rust would
        # have dropped this solution before ranking, so it should be unreachable;
        # surfaced as its own basis so validation can count it rather than hide it.
        return PairValues({}, db_score, "empty")

    if len(valuation.pair_surplus) == 1:
        ((pair, _),) = valuation.pair_surplus.items()
        return PairValues({pair: db_score}, db_score, "exact")

    surplus_total = valuation.total
    if pair_proxy == "scaled" and surplus_total > 0:
        values = {
            pair: db_score * value // surplus_total
            for pair, value in valuation.pair_surplus.items()
        }
        return PairValues(values, db_score, "scaled")

    return PairValues(dict(valuation.pair_surplus), db_score, "raw")
