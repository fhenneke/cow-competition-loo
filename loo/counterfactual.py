"""Re-run one auction with every `removed` solver absent at once, and diff the outcomes.

A full re-arbitration, fairness filter included: removing a solver can lower a pair
baseline and un-filter a rival (docs/winner-selection.md#reference-scores-do-not-re-filter)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import cached_property
from typing import Any, Literal, NamedTuple

from .extract import AuctionBundle, Bid, Settlement, SolutionCap
from .fees import FeeContext, Quote
from .primitives import MAX_WINNERS, Pair, price_in_eth
from .quotes import shifted_limit
from .rewards import SolverReward, Win, uncapped_rewards
from .valuation import Mode, Order, ValuedBid, build_solutions
from .winner_selection import (
    Arbitration,
    Ranking,
    ReferenceScore,
    Solution,
    arbitrate,
    filter_guess_could_matter,
)

PRICE_IMBALANCE_THRESHOLD = 2
"""Sell- vs buy-side native value ratio above which a solution's prices are suspect
(docs/analytics-db.md#native-prices-can-be-plain-wrong)."""

OutcomeRule = Literal["inherited", "assume-settled"]

OUTCOME_RULE = {
    "inherited": (
        "settlement belongs to the slot rather than to the solver holding it: a winner "
        "that also won for real takes its own recorded outcome, and a replacement "
        "inherits the outcome of the recorded winner(s) that held its token pairs — so "
        "if the batch that really occupied those pairs reverted, the replacement reverts "
        "too"
    ),
    "assume-settled": (
        "every winning solution on both sides is assumed to settle in time and execute "
        "its proposed amounts, so settlement risk is excluded from the comparison "
        "entirely"
    ),
}
"""Applied identically to both sides, so unchanged winners cancel; the rules differ only
for replacements, winners with no record of their own."""


class MissingSettlementError(Exception):
    """A recorded winner has no settlement outcome; any default would bias surplus."""


class RelaxationError(Exception):
    """Removal un-kept another solver's solution; baselines can only fall, so this is a bug."""


def leave_one_out(
    solutions: Iterable[Solution],
    removed: frozenset[str],
    max_winners: int = MAX_WINNERS,
) -> Ranking:
    """`arbitrate` without every solution of `removed` (`arbitrator.rs:620`)."""
    return arbitrate([s for s in solutions if s.solver not in removed], max_winners)


def un_filtered(baseline: Ranking, loo: Ranking, removed: frozenset[str]) -> frozenset[int]:
    """Solutions filtered in the baseline but kept without `removed`."""
    baseline_kept = {s.solution_uid for s in baseline.ranked}
    loo_kept = {s.solution_uid for s in loo.ranked}
    survivors = {
        s.solution_uid
        for s in baseline.ranked + baseline.filtered_out
        if s.solver not in removed
    }

    lost = (baseline_kept & survivors) - loo_kept
    if lost:
        raise RelaxationError(
            f"solutions {sorted(lost)} were kept in the baseline and filtered out once "
            f"{sorted(removed)} was removed; baselines can only fall"
        )

    return frozenset(loo_kept - baseline_kept)


def reference_margin(winning_score: int, reference_score: int) -> int:
    """A winner's margin over its reference score, `winning_score − min(winning_score,
    reference_score)`: what it earns before caps if it settles (docs/rewards.md)."""
    return winning_score - min(winning_score, reference_score)


def _winning_uids_by_solver(ranking: Ranking) -> dict[str, frozenset[int]]:
    by_solver: dict[str, set[int]] = {}
    for solution in ranking.winners:
        by_solver.setdefault(solution.solver, set()).add(solution.solution_uid)
    return {solver: frozenset(uids) for solver, uids in by_solver.items()}


def pressed_winners(
    baseline: Ranking, loo: Ranking, removed: frozenset[str]
) -> dict[str, frozenset[int]]:
    """Solvers outside `removed` winning the same solutions on both sides, with those
    solutions. One that wins other solutions without `removed` took part of a vacated
    slot, which belongs to Δrewards, not to the margin a runner-up left it."""
    base, counterfactual = _winning_uids_by_solver(baseline), _winning_uids_by_solver(loo)
    return {
        solver: uids
        for solver, uids in base.items()
        if solver not in removed and counterfactual.get(solver) == uids
    }


def runner_up_uids(
    baseline: Ranking, references: Mapping[str, ReferenceScore], removed: frozenset[str]
) -> frozenset[int]:
    """Removed solvers' non-winning solutions in the reference pick of a winner outside
    `removed`. By uid: a removed solver can win one pair and be runner-up on another."""
    losing = {
        s.solution_uid
        for s in baseline.ranked
        if s.solver in removed and s.solution_uid not in baseline.winner_uids
    }
    return frozenset(
        uid
        for solver, reference in references.items()
        if solver not in removed
        for uid in reference.setter_uids & losing
    )


@dataclass(frozen=True)
class OrderOutcome:
    executed: bool
    surplus_native: int | None
    """None when not executed; 0 is a real surplus."""
    solution_uid: int | None = None
    solver: str | None = None
    observed: bool = False
    """From the recorded competition rather than a proposal."""
    landed_late: bool = False
    """Filled after the deadline, so counted as a failure."""
    volume_native: int | None = None
    """Native value of the received leg; None when not executed."""
    partially_fillable: bool = False
    fee_native: int | None = None
    """Protocol fee incl. partner fees; None if unexecuted or unpriced (`fees_known`)."""
    partner_fee_native: int | None = None
    fees_known: bool = True
    """False only when executed with unrecorded policies; None is not a zero fee."""


UNEXECUTED = OrderOutcome(executed=False, surplus_native=None)


def _imbalance(sold: int, bought: int, threshold: int) -> Literal["sell", "buy", "ok"] | None:
    """Which of two native values is worth more than `threshold` times the other; None
    when either is worth nothing."""
    if not sold or not bought:
        return None
    if sold > threshold * bought:
        return "sell"
    if bought > threshold * sold:
        return "buy"
    return "ok"


def price_imbalanced(
    bid: Bid, prices: Mapping[str, int], threshold: int = PRICE_IMBALANCE_THRESHOLD
) -> bool:
    """Do the sell and buy sides' native values differ by more than `threshold`?"""
    sell_value = buy_value = 0
    for order in bid.orders:
        sell_price = prices.get(order.sell_token)
        buy_price = prices.get(order.buy_token)
        if sell_price is None or buy_price is None:
            continue
        sell_value += price_in_eth(sell_price, order.executed_sell)
        buy_value += price_in_eth(buy_price, order.executed_buy)
    return _imbalance(sell_value, buy_value, threshold) in ("sell", "buy")


def _order_imbalance(
    order: Order, prices: Mapping[str, int], threshold: int, *, limit: bool = False
) -> Literal["sell", "buy", "ok"] | None:
    """Which side of one fill (or, with `limit`, of the signed limit) is worth more than
    `threshold` times the other; None when a side is unpriced or worth nothing."""
    sell_price = prices.get(order.sell_token)
    buy_price = prices.get(order.buy_token)
    if sell_price is None or buy_price is None:
        return None
    sold = price_in_eth(sell_price, order.sell_amount if limit else order.executed_sell)
    bought = price_in_eth(buy_price, order.buy_amount if limit else order.executed_buy)
    return _imbalance(sold, bought, threshold)


def price_suspects(
    bids: Sequence[Bid], prices: Mapping[str, int], threshold: int = PRICE_IMBALANCE_THRESHOLD
) -> frozenset[int]:
    """Solutions whose prices are suspect: `price_imbalanced`, or filling an order whose
    every fill and whose own limit are valued past `threshold` the same way round.

    The second test catches a wrong price that cancels inside one solution, where a CoW
    buys the mispriced token for one order and sells it for another (polygon auction
    41425733, ~3,500x: one order's limit sells 40M POL-worth for 902 POL). A fill alone
    is not enough, because a bid can overpay one order of a balanced CoW at correct
    prices, often as the order's only fill (mainnet 14273947, xdai 17840083); a limit
    no user would sign is what says the price, not the bid, is wrong."""
    sides: dict[str, set[str]] = {}
    limits: dict[str, str | None] = {}
    imbalanced: set[int] = set()
    for bid in bids:
        # One pass per fill for both tests: its own imbalance, and the sums that
        # `price_imbalanced` compares.
        sell_value = buy_value = 0
        for order in bid.orders:
            if order.uid not in limits:  # the order's own, the same in every bid
                limits[order.uid] = _order_imbalance(order, prices, threshold, limit=True)
            sell_price = prices.get(order.sell_token)
            buy_price = prices.get(order.buy_token)
            if sell_price is None or buy_price is None:
                continue
            sold = price_in_eth(sell_price, order.executed_sell)
            bought = price_in_eth(buy_price, order.executed_buy)
            sell_value += sold
            buy_value += bought
            side = _imbalance(sold, bought, threshold)
            if side is not None:
                sides.setdefault(order.uid, set()).add(side)
        if _imbalance(sell_value, buy_value, threshold) in ("sell", "buy"):
            imbalanced.add(bid.uid)
    mispriced = {
        uid for uid, seen in sides.items() if seen in ({"sell"}, {"buy"}) and {limits[uid]} == seen
    }
    if not mispriced:
        return frozenset(imbalanced)
    return frozenset(
        bid.uid
        for bid in bids
        if bid.uid in imbalanced or any(order.uid in mispriced for order in bid.orders)
    )


def recorded_executed_volume(
    valued: Iterable[ValuedBid],
    db_winner_uids: frozenset[int],
    *,
    outcome_rule: OutcomeRule,
    settled: Mapping[int, Settlement],
) -> tuple[int, int, int]:
    """(fill-or-kill volume, fill-or-kill orders, all orders) the **recorded** winners
    executed under the outcome rule. Read off the record, not our baseline, so it exists
    for auctions the counterfactual skips."""
    volume = orders = orders_all = 0
    for entry in valued:
        uid = entry.bid.uid
        if uid not in db_winner_uids:
            continue
        if outcome_rule == "inherited":
            outcome = settled.get(uid)
            if outcome is None:
                raise MissingSettlementError(
                    f"auction {entry.bid.auction_id} solution {uid} won the recorded "
                    f"competition but has no settlement outcome; the settlement source "
                    f"does not cover this auction. Narrow the window or use "
                    f"--outcome-rule assume-settled."
                )
            if not outcome.counts_as_executed:
                continue
        for order in entry.bid.orders:
            order_volume = entry.valuation.order_volume_native[order.uid]
            orders_all += 1
            if order.partially_fillable:
                continue
            volume += order_volume
            orders += 1
    return volume, orders, orders_all


def recorded_settlement_by_pair(
    by_uid: Mapping[int, ValuedBid],
    recorded_winner_uids: frozenset[int],
    settled: Mapping[int, Settlement],
) -> dict[Pair, bool]:
    """Did the recorded batch holding each `winner_pairs` pair settle?"""
    by_pair: dict[Pair, bool] = {}
    for uid in sorted(recorded_winner_uids):
        entry = by_uid.get(uid)
        if entry is None:
            continue
        if uid not in settled:
            raise MissingSettlementError(
                f"auction {entry.bid.auction_id} solution {uid} won the recorded "
                f"competition but has no settlement outcome; the settlement source does "
                f"not cover this auction. Narrow the window or use --outcome-rule assume-settled."
            )
        for pair in entry.solution.winner_pairs:
            by_pair[pair] = settled[uid].counts_as_executed
    return by_pair


def recorded_caps_by_pair(
    by_uid: Mapping[int, ValuedBid],
    recorded_winner_uids: frozenset[int],
    caps: Mapping[int, SolutionCap],
) -> dict[Pair, tuple[int, SolutionCap]]:
    """Which recorded winner held each token pair, and its caps."""
    by_pair: dict[Pair, tuple[int, SolutionCap]] = {}
    for uid in sorted(recorded_winner_uids):
        entry = by_uid.get(uid)
        if entry is None or uid not in caps:
            continue
        for pair in entry.solution.winner_pairs:
            by_pair[pair] = (uid, caps[uid])
    return by_pair


@dataclass(frozen=True)
class SideCaps:
    """Per-winner upper-cap contribution and penalty cap for one side."""

    by_uid: dict[int, Decimal | None]
    """None: a replacement with nothing to inherit."""
    penalty_by_uid: dict[int, Decimal | None]
    """None: no cap (flat floor applies), not unknown."""
    double_inherited: int
    """Displaced winners whose cap more than one replacement inherited."""
    orphans: frozenset[int]


def winner_caps(
    winners: Iterable[ValuedBid],
    recorded_winner_uids: frozenset[int],
    caps: Mapping[int, SolutionCap],
    cap_by_pair: Mapping[Pair, tuple[int, SolutionCap]],
) -> SideCaps:
    """Own recorded caps for real winners; the displaced slots' summed caps for replacements."""
    by_uid: dict[int, Decimal | None] = {}
    penalty_by_uid: dict[int, Decimal | None] = {}
    orphans: set[int] = set()
    claimed: dict[int, int] = {}

    for entry in winners:
        uid = entry.bid.uid
        if uid in recorded_winner_uids and uid in caps:
            by_uid[uid] = caps[uid].upper
            penalty_by_uid[uid] = caps[uid].penalty
            continue
        displaced: dict[int, SolutionCap] = {}
        for pair in entry.solution.winner_pairs:
            found = cap_by_pair.get(pair)
            if found is not None:
                displaced_uid, cap = found
                displaced[displaced_uid] = cap
        if not displaced:
            by_uid[uid] = None
            penalty_by_uid[uid] = None
            orphans.add(uid)
            continue
        by_uid[uid] = sum((cap.upper for cap in displaced.values()), Decimal(0))
        penalty_by_uid[uid] = _summed_penalty(displaced.values())
        for displaced_uid in displaced:
            claimed[displaced_uid] = claimed.get(displaced_uid, 0) + 1

    return SideCaps(
        by_uid=by_uid,
        penalty_by_uid=penalty_by_uid,
        double_inherited=sum(1 for count in claimed.values() if count > 1),
        orphans=frozenset(orphans),
    )


def _summed_penalty(displaced: Iterable[SolutionCap]) -> Decimal | None:
    """None if any displaced slot had no cap, as the mart's every-solution-has-a-cap test."""
    total = Decimal(0)
    for cap in displaced:
        if cap.penalty is None:
            return None
        total += cap.penalty
    return total


@dataclass(frozen=True)
class SideOutcomes:
    orders: dict[str, OrderOutcome]
    replacements: frozenset[int]
    """Winners that never won for real."""
    inherited_reverts: frozenset[int]
    orphans: frozenset[int]
    """Replacements holding no pair a recorded winner held; settlement assumed."""
    solution_executed: dict[int, bool] = field(default_factory=dict[int, bool])
    """Per-winner settlement decision; also feeds `observed_score`, so surplus and rewards
    agree on who delivered."""


def side_outcomes(
    winners: Iterable[ValuedBid],
    *,
    outcome_rule: OutcomeRule,
    settled: Mapping[int, Settlement],
    recorded_winner_uids: frozenset[int],
    inherit: Mapping[Pair, bool] | None = None,
) -> SideOutcomes:
    """Outcome per order for one side, per `OUTCOME_RULE`.

    Pass both sides the same `inherit` map. Executed means landed in time
    (`Settlement.counts_as_executed`)."""
    outcomes: dict[str, OrderOutcome] = {}
    replacements: set[int] = set()
    inherited_reverts: set[int] = set()
    orphans: set[int] = set()
    solution_executed: dict[int, bool] = {}
    inherit = dict(inherit) if inherit else {}

    for entry in winners:
        uid = entry.bid.uid
        recorded = uid in recorded_winner_uids
        if not recorded:
            replacements.add(uid)

        late = False
        if outcome_rule == "assume-settled":
            executed, observed = True, False
        elif recorded:
            if uid not in settled:
                raise MissingSettlementError(
                    f"auction {entry.bid.auction_id} solution {uid} won the recorded "
                    f"competition but has no settlement outcome; the settlement source "
                    f"does not cover this auction. Narrow the window or use "
                    f"--outcome-rule assume-settled."
                )
            outcome = settled[uid]
            executed, observed = outcome.counts_as_executed, True
            late = outcome.landed and not outcome.in_time
        else:
            # `inherited`: a batch spanning several slots settles only if all of them did.
            displaced = [inherit[p] for p in entry.solution.winner_pairs if p in inherit]
            executed, observed = (all(displaced) if displaced else True), False
            if not displaced:
                orphans.add(uid)
            elif not executed:
                inherited_reverts.add(uid)

        solution_executed[uid] = executed

        for order in entry.bid.orders:
            if order.uid in outcomes:
                raise AssertionError(
                    f"order {order.uid} is claimed twice, by solutions "
                    f"{outcomes[order.uid].solution_uid} and {uid}: winners are supposed "
                    f"to hold disjoint token pairs, and an order fixes its own pair"
                )
            outcomes[order.uid] = OrderOutcome(
                executed=executed,
                surplus_native=(
                    entry.valuation.order_surplus_native.get(order.uid, 0)
                    if executed
                    else None
                ),
                solution_uid=uid,
                solver=entry.bid.solver,
                observed=observed,
                landed_late=late,
                volume_native=(
                    entry.valuation.order_volume_native.get(order.uid, 0)
                    if executed
                    else None
                ),
                partially_fillable=order.partially_fillable,
                fee_native=(
                    entry.valuation.order_fee_native.get(order.uid, 0)
                    if executed and order.uid not in entry.valuation.fees_unknown
                    else None
                ),
                partner_fee_native=(
                    entry.valuation.order_partner_fee_native.get(order.uid, 0)
                    if executed and order.uid not in entry.valuation.fees_unknown
                    else None
                ),
                fees_known=(
                    not executed or order.uid not in entry.valuation.fees_unknown
                ),
            )

    return SideOutcomes(
        orders=outcomes,
        replacements=frozenset(replacements),
        inherited_reverts=frozenset(inherited_reverts),
        orphans=frozenset(orphans),
        solution_executed=solution_executed,
    )


@dataclass(frozen=True)
class OrderDiff:
    order_uid: str
    executed_base: bool
    executed_loo: bool
    surplus_base: int | None
    surplus_loo: int | None
    solver_base: str | None = None
    solver_loo: str | None = None
    observed_base: bool = False
    """Outcome came from this solution's own record, not inherited or assumed."""

    late_base: bool = False
    """Filled after the deadline but carried as a failure: surplus knowingly discarded."""

    volume_base: int | None = None
    """Native received-leg value; `delta_surplus / volume_base` is the relative price change."""
    partially_fillable: bool = False
    """Excluded from relative-price stats: sides may execute different amounts."""

    fee_base: int | None = None
    fee_loo: int | None = None
    partner_fee_base: int | None = None
    partner_fee_loo: int | None = None
    """None when that side did not execute the order or could not price its fee."""
    fees_known_base: bool = True
    fees_known_loo: bool = True
    """False: executed but policies unrecorded; the fee delta is unavailable, not zero."""

    @property
    def fees_known(self) -> bool:
        return self.fees_known_base and self.fees_known_loo

    @property
    def unsettled_base(self) -> bool:
        return self.observed_base and not self.executed_base

    @property
    def delta_surplus(self) -> int:
        """Counterfactual minus actual; unexecuted counts as 0."""
        return (self.surplus_loo or 0) - (self.surplus_base or 0)

    @property
    def only_with_solver(self) -> bool:
        return self.executed_base and not self.executed_loo

    @property
    def only_without_solver(self) -> bool:
        """Not a defect: removing a rival that held one pair can unblock a whole batch."""
        return self.executed_loo and not self.executed_base


def diff_outcomes(
    base: Mapping[str, OrderOutcome], loo: Mapping[str, OrderOutcome]
) -> tuple[OrderDiff, ...]:
    diffs: list[OrderDiff] = []
    for order_uid in sorted(set(base) | set(loo)):
        left = base.get(order_uid, UNEXECUTED)
        right = loo.get(order_uid, UNEXECUTED)
        diffs.append(
            OrderDiff(
                order_uid=order_uid,
                executed_base=left.executed,
                executed_loo=right.executed,
                surplus_base=left.surplus_native,
                surplus_loo=right.surplus_native,
                solver_base=left.solver,
                solver_loo=right.solver,
                observed_base=left.observed,
                late_base=left.landed_late,
                volume_base=left.volume_native,
                partially_fillable=left.partially_fillable or right.partially_fillable,
                fee_base=left.fee_native,
                fee_loo=right.fee_native,
                partner_fee_base=left.partner_fee_native,
                partner_fee_loo=right.partner_fee_native,
                fees_known_base=left.fees_known,
                fees_known_loo=right.fees_known,
            )
        )
    return tuple(diffs)


class DiffTotals(NamedTuple):
    """Sums and counts over one auction's `OrderDiff`s, in one pass."""

    surplus_base: int
    surplus_loo: int
    fees_base: int
    fees_loo: int
    partner_fees_base: int
    partner_fees_loo: int
    fees_complete: bool
    only_with_solver: int
    only_without_solver: int
    unsettled_base: int
    late_base: int


class SideTotals(NamedTuple):
    """One side's margin and reward sums, computed together."""

    reference_margin: int
    rewards: int
    rewards_capped: Decimal | None
    consistency_budget: Decimal | None
    margin_above_cap: Decimal | None
    pressed_rewards_capped: Decimal | None
    settled_margin: int | None
    """None where a pressed winner has no reward row, which the property raises on."""


def diff_totals(diffs: Iterable[OrderDiff]) -> DiffTotals:
    surplus_base = surplus_loo = fees_base = fees_loo = partner_base = partner_loo = 0
    only_with = only_without = unsettled = late = 0
    complete = True
    for d in diffs:
        surplus_base += d.surplus_base or 0
        surplus_loo += d.surplus_loo or 0
        fees_base += d.fee_base or 0
        fees_loo += d.fee_loo or 0
        partner_base += d.partner_fee_base or 0
        partner_loo += d.partner_fee_loo or 0
        complete = complete and d.fees_known_base and d.fees_known_loo
        only_with += d.executed_base and not d.executed_loo
        only_without += d.executed_loo and not d.executed_base
        unsettled += d.observed_base and not d.executed_base
        late += d.late_base
    return DiffTotals(
        surplus_base,
        surplus_loo,
        fees_base,
        fees_loo,
        partner_base,
        partner_loo,
        complete,
        only_with,
        only_without,
        unsettled,
        late,
    )


@dataclass(frozen=True)
class AuctionCounterfactual:
    auction_id: int
    n_solutions: int
    solver_present: bool
    """Any removed solver bid. If none did, skipped but kept in the denominator."""
    removed_present: frozenset[str] = frozenset()
    solver_won_baseline: bool = False
    solver_won_db: bool = False
    """Any removed solver won: by our arbitration / by the DB record."""
    baseline_winner_uids: frozenset[int] = frozenset()
    loo_winner_uids: frozenset[int] = frozenset()
    baseline_reference_scores: dict[str, int] = field(default_factory=dict[str, int])
    loo_reference_scores: dict[str, int] = field(default_factory=dict[str, int])
    reference_margins_base: dict[str, int] = field(default_factory=dict[str, int])
    reference_margins_loo: dict[str, int] = field(default_factory=dict[str, int])
    """`reference_margin` per pressed winner (`pressed_winners`), from each side's own
    winning and reference scores; settlement plays no part."""
    pressed_volume_base: int = 0
    """Received-leg volume of the winning solutions of pressed winners whose margin moved."""
    runner_up_uids: frozenset[int] = frozenset()
    """Removed solvers' losing solutions that set another winner's reference score."""
    baseline_rewards: dict[str, SolverReward] = field(default_factory=dict[str, SolverReward])
    loo_rewards: dict[str, SolverReward] = field(default_factory=dict[str, SolverReward])
    """Score mode only; empty in surplus mode."""
    cap_double_inherited: int = 0
    cap_orphans_loo: frozenset[int] = frozenset()
    """LOO winners with no estimable cap; the auction drops out of the capped sum."""
    quote_carried_uids: frozenset[str] = frozenset()
    """Orders carrying a replacement quote (a removed solver won their quote competition),
    also where no removed solver bid."""
    quote_substituted_uids: frozenset[str] = frozenset()
    """Of those, executed on the loo side with a `priceimprovement` fee: the recharges."""
    quote_fee_recharges: dict[str, int] = field(default_factory=dict[str, int])
    quote_partner_fee_recharges: dict[str, int] = field(default_factory=dict[str, int])
    """Per recharged order, native fee against the replacement minus the recorded quote.
    Kept apart from the fee deltas (recorded quotes on both sides): a worse quote raises
    the price-improvement baseline mechanically."""
    quote_limit_unclearable_uids: frozenset[str] = frozenset()
    """Recharged orders whose execution misses the replacement quote's limit."""
    quote_shift_winner_set_changed: bool = False
    """Would shifting scores by the limit change move the loo winners? A check only:
    scores are never recomputed from a counterfactual quote."""

    price_suspect_uids: frozenset[int] = frozenset()
    """Solutions failing `price_suspects`."""
    multi_pair_solutions: int = 0
    """Filter-judged solutions touching more than one directed pair."""
    multi_pair_filter_proxied: int = 0
    """Of those, verdicts resting on the surplus fallback (no recorded pair score)."""
    filter_guess_could_matter: bool = False
    """Could a guessed filter verdict change the winners on either side? False is a proof."""
    block_deadline: int = 0
    executed_volume_base: int = 0
    executed_orders_base: int = 0
    executed_orders_all_base: int = 0
    """`recorded_executed_volume`, whether or not a removed solver bid."""
    un_filtered_uids: frozenset[int] = frozenset()
    un_filtered_winner_uids: frozenset[int] = frozenset()
    order_diffs: tuple[OrderDiff, ...] = ()
    replacements_base: frozenset[int] = frozenset()
    replacements_loo: frozenset[int] = frozenset()
    """Winners that never won for real; on the baseline only where we disagree with the DB."""
    inherited_reverts_loo: frozenset[int] = frozenset()
    orphans_base: frozenset[int] = frozenset()
    orphans_loo: frozenset[int] = frozenset()
    """Replacements whose settlement was assumed: no recorded winner held their pairs."""
    valuation_failures: tuple[tuple[int, str], ...] = ()
    baseline_matches_db: bool = True

    @property
    def winner_set_changed(self) -> bool:
        return self.baseline_winner_uids != self.loo_winner_uids

    @property
    def filter_relaxed(self) -> bool:
        return bool(self.un_filtered_uids)

    @property
    def reference_scores_moved(self) -> bool:
        """Can move without the winner set: a removed non-winning solution can win a
        without-`s` pick in `compute_reference_scores`."""
        return self.baseline_reference_scores != self.loo_reference_scores

    @property
    def runner_up(self) -> bool:
        return bool(self.runner_up_uids)

    def _side_totals(
        self, margins: Mapping[str, int], rewards: dict[str, SolverReward]
    ) -> SideTotals:
        return SideTotals(
            reference_margin=sum(margins.values()),
            rewards=sum(r.uncapped_reward for r in rewards.values()),
            rewards_capped=self._capped_total(rewards),
            consistency_budget=self._consistency_budget(rewards),
            margin_above_cap=self._margin_above_cap(margins, rewards),
            pressed_rewards_capped=self._pressed_capped(margins, rewards),
            settled_margin=(
                self._settled_margin(margins, rewards)
                if margins.keys() <= rewards.keys()
                else None
            ),
        )

    @cached_property
    def base_totals(self) -> SideTotals:
        return self._side_totals(self.reference_margins_base, self.baseline_rewards)

    @cached_property
    def loo_totals(self) -> SideTotals:
        if (
            self.reference_margins_loo is self.reference_margins_base
            and self.loo_rewards is self.baseline_rewards
        ):
            return self.base_totals
        return self._side_totals(self.reference_margins_loo, self.loo_rewards)

    @property
    def reference_margin_base(self) -> int:
        return self.base_totals.reference_margin

    @property
    def reference_margin_loo(self) -> int:
        return self.loo_totals.reference_margin

    @property
    def delta_reference_margin(self) -> int:
        """Counterfactual minus actual. A margin moves only with the winning score or a
        reference score, so `anything_moved` retains every auction where this is nonzero."""
        return self.reference_margin_loo - self.reference_margin_base

    @cached_property
    def reference_margin_rises(self) -> int:
        return sum(
            1
            for solver, margin in self.reference_margins_base.items()
            if self.reference_margins_loo[solver] > margin
        )

    @cached_property
    def reference_margin_falls(self) -> int:
        """Removal lowers a margin where the reference pick does better without it: an
        un-filtered solution, a batch a removed winner's claim kept out, or smaller bids a
        removed losing batch crowded out of the greedy pick."""
        return sum(
            1
            for solver, margin in self.reference_margins_base.items()
            if self.reference_margins_loo[solver] < margin
        )

    @property
    def anything_moved(self) -> bool:
        """Rewards also move on reference scores alone, and fees on a quote recharge alone."""
        return (
            self.winner_set_changed
            or self.filter_relaxed
            or self.reference_scores_moved
            or bool(self.quote_substituted_uids)
        )

    @cached_property
    def diff_totals(self) -> DiffTotals:
        return diff_totals(self.order_diffs)

    @property
    def delta_surplus(self) -> int:
        return self.diff_totals.surplus_loo - self.diff_totals.surplus_base

    @property
    def surplus_base(self) -> int:
        return self.diff_totals.surplus_base

    @property
    def surplus_loo(self) -> int:
        return self.diff_totals.surplus_loo

    @property
    def fees_complete(self) -> bool:
        """Every executed order's fee known on both sides; fails only when a solution the record
        filtered out wins the loo side."""
        return self.diff_totals.fees_complete

    @property
    def protocol_and_partner_fees_base(self) -> int:
        return self.diff_totals.fees_base

    @property
    def protocol_and_partner_fees_loo(self) -> int:
        return self.diff_totals.fees_loo

    @property
    def partner_fees_base(self) -> int:
        return self.diff_totals.partner_fees_base

    @property
    def partner_fees_loo(self) -> int:
        return self.diff_totals.partner_fees_loo

    @property
    def protocol_fees_base(self) -> int:
        return self.protocol_and_partner_fees_base - self.partner_fees_base

    @property
    def protocol_fees_loo(self) -> int:
        return self.protocol_and_partner_fees_loo - self.partner_fees_loo

    @property
    def delta_protocol_and_partner_fees(self) -> int:
        return self.protocol_and_partner_fees_loo - self.protocol_and_partner_fees_base

    @property
    def delta_partner_fees(self) -> int:
        return self.partner_fees_loo - self.partner_fees_base

    @property
    def delta_protocol_fees(self) -> int:
        """Gross revenue change; `delta_rewards` usually moves the other way."""
        return self.protocol_fees_loo - self.protocol_fees_base

    @cached_property
    def quote_fee_recharge(self) -> int:
        """Σ (fee against replacement − fee against recorded quote) over recharged orders."""
        return sum(self.quote_fee_recharges.values())

    @cached_property
    def quote_fee_recharge_partner(self) -> int:
        return sum(self.quote_partner_fee_recharges.values())

    @property
    def quote_fee_recharge_protocol(self) -> int:
        return self.quote_fee_recharge - self.quote_fee_recharge_partner

    @property
    def rewards_base(self) -> int:
        return self.base_totals.rewards

    @property
    def rewards_loo(self) -> int:
        return self.loo_totals.rewards

    @property
    def delta_rewards(self) -> int:
        return self.rewards_loo - self.rewards_base

    @staticmethod
    def _capped_total(rewards: dict[str, SolverReward]) -> Decimal | None:
        total = Decimal(0)
        for reward in rewards.values():
            if reward.capped_reward is None:
                return None
            total += reward.capped_reward
        return total

    @property
    def rewards_base_capped(self) -> Decimal | None:
        return self.base_totals.rewards_capped

    @property
    def rewards_loo_capped(self) -> Decimal | None:
        return self.loo_totals.rewards_capped

    @property
    def delta_rewards_capped(self) -> Decimal | None:
        """None when either side has a winner without an estimable cap."""
        base, loo = self.rewards_base_capped, self.rewards_loo_capped
        if base is None or loo is None:
            return None
        return loo - base

    @staticmethod
    def _consistency_budget(rewards: dict[str, SolverReward]) -> Decimal | None:
        """Σ (upper cap − capped reward), as `consistency_budget_contribution`."""
        total = Decimal(0)
        for reward in rewards.values():
            if reward.capped_reward is None or reward.upper_cap is None:
                return None
            total += reward.upper_cap - reward.capped_reward
        return total

    @property
    def consistency_budget_base(self) -> Decimal | None:
        return self.base_totals.consistency_budget

    @property
    def consistency_budget_loo(self) -> Decimal | None:
        return self.loo_totals.consistency_budget

    @property
    def delta_consistency_budget(self) -> Decimal | None:
        """Defined exactly where `delta_rewards_capped` is; the two sum to Δ Σ upper caps."""
        base, loo = self.consistency_budget_base, self.consistency_budget_loo
        if base is None or loo is None:
            return None
        return loo - base

    @staticmethod
    def _margin_above_cap(
        margins: Mapping[str, int], rewards: Mapping[str, SolverReward]
    ) -> Decimal | None:
        """Σ max(0, margin − upper cap) over the pressed winners whose wins all settled:
        score a winner could have withheld from users at no cost in reward, since above its
        cap the reward no longer depends on its bid. A batch that did not land delivered
        nothing to withhold. None without rewards (surplus mode) or with an unknown cap."""
        total = Decimal(0)
        for solver, margin in margins.items():
            reward = rewards.get(solver)
            if reward is None or reward.upper_cap is None:
                return None
            if reward.observed_score == reward.competition_score:
                total += max(Decimal(0), margin - reward.upper_cap)
        return total

    @property
    def margin_above_cap_base(self) -> Decimal | None:
        return self.base_totals.margin_above_cap

    @property
    def margin_above_cap_loo(self) -> Decimal | None:
        return self.loo_totals.margin_above_cap

    @property
    def delta_margin_above_cap(self) -> Decimal | None:
        base, loo = self.margin_above_cap_base, self.margin_above_cap_loo
        if base is None or loo is None:
            return None
        return loo - base

    @staticmethod
    def _pressed_capped(
        margins: Mapping[str, int], rewards: Mapping[str, SolverReward]
    ) -> Decimal | None:
        """Σ capped reward of the pressed winners (the keys of `margins`)."""
        total = Decimal(0)
        for solver in margins:
            reward = rewards.get(solver)
            if reward is None or reward.capped_reward is None:
                return None
            total += reward.capped_reward
        return total

    @property
    def pressed_rewards_capped_base(self) -> Decimal | None:
        return self.base_totals.pressed_rewards_capped

    @property
    def pressed_rewards_capped_loo(self) -> Decimal | None:
        return self.loo_totals.pressed_rewards_capped

    @property
    def delta_pressed_rewards_capped(self) -> Decimal | None:
        """What the pressed winners would be paid more without the removed solvers, bids
        fixed. Their caps are the same on both sides, so the consistency budget moves by
        exactly the opposite: mechanism accounting, no assumption about behaviour."""
        base, loo = self.pressed_rewards_capped_base, self.pressed_rewards_capped_loo
        if base is None or loo is None:
            return None
        return loo - base

    @staticmethod
    def _settled_margin(margins: Mapping[str, int], rewards: Mapping[str, SolverReward]) -> int:
        """Σ margin over the pressed winners whose wins all settled. Callers check first
        that every pressed winner has a reward row."""
        return sum(
            margin
            for solver, margin in margins.items()
            if rewards[solver].observed_score == rewards[solver].competition_score
        )

    @property
    def delta_settled_margin(self) -> int | None:
        """The pressed winners' margin over their reference, counterfactual − actual, over
        those whose wins all settled: the rent the removed solvers' losing bids take from
        the winners, whoever ends up paying it (docs/winner-selection.md, "The margin of
        settled winners"). Defined where the two capped figures are, so the three cover
        the same auctions."""
        if self.delta_margin_above_cap is None or self.delta_pressed_rewards_capped is None:
            return None
        return self.settled_margin_loo - self.settled_margin_base

    @property
    def settled_margin_base(self) -> int:
        margin = self.base_totals.settled_margin
        if margin is None:  # raises the missing reward row's KeyError
            return self._settled_margin(self.reference_margins_base, self.baseline_rewards)
        return margin

    @property
    def settled_margin_loo(self) -> int:
        margin = self.loo_totals.settled_margin
        if margin is None:  # raises the missing reward row's KeyError
            return self._settled_margin(self.reference_margins_loo, self.loo_rewards)
        return margin

    @property
    def delta_score(self) -> int:
        """Δsurplus + Δprotocol and partner fees, the fee leg only where every fee is
        priced: the window total is then the sum of the two rows."""
        fees = self.delta_protocol_and_partner_fees if self.fees_complete else 0
        return self.delta_surplus + fees

    @property
    def price_suspect(self) -> bool:
        return bool(self.price_suspect_uids)


def _quote_overlay(
    bundle: AuctionBundle,
    baseline: Sequence[ValuedBid],
    quoted: frozenset[str],
    counterfactual_quotes: Mapping[str, Quote],
    weth: str,
    mode: Mode,
) -> tuple[list[ValuedBid] | None, frozenset[str]]:
    """Revalue every bid with the replacement quotes; return them and the orders substituted.

    An order whose replacement leaves its fee unknown keeps its recorded quote, so one
    unusable replacement does not void the rest. `(None, ∅)` if nothing could be substituted."""
    known_in_baseline = {
        order.uid
        for entry in baseline
        for order in entry.bid.orders
        if order.uid not in entry.valuation.fees_unknown
    }
    overlay: dict[str, Quote] = {uid: counterfactual_quotes[uid] for uid in quoted}
    for _attempt in range(2):
        if not overlay:
            return None, frozenset()
        quotes: dict[str, Quote] = dict(bundle.fee_context.quotes)
        quotes.update(overlay)
        valued, failures = build_solutions(
            bundle.bids,
            bundle.native_prices,
            weth,
            mode=mode,
            fee_context=replace(bundle.fee_context, quotes=quotes),
        )
        broken = {
            uid
            for entry in valued
            for uid in entry.valuation.fees_unknown
            if uid in overlay and uid in known_in_baseline
        } | {
            order.uid
            for bid in bundle.bids
            if bid.uid in failures
            for order in bid.orders
            if order.uid in overlay
        }
        if not failures and not broken:
            return valued, frozenset(overlay)
        overlay = {uid: quote for uid, quote in overlay.items() if uid not in broken}
    return None, frozenset()


def _charges_price_improvement(context: FeeContext, uid: str) -> bool:
    return any(p.kind == "priceimprovement" for p in context.policies.get(uid, ()))


def _limit_checks(
    valued: Iterable[ValuedBid],
    substituted: frozenset[str],
    executed_by: Mapping[str, int | None],
    counterfactual_quotes: Mapping[str, Quote],
    fee_context: FeeContext,
    native_prices: Mapping[str, int],
) -> tuple[frozenset[str], dict[int, dict[Pair, int]]]:
    """Substituted orders whose loo execution misses the replacement quote's limit, and per
    solution and pair the native surplus shift those limits imply (ranking check input only)."""
    unclearable: set[str] = set()
    shifts: dict[int, dict[Pair, int]] = {}
    for entry in valued:
        for order in entry.bid.orders:
            if order.uid not in substituted:
                continue
            recorded = fee_context.quotes.get(order.uid)
            if recorded is None or order.uid not in entry.valuation.order_surplus_native:
                continue
            shift = shifted_limit(order, recorded, counterfactual_quotes[order.uid])
            if shift is None:
                continue
            if not shift.clears and executed_by.get(order.uid) == entry.bid.uid:
                unclearable.add(order.uid)
            token = order.buy_token if order.side == "sell" else order.sell_token
            price = native_prices.get(token)
            if price is None or not shift.surplus_shift_atoms:
                continue
            atoms = shift.surplus_shift_atoms
            native = (
                price_in_eth(price, atoms) if atoms > 0 else -price_in_eth(price, -atoms)
            )
            pair: Pair = (order.sell_token, order.buy_token)
            by_pair = shifts.setdefault(entry.bid.uid, {})
            by_pair[pair] = by_pair.get(pair, 0) + native
    return frozenset(unclearable), shifts


def _shift_moves_winners(
    solutions: Iterable[Solution],
    removed: frozenset[str],
    shifts: Mapping[int, Mapping[Pair, int]],
    loo_winner_uids: frozenset[int],
    max_winners: int,
) -> bool:
    """Does shifting each score by its limit-implied surplus shift change the loo winners?"""
    shifted: list[Solution] = []
    for solution in solutions:
        if solution.solver in removed:
            continue
        delta = shifts.get(solution.solution_uid)
        if not delta:
            shifted.append(solution)
            continue
        shifted.append(
            replace(
                solution,
                total=solution.total + sum(delta.values()),
                pair_values={
                    pair: value + delta.get(pair, 0)
                    for pair, value in solution.pair_values.items()
                },
                pair_scores={
                    pair: value + delta.get(pair, 0)
                    for pair, value in solution.pair_scores.items()
                },
            )
        )
    return arbitrate(shifted, max_winners).winner_uids != loo_winner_uids


UNMOVED_SHARED = (
    "diff_totals",
    "base_totals",
    "loo_totals",
    "reference_margin_rises",
    "reference_margin_falls",
    "quote_fee_recharge",
    "quote_fee_recharge_partner",
)
"""`AuctionCounterfactual` cached properties that read none of the fields in which one
auction's unmoved results differ (`removed_present`, `solver_won_db`,
`filter_guess_could_matter`), so `PreparedAuction` shares them among those results."""

_SCENARIO_CACHED = frozenset(
    name
    for name, value in vars(AuctionCounterfactual).items()
    if isinstance(value, cached_property)
) - frozenset(UNMOVED_SHARED)
"""Every other cached property: never copied from one scenario's result to another's."""


class PreparedAuction:
    """One auction valued and arbitrated with every solver present: all of it that no
    removal changes, computed once and shared by every scenario and outcome rule.
    `analyse` adds the side without the removed solvers; `analyse_auction` does both."""

    def __init__(
        self,
        bundle: AuctionBundle,
        weth: str,
        *,
        mode: Mode = "score",
        max_winners: int = MAX_WINNERS,
        settled: Mapping[int, Settlement] | None = None,
        solution_caps: Mapping[int, SolutionCap] | None = None,
    ) -> None:
        self.bundle = bundle
        self.weth = weth
        self.mode: Mode = mode
        self.max_winners = max_winners
        self.settled: Mapping[int, Settlement] = settled or {}
        self.caps: Mapping[int, SolutionCap] = solution_caps if solution_caps else {}
        self.db_winner_uids = frozenset(b.uid for b in bundle.bids if b.is_winner)
        self.bidders = frozenset(b.solver for b in bundle.bids)
        self.db_winning_solvers = frozenset(b.solver for b in bundle.bids if b.is_winner)
        self.order_uids = frozenset(o.uid for b in bundle.bids for o in b.orders)
        self.suspects = price_suspects(bundle.bids, bundle.native_prices)
        self.valued, self.failures = build_solutions(
            bundle.bids,
            bundle.native_prices,
            weth,
            mode=mode,
            fee_context=bundle.fee_context,
        )
        self.solutions = [v.solution for v in self.valued]
        self.by_uid = {v.bid.uid: v for v in self.valued}
        self._executed: dict[OutcomeRule, tuple[int, int, int]] = {}
        self._absent: dict[OutcomeRule, AuctionCounterfactual] = {}
        self._inherit: dict[OutcomeRule, dict[Pair, bool] | None] = {}
        self._base: dict[OutcomeRule, SideOutcomes] = {}
        self._base_diffs: dict[OutcomeRule, tuple[OrderDiff, ...]] = {}
        self._unmoved_first: dict[OutcomeRule, AuctionCounterfactual] = {}
        self._baseline_rewards: dict[OutcomeRule, dict[str, SolverReward]] = {}

    def executed_volume(self, outcome_rule: OutcomeRule) -> tuple[int, int, int]:
        if outcome_rule not in self._executed:
            self._executed[outcome_rule] = (
                (0, 0, 0)
                if self.failures
                else recorded_executed_volume(
                    self.valued,
                    self.db_winner_uids,
                    outcome_rule=outcome_rule,
                    settled=self.settled,
                )
            )
        return self._executed[outcome_rule]

    @cached_property
    def arbitration(self) -> Arbitration:
        return Arbitration(self.solutions, self.max_winners)

    @cached_property
    def baseline(self) -> Ranking:
        return self.arbitration.ranking

    @cached_property
    def multi_pair(self) -> int:
        """Baseline side: its baselines are the higher, so this never understates."""
        return sum(1 for s in self.baseline.tested if len(s.pair_values) > 1)

    @cached_property
    def baseline_guess_could_matter(self) -> bool:
        return filter_guess_could_matter(self.baseline, self.max_winners)

    @cached_property
    def baseline_references(self) -> dict[str, ReferenceScore]:
        return self.arbitration.references

    @cached_property
    def baseline_reference_scores(self) -> dict[str, int]:
        return self.arbitration.reference_scores

    @cached_property
    def baseline_by_solver(self) -> dict[str, frozenset[int]]:
        return _winning_uids_by_solver(self.baseline)

    @cached_property
    def baseline_margins(self) -> dict[str, int]:
        """Every winning solver's `reference_margin`."""
        return {
            solver: reference_margin(
                self.baseline.winning_score, self.baseline_reference_scores[solver]
            )
            for solver in self.baseline_by_solver
        }

    def inherit(self, outcome_rule: OutcomeRule) -> dict[Pair, bool] | None:
        if outcome_rule not in self._inherit:
            self._inherit[outcome_rule] = (
                recorded_settlement_by_pair(self.by_uid, self.db_winner_uids, self.settled)
                if outcome_rule == "inherited"
                else None
            )
        return self._inherit[outcome_rule]

    def side(
        self,
        ranking: Ranking,
        outcome_rule: OutcomeRule,
        by_uid: Mapping[int, ValuedBid] | None = None,
    ) -> SideOutcomes:
        valued = by_uid if by_uid is not None else self.by_uid
        return side_outcomes(
            [valued[s.solution_uid] for s in ranking.winners],
            outcome_rule=outcome_rule,
            settled=self.settled,
            recorded_winner_uids=self.db_winner_uids,
            inherit=self.inherit(outcome_rule),
        )

    def base_side(self, outcome_rule: OutcomeRule) -> SideOutcomes:
        if outcome_rule not in self._base:
            self._base[outcome_rule] = self.side(self.baseline, outcome_rule)
        return self._base[outcome_rule]

    def base_diffs(self, outcome_rule: OutcomeRule) -> tuple[OrderDiff, ...]:
        """The baseline diffed against itself: the diffs of a removal that moves no winner."""
        if outcome_rule not in self._base_diffs:
            orders = self.base_side(outcome_rule).orders
            self._base_diffs[outcome_rule] = diff_outcomes(orders, orders)
        return self._base_diffs[outcome_rule]

    @cached_property
    def cap_by_pair(self) -> dict[Pair, tuple[int, SolutionCap]]:
        return recorded_caps_by_pair(self.by_uid, self.db_winner_uids, self.caps)

    def winner_caps(self, ranking: Ranking) -> SideCaps:
        return winner_caps(
            [self.by_uid[s.solution_uid] for s in ranking.winners],
            self.db_winner_uids,
            self.caps,
            self.cap_by_pair,
        )

    @cached_property
    def base_caps(self) -> SideCaps:
        return self.winner_caps(self.baseline)

    def rewards(
        self,
        ranking: Ranking,
        side: SideOutcomes,
        caps: SideCaps,
        reference_scores: Mapping[str, int],
    ) -> dict[str, SolverReward]:
        # Auction-level cap facts (flat floor, exclusion) ride on every recorded winner's row.
        any_cap = next(iter(self.caps.values()), None)
        return uncapped_rewards(
            [
                Win(
                    s.solver,
                    s.total,
                    side.solution_executed[s.solution_uid],
                    caps.by_uid[s.solution_uid],
                    caps.penalty_by_uid[s.solution_uid],
                )
                for s in ranking.winners
            ],
            reference_scores,
            lower_cap=any_cap.lower if any_cap else None,
            excluded=any_cap.excluded if any_cap else False,
        )

    def baseline_rewards(self, outcome_rule: OutcomeRule) -> dict[str, SolverReward]:
        if outcome_rule not in self._baseline_rewards:
            self._baseline_rewards[outcome_rule] = self.rewards(
                self.baseline,
                self.base_side(outcome_rule),
                self.base_caps,
                self.baseline_reference_scores,
            )
        return self._baseline_rewards[outcome_rule]

    def _unarbitrated(
        self, outcome_rule: OutcomeRule, removed_present: frozenset[str], won_db: bool
    ) -> AuctionCounterfactual:
        executed_volume, executed_orders, executed_orders_all = self.executed_volume(
            outcome_rule
        )
        return AuctionCounterfactual(
            auction_id=self.bundle.auction_id,
            n_solutions=len(self.bundle.bids),
            solver_present=bool(removed_present),
            removed_present=removed_present,
            solver_won_db=won_db,
            valuation_failures=tuple(sorted(self.failures.items())),
            block_deadline=self.bundle.block_deadline,
            price_suspect_uids=self.suspects,
            executed_volume_base=executed_volume,
            executed_orders_base=executed_orders,
            executed_orders_all_base=executed_orders_all,
        )

    def _unmoved_fields(self, outcome_rule: OutcomeRule) -> dict[str, Any]:
        """Every field of an unmoved result but the three a scenario sets itself."""
        baseline = self.baseline
        base = self.base_side(outcome_rule)
        executed_volume, executed_orders, executed_orders_all = self.executed_volume(
            outcome_rule
        )
        rewards: dict[str, SolverReward] = {}
        cap_double_inherited = 0
        cap_orphans: frozenset[int] = frozenset()
        if self.mode == "score":
            cap_double_inherited = 2 * self.base_caps.double_inherited
            cap_orphans = self.base_caps.orphans
            rewards = self.baseline_rewards(outcome_rule)
        return dict(
            auction_id=self.bundle.auction_id,
            n_solutions=len(self.bundle.bids),
            solver_present=True,
            solver_won_baseline=False,
            baseline_winner_uids=baseline.winner_uids,
            loo_winner_uids=baseline.winner_uids,
            baseline_reference_scores=self.baseline_reference_scores,
            loo_reference_scores=self.baseline_reference_scores,
            reference_margins_base=self.baseline_margins,
            reference_margins_loo=self.baseline_margins,
            baseline_rewards=rewards,
            loo_rewards=rewards,
            cap_double_inherited=cap_double_inherited,
            cap_orphans_loo=cap_orphans,
            price_suspect_uids=self.suspects,
            multi_pair_solutions=self.multi_pair,
            multi_pair_filter_proxied=len(baseline.proxied_uids),
            block_deadline=self.bundle.block_deadline,
            executed_volume_base=executed_volume,
            executed_orders_base=executed_orders,
            executed_orders_all_base=executed_orders_all,
            order_diffs=self.base_diffs(outcome_rule),
            replacements_base=base.replacements,
            replacements_loo=base.replacements,
            inherited_reverts_loo=base.inherited_reverts,
            orphans_base=base.orphans,
            orphans_loo=base.orphans,
            baseline_matches_db=baseline.winner_uids == self.db_winner_uids,
        )

    def _unmoved(
        self,
        outcome_rule: OutcomeRule,
        removed_present: frozenset[str],
        won_db: bool,
        guess_could_matter: bool,
    ) -> AuctionCounterfactual:
        """A removal that moves nothing (`Arbitration.unmoved_by`) and carries no quote:
        the side without it is the baseline. What `analyse` would build, from the cache."""
        first = self._unmoved_first.get(outcome_rule)
        if first is None:
            first = self._unmoved_first[outcome_rule] = AuctionCounterfactual(
                **self._unmoved_fields(outcome_rule),
                removed_present=removed_present,
                solver_won_db=won_db,
                filter_guess_could_matter=guess_could_matter,
            )
            return first
        # The first one with this scenario's three fields. Every other field is the same
        # by construction, and so is what the first has derived from them by now
        # (`UNMOVED_SHARED`, cached as `Analysis.add` reads it). Copied, not constructed:
        # `__init__` of 46 frozen fields costs more than all the rest of this path.
        result = object.__new__(AuctionCounterfactual)
        state = result.__dict__
        state.update(first.__dict__)
        for name in _SCENARIO_CACHED:
            state.pop(name, None)
        state["removed_present"] = removed_present
        state["solver_won_db"] = won_db
        state["filter_guess_could_matter"] = guess_could_matter
        return result

    def analyse(
        self,
        removed: frozenset[str],
        *,
        outcome_rule: OutcomeRule = "inherited",
        counterfactual_quotes: Mapping[str, Quote] | None = None,
    ) -> AuctionCounterfactual:
        """This auction without every solver of `removed`, diffed against the baseline."""
        removed_present = self.bidders & removed
        if not removed_present and not counterfactual_quotes:
            # Most scenarios in most auctions: the result every such scenario shares.
            absent = self._absent.get(outcome_rule)
            if absent is not None:
                return absent
        bundle = self.bundle
        max_winners = self.max_winners
        present = bool(removed_present)
        executed_volume, executed_orders, executed_orders_all = self.executed_volume(
            outcome_rule
        )

        # An auction holding a replacement quote is arbitrated even without a removed bidder.
        quoted: frozenset[str] = frozenset()
        if counterfactual_quotes:
            quoted = frozenset(
                uid for uid in self.order_uids if uid in counterfactual_quotes
            )

        if self.failures or (not present and not quoted):
            if present:
                return self._unarbitrated(
                    outcome_rule, removed_present, bool(removed & self.db_winning_solvers)
                )
            # Without a removed bidder the result is the same for every scenario.
            if outcome_rule not in self._absent:
                self._absent[outcome_rule] = self._unarbitrated(
                    outcome_rule, removed_present, False
                )
            return self._absent[outcome_rule]

        valued = self.valued
        by_uid = self.by_uid

        # Quotes change only fees, so arbitration runs on `solutions` either way.
        substituted: frozenset[str] = frozenset()
        by_uid_loo = by_uid
        if quoted and counterfactual_quotes:
            loo_valued, substituted = _quote_overlay(
                bundle, valued, quoted, counterfactual_quotes, self.weth, self.mode
            )
            if loo_valued is not None:
                by_uid_loo = {v.bid.uid: v for v in loo_valued}

        baseline = self.baseline
        won_db = bool(removed & self.db_winning_solvers)
        if not substituted and self.arbitration.unmoved_by(removed):
            # Only guessed verdicts need the ranking without `removed`.
            guess_could_matter = self.baseline_guess_could_matter or (
                bool(baseline.proxied_uids)
                and filter_guess_could_matter(
                    self.arbitration.without(removed).ranking, max_winners
                )
            )
            return self._unmoved(outcome_rule, removed_present, won_db, guess_could_matter)

        loo, loo_reference_scores, verdicts_moved = self.arbitration.without(removed)
        guess_could_matter = self.baseline_guess_could_matter or filter_guess_could_matter(
            loo, max_winners
        )
        # With every verdict standing, nothing is un-filtered and nothing can be lost.
        relaxed = un_filtered(baseline, loo, removed) if verdicts_moved else frozenset[int]()
        # The same winners come in the same order (`score_order`), so everything that
        # follows from the winners alone is the baseline's.
        same_winners = loo.winner_uids == baseline.winner_uids

        base = self.base_side(outcome_rule)
        # Recorded quotes: the fee delta carries nothing of the quote.
        loo_side = base if same_winners else self.side(loo, outcome_rule)

        recharged: frozenset[str] = frozenset()
        unclearable: frozenset[str] = frozenset()
        shift_moves_winners = False
        fee_recharges: dict[str, int] = {}
        partner_recharges: dict[str, int] = {}
        if substituted and counterfactual_quotes:
            loo_replaced = self.side(loo, outcome_rule, by_uid_loo)
            recharged = frozenset(
                uid
                for uid in substituted
                if (outcome := loo_side.orders.get(uid)) is not None
                and outcome.executed
                and _charges_price_improvement(bundle.fee_context, uid)
            )
            for uid in recharged:
                before, after = loo_side.orders[uid], loo_replaced.orders[uid]
                if before.fee_native is not None and after.fee_native is not None:
                    fee_recharges[uid] = after.fee_native - before.fee_native
                    partner_recharges[uid] = (after.partner_fee_native or 0) - (
                        before.partner_fee_native or 0
                    )
            unclearable, shifts = _limit_checks(
                valued,
                substituted,
                {uid: outcome.solution_uid for uid, outcome in loo_side.orders.items()},
                counterfactual_quotes,
                bundle.fee_context,
                bundle.native_prices,
            )
            unclearable &= recharged
            shift_moves_winners = bool(shifts) and _shift_moves_winners(
                self.solutions, removed, shifts, loo.winner_uids, max_winners
            )

        baseline_references = self.baseline_references
        baseline_reference_scores = self.baseline_reference_scores

        pressed = (
            {s: uids for s, uids in self.baseline_by_solver.items() if s not in removed}
            if same_winners
            else pressed_winners(baseline, loo, removed)
        )
        margins_base = {
            solver: reference_margin(baseline.winning_score, baseline_reference_scores[solver])
            for solver in pressed
        }
        margins_loo = {
            solver: reference_margin(loo.winning_score, loo_reference_scores[solver])
            for solver in pressed
        }
        pressed_volume = sum(
            by_uid[uid].valuation.order_volume_native[order.uid]
            for solver, uids in pressed.items()
            if margins_base[solver] != margins_loo[solver]
            for uid in uids
            for order in by_uid[uid].bid.orders
        )

        baseline_rewards: dict[str, SolverReward] = {}
        loo_rewards: dict[str, SolverReward] = {}
        cap_double_inherited = 0
        cap_orphans: frozenset[int] = frozenset()
        if self.mode == "score":
            base_caps = self.base_caps
            loo_caps = base_caps if same_winners else self.winner_caps(loo)
            cap_double_inherited = base_caps.double_inherited + loo_caps.double_inherited
            cap_orphans = loo_caps.orphans
            baseline_rewards = self.baseline_rewards(outcome_rule)
            loo_rewards = (
                baseline_rewards
                if same_winners and loo_reference_scores == baseline_reference_scores
                else self.rewards(loo, loo_side, loo_caps, loo_reference_scores)
            )

        return AuctionCounterfactual(
            auction_id=bundle.auction_id,
            n_solutions=len(bundle.bids),
            solver_present=present,
            removed_present=removed_present,
            solver_won_baseline=any(s.solver in removed for s in baseline.winners),
            solver_won_db=won_db,
            baseline_winner_uids=baseline.winner_uids,
            loo_winner_uids=loo.winner_uids,
            baseline_reference_scores=baseline_reference_scores,
            loo_reference_scores=loo_reference_scores,
            reference_margins_base=margins_base,
            reference_margins_loo=margins_loo,
            pressed_volume_base=pressed_volume,
            runner_up_uids=runner_up_uids(baseline, baseline_references, removed),
            baseline_rewards=baseline_rewards,
            loo_rewards=loo_rewards,
            cap_double_inherited=cap_double_inherited,
            cap_orphans_loo=cap_orphans,
            price_suspect_uids=self.suspects,
            multi_pair_solutions=self.multi_pair,
            multi_pair_filter_proxied=len(baseline.proxied_uids),
            filter_guess_could_matter=guess_could_matter,
            block_deadline=bundle.block_deadline,
            executed_volume_base=executed_volume,
            executed_orders_base=executed_orders,
            executed_orders_all_base=executed_orders_all,
            un_filtered_uids=relaxed,
            un_filtered_winner_uids=relaxed & loo.winner_uids,
            quote_carried_uids=substituted,
            quote_substituted_uids=recharged,
            quote_fee_recharges=fee_recharges,
            quote_partner_fee_recharges=partner_recharges,
            quote_limit_unclearable_uids=unclearable,
            quote_shift_winner_set_changed=shift_moves_winners,
            order_diffs=(
                self.base_diffs(outcome_rule)
                if same_winners
                else diff_outcomes(base.orders, loo_side.orders)
            ),
            replacements_base=base.replacements,
            replacements_loo=loo_side.replacements,
            inherited_reverts_loo=loo_side.inherited_reverts,
            orphans_base=base.orphans,
            orphans_loo=loo_side.orphans,
            baseline_matches_db=baseline.winner_uids == self.db_winner_uids,
        )


def analyse_auction(
    bundle: AuctionBundle,
    weth: str,
    removed: frozenset[str],
    *,
    mode: Mode = "score",
    max_winners: int = MAX_WINNERS,
    outcome_rule: OutcomeRule = "inherited",
    settled: Mapping[int, Settlement] | None = None,
    solution_caps: Mapping[int, SolutionCap] | None = None,
    counterfactual_quotes: Mapping[str, Quote] | None = None,
) -> AuctionCounterfactual:
    """Arbitrate one auction with and without `removed`, and diff the two.

    `solution_caps` (recorded winners) enables the capped-reward estimate. Under
    `counterfactual_quotes` bids, executions and ranking stay as recorded and only the fee
    is recomputed, reported as `quote_fee_recharge` and never added to the fee delta. This
    applies where no removed solver bid too: the quote winner often does not bid on its order.
    Several scenarios over one auction share a `PreparedAuction` instead."""
    return PreparedAuction(
        bundle,
        weth,
        mode=mode,
        max_winners=max_winners,
        settled=settled,
        solution_caps=solution_caps,
    ).analyse(
        removed, outcome_rule=outcome_rule, counterfactual_quotes=counterfactual_quotes
    )


@dataclass
class Analysis:
    """Window aggregate; `auctions` includes those no removed solver bid in. With several
    `addresses` every statistic describes the joint removal; nothing is split per member."""

    solver: str = ""
    addresses: frozenset[str] = frozenset()
    addresses_seen: frozenset[str] = frozenset()
    """Removed addresses that bid in a clean analysed auction (`run.absent_members`)."""
    mode: Mode = "score"
    outcome_rule: OutcomeRule = "inherited"

    auctions: int = 0
    auctions_with_solver: int = 0
    auctions_skipped: int = 0
    """A bid could not be valued."""
    auctions_solver_won_baseline: int = 0
    auctions_solver_won_db: int = 0
    """`_db` counts every auction; `_baseline` only arbitrated ones."""
    auctions_winner_set_changed: int = 0
    auctions_filter_relaxed: int = 0
    multi_pair_solutions: int = 0
    multi_pair_filter_proxied: int = 0
    """Over arbitrated auctions only; meaningless in surplus mode."""
    auctions_filter_guess_could_matter: int = 0
    """Residual filter uncertainty: a guessed verdict could have moved the winners."""
    auctions_newly_kept_won: int = 0
    auctions_baseline_differs_from_db: int = 0

    surplus_base: int = 0
    surplus_loo: int = 0

    protocol_and_partner_fees_base: int = 0
    protocol_and_partner_fees_loo: int = 0
    partner_fees_base: int = 0
    partner_fees_loo: int = 0
    """Recorded quotes on both sides (the quote effect is `quote_fee_recharge`); summed only
    over auctions priceable on both sides."""
    auctions_fees_counted: int = 0
    auctions_fees_incomplete: int = 0
    """Dropped from both sides' fee sums: an executed order's policies are unrecorded."""

    window_volume_base: int = 0
    window_orders_base: int = 0
    window_order_executions_base: int = 0
    """Denominators over every clean analysed auction, removed solver present or not;
    executions count once per auction."""

    rewards_base: int = 0
    rewards_loo: int = 0
    """Uncapped, over arbitrated auctions only (the rest cancel). Score mode only."""
    removed_reward_base: int = 0
    """The removed solvers' own share of `rewards_base`."""
    auctions_rewards_moved: int = 0
    negative_rewards_base: int = 0
    negative_rewards_loo: int = 0
    negative_reward_sum_base: int = 0
    negative_reward_sum_loo: int = 0
    """(count, native sum) of negative uncapped rewards; payouts clamp at `lower_reward_cap`."""

    capped_estimate: bool = False
    """Recorded caps were supplied; capped fields aggregate only then."""
    rewards_base_capped: Decimal = Decimal(0)
    rewards_loo_capped: Decimal = Decimal(0)
    consistency_budget_base: Decimal = Decimal(0)
    consistency_budget_loo: Decimal = Decimal(0)
    """Σ (upper cap − capped reward), the weekly consistency budget's contributions; over
    `auctions_capped` like the capped rewards."""
    auctions_capped: int = 0
    auctions_capped_skipped: int = 0
    """Dropped from both sides: some winner's cap could not be estimated."""
    cap_double_inherited: int = 0
    cap_orphans: int = 0

    exclude_price_suspect: bool = True
    """Drop `price_imbalanced` auctions from every statistic; they stay in `auctions` and
    `price_suspect_auctions`."""
    auctions_quote_checked: int = 0
    """Auctions where some order carried a replacement quote."""
    quote_substituted_orders: int = 0
    auctions_quote_substituted: int = 0
    """Distinct recharged orders, and the auctions holding them."""
    auctions_quote_only: int = 0
    """Checked auctions no removed solver bid in; only quote recharges and counters."""
    quote_limit_unclearable_orders: int = 0
    auctions_quote_shift_would_change_winners: int = 0
    quote_shift_auctions: list[int] = field(default_factory=list[int])
    quote_fee_recharge: int = 0
    quote_partner_fee_recharge: int = 0
    """Over fee-complete auctions; never added to the fee deltas. Positive when the removed
    solver's quotes were good."""
    quote_substituted_uid_set: set[str] = field(
        default_factory=set[str], metadata={"report": False}
    )
    quote_unclearable_uid_set: set[str] = field(
        default_factory=set[str], metadata={"report": False}
    )
    """Distinct orders behind the counts above; an order recurs across auctions."""

    price_suspect_auctions: list[int] = field(default_factory=list[int])

    fast_path_auctions: list[int] = field(default_factory=list[int])
    """Filled by `extract.load_auctions`, not `add`; not counted in `auctions`."""

    orders_compared: int = 0
    orders_only_with_solver: int = 0
    orders_only_without_solver: int = 0
    orders_unsettled_base: int = 0
    """Baseline orders lost to a settlement failure."""
    orders_lost_to_lateness: int = 0
    """Of those, filled after the deadline: real surplus knowingly discarded."""
    replacements_base: int = 0
    replacements_loo: int = 0
    inherited_reverts_loo: int = 0
    orphans_base: int = 0
    orphans_loo: int = 0
    """Replacements whose settlement was assumed: no recorded winner held their pairs."""

    auctions_runner_up: int = 0
    """A removed solver's losing solution set another winner's reference score."""
    reference_margin_base: int = 0
    reference_margin_loo: int = 0
    """Σ margin over the reference score of the pressed winners (outside the removal,
    same winning solutions on both sides); in the mode's currency, bids held fixed."""
    reference_margin_rises: int = 0
    reference_margin_falls: int = 0
    """(auction, pressed winner) pairs whose margin rose / fell without the removed solvers."""
    auctions_reference_margin_moved: int = 0
    reference_margin_volume: int = 0
    """Received-leg volume of those winners' winning solutions, where their margin moved."""
    margin_above_cap_base: Decimal = Decimal(0)
    margin_above_cap_loo: Decimal = Decimal(0)
    """Σ max(0, margin − upper cap) over the pressed winners that settled (score mode)."""
    pressed_rewards_capped_base: Decimal = Decimal(0)
    pressed_rewards_capped_loo: Decimal = Decimal(0)
    """Σ capped reward of the pressed winners (score mode)."""
    settled_margin_base: int = 0
    settled_margin_loo: int = 0
    """Σ margin over the pressed winners that settled, uncapped (score mode)."""
    auctions_margin_cap_unknown: int = 0
    """Auctions with a pressed winner whose cap is unknown, left out of the three above."""

    valuation_failures: list[tuple[int, int, str]] = field(
        default_factory=list[tuple[int, int, str]]
    )
    changed: list[AuctionCounterfactual] = field(default_factory=list[AuctionCounterfactual])

    @property
    def delta_surplus(self) -> int:
        return self.surplus_loo - self.surplus_base

    @property
    def delta_protocol_and_partner_fees(self) -> int:
        return self.protocol_and_partner_fees_loo - self.protocol_and_partner_fees_base

    @property
    def delta_partner_fees(self) -> int:
        return self.partner_fees_loo - self.partner_fees_base

    @property
    def protocol_fees_base(self) -> int:
        return self.protocol_and_partner_fees_base - self.partner_fees_base

    @property
    def protocol_fees_loo(self) -> int:
        return self.protocol_and_partner_fees_loo - self.partner_fees_loo

    @property
    def quote_fee_recharge_protocol(self) -> int:
        return self.quote_fee_recharge - self.quote_partner_fee_recharge

    @property
    def delta_protocol_fees(self) -> int:
        """Gross revenue change; `delta_rewards` usually moves the other way."""
        return self.protocol_fees_loo - self.protocol_fees_base

    @property
    def delta_rewards(self) -> int:
        return self.rewards_loo - self.rewards_base

    @property
    def delta_reference_margin(self) -> int:
        return self.reference_margin_loo - self.reference_margin_base

    @property
    def delta_margin_above_cap(self) -> Decimal | None:
        """None in surplus mode, which has no caps."""
        if self.mode != "score":
            return None
        return self.margin_above_cap_loo - self.margin_above_cap_base

    @property
    def delta_pressed_rewards_capped(self) -> Decimal | None:
        """None in surplus mode, which has no caps."""
        if self.mode != "score":
            return None
        return self.pressed_rewards_capped_loo - self.pressed_rewards_capped_base

    @property
    def delta_settled_margin(self) -> int | None:
        """None in surplus mode, which has no reward rows to tell settlement by."""
        if self.mode != "score":
            return None
        return self.settled_margin_loo - self.settled_margin_base

    @property
    def delta_score(self) -> int:
        """Δsurplus + Δprotocol and partner fees, as the two rows report them."""
        return self.delta_surplus + self.delta_protocol_and_partner_fees

    @property
    def delta_rewards_capped(self) -> Decimal:
        """Over `auctions_capped`."""
        return self.rewards_loo_capped - self.rewards_base_capped

    @property
    def delta_consistency_budget(self) -> Decimal:
        """Over `auctions_capped`; with `delta_rewards_capped` it sums to Δ Σ upper caps."""
        return self.consistency_budget_loo - self.consistency_budget_base

    def add(self, result: AuctionCounterfactual) -> None:
        self.auctions += 1
        if result.price_suspect:
            self.price_suspect_auctions.append(result.auction_id)
            if self.exclude_price_suspect:
                # Excluded from everything, so all statistics share one auction set.
                return
        if result.valuation_failures:
            self.auctions_skipped += 1
            self.valuation_failures.extend(
                (result.auction_id, uid, error) for uid, error in result.valuation_failures
            )
        self.auctions_solver_won_db += result.solver_won_db
        if result.valuation_failures:
            return
        # Window denominators include auctions no removed solver bid in.
        self.window_volume_base += result.executed_volume_base
        self.window_orders_base += result.executed_orders_base
        self.window_order_executions_base += result.executed_orders_all_base
        if not result.solver_present:
            if result.quote_carried_uids:
                self._add_quote_only(result)
            return

        self.auctions_with_solver += 1
        if result.quote_carried_uids:  # every other quote field is a subset of it
            self._add_quote_checks(result)
        if not result.removed_present <= self.addresses_seen:
            self.addresses_seen |= result.removed_present
        self.multi_pair_solutions += result.multi_pair_solutions
        self.multi_pair_filter_proxied += result.multi_pair_filter_proxied
        self.auctions_filter_guess_could_matter += result.filter_guess_could_matter
        self.auctions_solver_won_baseline += result.solver_won_baseline
        self.auctions_winner_set_changed += result.winner_set_changed
        self.auctions_filter_relaxed += result.filter_relaxed
        self.auctions_newly_kept_won += bool(result.un_filtered_winner_uids)
        self.auctions_baseline_differs_from_db += not result.baseline_matches_db
        diffs, base, loo = result.diff_totals, result.base_totals, result.loo_totals
        self.surplus_base += diffs.surplus_base
        self.surplus_loo += diffs.surplus_loo
        if diffs.fees_complete:
            self.auctions_fees_counted += 1
            self.protocol_and_partner_fees_base += diffs.fees_base
            self.protocol_and_partner_fees_loo += diffs.fees_loo
            self.partner_fees_base += diffs.partner_fees_base
            self.partner_fees_loo += diffs.partner_fees_loo
            self.quote_fee_recharge += result.quote_fee_recharge
            self.quote_partner_fee_recharge += result.quote_fee_recharge_partner
        else:
            self.auctions_fees_incomplete += 1
        self.rewards_base += base.rewards
        self.rewards_loo += loo.rewards
        self.removed_reward_base += sum(
            r.uncapped_reward
            for solver, r in result.baseline_rewards.items()
            if solver in self.addresses
        )
        self.auctions_rewards_moved += result.baseline_rewards != result.loo_rewards
        for reward in result.baseline_rewards.values():
            if reward.uncapped_reward < 0:
                self.negative_rewards_base += 1
                self.negative_reward_sum_base += reward.uncapped_reward
        for reward in result.loo_rewards.values():
            if reward.uncapped_reward < 0:
                self.negative_rewards_loo += 1
                self.negative_reward_sum_loo += reward.uncapped_reward

        if self.capped_estimate:
            base_capped = base.rewards_capped
            loo_capped = loo.rewards_capped
            base_budget = base.consistency_budget
            loo_budget = loo.consistency_budget
            if (
                base_capped is None
                or loo_capped is None
                or base_budget is None
                or loo_budget is None
            ):
                self.auctions_capped_skipped += 1
            else:
                self.auctions_capped += 1
                self.rewards_base_capped += base_capped
                self.rewards_loo_capped += loo_capped
                self.consistency_budget_base += base_budget
                self.consistency_budget_loo += loo_budget
        self.cap_double_inherited += result.cap_double_inherited
        self.cap_orphans += len(result.cap_orphans_loo)

        self.replacements_base += len(result.replacements_base)
        self.replacements_loo += len(result.replacements_loo)
        self.inherited_reverts_loo += len(result.inherited_reverts_loo)
        self.orphans_base += len(result.orphans_base)
        self.orphans_loo += len(result.orphans_loo)
        self.auctions_runner_up += result.runner_up
        self.reference_margin_base += base.reference_margin
        self.reference_margin_loo += loo.reference_margin
        self.reference_margin_rises += result.reference_margin_rises
        self.reference_margin_falls += result.reference_margin_falls
        self.auctions_reference_margin_moved += bool(
            result.reference_margin_rises or result.reference_margin_falls
        )
        self.reference_margin_volume += result.pressed_volume_base
        if self.mode == "score":
            above_base, above_loo = base.margin_above_cap, loo.margin_above_cap
            paid_base = base.pressed_rewards_capped
            paid_loo = loo.pressed_rewards_capped
            if above_base is None or above_loo is None or paid_base is None or paid_loo is None:
                self.auctions_margin_cap_unknown += 1
            else:
                self.margin_above_cap_base += above_base
                self.margin_above_cap_loo += above_loo
                self.pressed_rewards_capped_base += paid_base
                self.pressed_rewards_capped_loo += paid_loo
                self.settled_margin_base += result.settled_margin_base
                self.settled_margin_loo += result.settled_margin_loo

        self.orders_compared += len(result.order_diffs)
        self.orders_only_with_solver += diffs.only_with_solver
        self.orders_only_without_solver += diffs.only_without_solver
        self.orders_unsettled_base += diffs.unsettled_base
        self.orders_lost_to_lateness += diffs.late_base

        if result.anything_moved:
            self.changed.append(result)

    def _add_quote_checks(self, result: AuctionCounterfactual) -> None:
        self.auctions_quote_checked += bool(result.quote_carried_uids)
        self.quote_substituted_uid_set |= result.quote_substituted_uids
        self.quote_substituted_orders = len(self.quote_substituted_uid_set)
        self.auctions_quote_substituted += bool(result.quote_substituted_uids)
        self.quote_unclearable_uid_set |= result.quote_limit_unclearable_uids
        self.quote_limit_unclearable_orders = len(self.quote_unclearable_uid_set)
        self.auctions_quote_shift_would_change_winners += (
            result.quote_shift_winner_set_changed
        )
        if result.quote_shift_winner_set_changed:
            self.quote_shift_auctions.append(result.auction_id)

    def _add_quote_only(self, result: AuctionCounterfactual) -> None:
        """An auction no removed solver bid in, kept only for its replacement quotes."""
        self.auctions_quote_only += 1
        self._add_quote_checks(result)
        if not result.quote_substituted_uids:
            return
        diffs = [
            d
            for d in result.order_diffs
            if d.order_uid in result.quote_substituted_uids
        ]
        if not all(d.fees_known for d in diffs):
            self.auctions_fees_incomplete += 1
        else:
            self.quote_fee_recharge += result.quote_fee_recharge
            self.quote_partner_fee_recharge += result.quote_fee_recharge_partner
        if result.anything_moved:
            self.changed.append(result)

    def largest_moves(self, limit: int = 10) -> list[AuctionCounterfactual]:
        return sorted(self.changed, key=lambda r: -abs(r.delta_surplus))[:limit]
