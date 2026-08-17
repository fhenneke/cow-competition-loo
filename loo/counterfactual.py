"""Re-run one auction with a solver removed, and diff the two outcomes.

The counterfactual is a **full re-arbitration** — steps 3-6 of
docs/winner-selection.md, not just the winner pick. Removing a solver can lower a
per-pair baseline, which can *un-filter* a solution that was unfair only because the
removed solver set that baseline high. `compute_reference_scores` deliberately does not
do this (it re-runs step 5 on the already-filtered set), so it is not the right
primitive here; see docs/winner-selection.md#reference-scores-do-not-re-filter.

Both sides of the comparison are produced by the same code from the same bids, so model
error largely cancels in the difference. What does *not* cancel is which outcome each
winner's orders are given, which is the one judgement call this module makes — see
`OUTCOME_RULE`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from .extract import AuctionBundle, Bid, Settlement, SolutionCap
from .primitives import MAX_WINNERS, Pair, price_in_eth
from .rewards import SolverReward, Win, uncapped_rewards
from .valuation import Mode, ValuedBid, build_solutions
from .winner_selection import (
    Ranking,
    Solution,
    arbitrate,
    compute_reference_outcomes,
    compute_reference_scores,
)

PRICE_IMBALANCE_THRESHOLD = 2
"""A solution is price-suspect when its orders' executed amounts, valued through the
auction's own native prices, disagree between the sell side and the buy side by more
than this factor. A real trade exchanges roughly equal value, so a large imbalance
means one token's native price is wrong — and a wrong price fabricates score, surplus
and rewards out of nothing. Measured on the 2026-08-01..04 validation window: healthy
winners sit within 1.04x, while the window's five largest "whales" are 14,000x apart,
because one obscure token's price is persistently ~15,300x too high — see
docs/analytics-db.md#native-prices-can-be-plain-wrong. The threshold is deliberately
loose; it separates 1.04 from 14,000, not signal from noise."""

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
"""How a winner's orders are given an outcome. `inherited` is the default.

Both rules apply to **both** sides identically, which is what makes the difference
meaningful: a solution that wins in the baseline and again without the removed solver gets
the same outcome in both, so it cancels exactly and only genuinely changed orders move
`delta_surplus`.

They differ only in what a *replacement* — a winner that never won for real, so nothing
was ever recorded about it — is taken to do. That is not a detail: 1,533 of the 10,301
winners in the 2026-08-01..04 validation window never settled, so the assumption is load-bearing.

- `inherited` reads settlement off the auction's token pairs. Whatever really happened to
  the pairs a replacement claims, happens to the replacement. Settlement therefore cancels
  out of `delta_surplus` entirely and the result measures the competition's *decision*,
  which is the question the surplus delta asks.
- `assume-settled` is the everything-lands-in-time scenario: every winner on both sides
  settles, so the comparison is about proposals alone.

A third rule, `observed` — a replacement assumed to settle while recorded winners keep
their real outcomes — was carried for a while as a "lower bound" and was **removed in
review** (D4): settlement attached to the solution is not a counterfactual anyone would defend
(it charges the baseline for real reverts while crediting the counterfactual with never
reverting), and a number nobody should quote is not made useful by calling it a bound.
Measured before removal it moved the answer by 2–4× the headline, which is the measure of
how wrong an unrealistic settlement assumption can be — not a bracket worth reporting.
"""


class MissingSettlementError(Exception):
    """A recorded winner has no settlement outcome under the `inherited` rule.

    Loud rather than defaulted: silently assuming a missing winner settled would inflate
    baseline surplus, and assuming it did not would deflate it. Both would read as a
    finding about the removed solver.
    """


class RelaxationError(Exception):
    """Removing a solver un-kept a solution belonging to someone else.

    Baselines are maxima over a set of solutions, so dropping solutions can only lower
    them, and fairness is therefore monotone: every solution kept in the baseline must
    still be kept once another solver is gone. A violation means the filter or the
    baseline computation is wrong, not that the counterfactual is interesting.
    """


def leave_one_out(
    solutions: Iterable[Solution],
    removed: frozenset[str],
    max_winners: int = MAX_WINNERS,
) -> Ranking:
    """`arbitrate` over everything the removed solver did not submit.

    A solver can have several solutions in one auction and all of them go, which is what
    `compute_reference_scores` does too (`arbitrator.rs:620`).
    """
    return arbitrate([s for s in solutions if s.solver not in removed], max_winners)


def un_filtered(baseline: Ranking, loo: Ranking, removed: frozenset[str]) -> frozenset[int]:
    """Solutions the fairness filter dropped in the baseline but keeps without `removed`.

    Raises `RelaxationError` on the impossible direction — see that exception.
    """
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


@dataclass(frozen=True)
class OrderOutcome:
    """What one order did under one side of the comparison."""

    executed: bool
    surplus_native: int | None
    """`None` when the order was not executed. Zero is a legitimate value for an
    executed order — an order filled at exactly its limit price has no surplus — so the
    flag and the amount have to stay separate."""
    contributes: bool
    """Does this order count as *user* surplus (`auction.rs:45`)? A JIT order whose
    owner is not in `surplus_capturing_jit_order_owners` is executed but its surplus
    accrues to the market maker, so it is carried at zero."""
    solution_uid: int | None = None
    solver: str | None = None
    observed: bool = False
    """True when this came from the recorded competition rather than from a proposal."""
    landed_late: bool = False
    """This order's batch really did fill, but after its deadline, so
    `Settlement.counts_as_executed` treats it as a failure. Tracked only where the record is
    this solution's own, which is where the discarded surplus is real."""
    volume_native: int | None = None
    """Native value of the received leg when executed, `None` otherwise — the
    denominator for relative price statements. Carried at 0 for a non-contributing
    order, mirroring `surplus_native`; neither enters any user statistic."""
    partially_fillable: bool = False
    """Carried so the aggregation can exclude partially fillable orders from the
    relative-price statistic: the two sides may execute different amounts, and Δsurplus
    over one side's volume is then a mixture of price and quantity."""


UNEXECUTED = OrderOutcome(executed=False, surplus_native=None, contributes=False)


def price_imbalanced(
    bid: Bid, prices: Mapping[str, int], threshold: int = PRICE_IMBALANCE_THRESHOLD
) -> bool:
    """Do this solution's two sides disagree about the value being traded?

    Orders missing a price on either side are skipped — nothing can be cross-checked
    for them — and a solution with nothing checkable is not suspect."""
    sell_value = buy_value = 0
    for order in bid.orders:
        sell_price = prices.get(order.sell_token)
        buy_price = prices.get(order.buy_token)
        if sell_price is None or buy_price is None:
            continue
        sell_value += price_in_eth(sell_price, order.executed_sell)
        buy_value += price_in_eth(buy_price, order.executed_buy)
    if not sell_value or not buy_value:
        return False
    return sell_value > threshold * buy_value or buy_value > threshold * sell_value


def recorded_executed_volume(
    valued: Iterable[ValuedBid],
    db_winner_uids: frozenset[int],
    *,
    outcome_rule: OutcomeRule,
    settled: Mapping[int, Settlement],
) -> tuple[int, int, int]:
    """(volume, fill-or-kill orders, all orders) the auction actually traded: what the
    **recorded** winners executed, under the outcome rule's settlement reading.

    The volume and the fill-or-kill count are over fill-or-kill user orders only — the
    denominator for "averaged over all traded volume", where a partially fillable
    order's Δsurplus would mix price and quantity. The third count is every executed
    user order regardless of fillability — the denominator of the coverage share,
    where an execution lost is an execution lost either way (D24). Summed over a
    window it counts order *executions*: a partially fillable order executing in five
    auctions contributes five.

    It reads the recorded winners rather than our re-arbitrated baseline,
    deliberately: the denominator must exist for every auction, including the ones
    the removed solver never bid in, and those are exactly the auctions the
    counterfactual never arbitrates. The record differs from our baseline in 5 of
    7,745 validation auctions — noise in a window denominator, and arguably the more
    literal reading of "volume users actually traded"."""
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
            order_volume = entry.valuation.order_volume_native.get(order.uid)
            if order_volume is None:
                # Non-contributing (JIT) order: not a user's volume.
                continue
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
    """Did the batch that really held each token pair settle?

    This is what the `inherited` rule reads a replacement's outcome off. Keying on
    `winner_pairs` is deliberate: those are the `as_erc20`-normalised pairs `pick_winners`
    claims, so they are precisely what makes one solution displace another. Two solutions
    that conflict hold the same pair, which is what "the same slot" means here.

    Recorded winners hold disjoint pairs — that is the invariant `pick_winners` enforces —
    so no pair gets two answers.
    """
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
) -> dict[Pair, tuple[int, Decimal]]:
    """Which recorded winner held each token pair, and its upper-cap contribution.

    The capped-reward estimate reads a replacement's cap off this, the same way the
    `inherited` outcome rule reads its settlement: the cap is `scaling_factor ×
    realised fees` and fees follow the orders, so the batch that really traded a
    replacement's pairs is the best available estimate of the fees the replacement
    would have realised. Self-consistent with settlement inheritance for free: a
    reverted slot realised no fees, so its cap is 0 exactly where its settlement is a
    revert."""
    by_pair: dict[Pair, tuple[int, Decimal]] = {}
    for uid in sorted(recorded_winner_uids):
        entry = by_uid.get(uid)
        if entry is None or uid not in caps:
            continue
        for pair in entry.solution.winner_pairs:
            by_pair[pair] = (uid, caps[uid].upper)
    return by_pair


@dataclass(frozen=True)
class SideCaps:
    """Upper-cap contribution per winning solution of one side."""

    by_uid: dict[int, Decimal | None]
    """`None` marks a replacement with nothing to inherit — its solver's
    `capped_reward` is then `None` rather than a guess."""
    inherited_solvers: frozenset[str]
    """Solvers whose cap includes at least one inherited contribution, i.e. whose
    capped reward is an estimate rather than a record."""
    double_inherited: int
    """Displaced winners whose cap was inherited by more than one replacement on this
    side, double-counting it. Expected ~0: a replacement usually claims exactly the
    displaced winner's pairs."""
    orphans: frozenset[int]


def winner_caps(
    winners: Iterable[ValuedBid],
    recorded_winner_uids: frozenset[int],
    caps: Mapping[int, SolutionCap],
    cap_by_pair: Mapping[Pair, tuple[int, Decimal]],
) -> SideCaps:
    """Assign each winner its upper-cap contribution: its own recorded cap where it
    won for real, the displaced slot's where it did not (D4's logic applied to fees)."""
    by_uid: dict[int, Decimal | None] = {}
    inherited: set[str] = set()
    orphans: set[int] = set()
    claimed: dict[int, int] = {}

    for entry in winners:
        uid = entry.bid.uid
        if uid in recorded_winner_uids and uid in caps:
            by_uid[uid] = caps[uid].upper
            continue
        displaced: dict[int, Decimal] = {}
        for pair in entry.solution.winner_pairs:
            found = cap_by_pair.get(pair)
            if found is not None:
                displaced_uid, cap = found
                displaced[displaced_uid] = cap
        if not displaced:
            by_uid[uid] = None
            orphans.add(uid)
            continue
        by_uid[uid] = sum(displaced.values(), Decimal(0))
        inherited.add(entry.bid.solver)
        for displaced_uid in displaced:
            claimed[displaced_uid] = claimed.get(displaced_uid, 0) + 1

    return SideCaps(
        by_uid=by_uid,
        inherited_solvers=frozenset(inherited),
        double_inherited=sum(1 for count in claimed.values() if count > 1),
        orphans=frozenset(orphans),
    )


@dataclass(frozen=True)
class SideOutcomes:
    """One side of the comparison, order by order."""

    orders: dict[str, OrderOutcome]
    replacements: frozenset[int]
    """Winners that never won for real, so nothing was recorded about their settlement."""
    inherited_reverts: frozenset[int]
    """Replacements taken as reverting because the batch that held their pairs reverted.
    Empty under any rule but `inherited`."""
    orphans: frozenset[int]
    """Replacements whose settlement had to be assumed rather than derived — the slot
    rule's mapping-failure rate (D4). Under `inherited` these are the
    replacements claiming a pair no recorded winner held, so there was nothing to
    inherit. Empty under `assume-settled`, which does not consult the record at all."""
    solution_executed: dict[int, bool] = field(default_factory=dict[int, bool])
    """The per-winner settlement decision the orders above were given, by solution uid.
    This is what the reward formula's `observed_score` consumes — keeping it here rather
    than re-deriving it guarantees the surplus and reward sides of one auction can never
    disagree about which winners delivered (D5)."""


def side_outcomes(
    winners: Iterable[ValuedBid],
    *,
    outcome_rule: OutcomeRule,
    settled: Mapping[int, Settlement],
    recorded_winner_uids: frozenset[int],
    inherit: Mapping[Pair, bool] | None = None,
) -> SideOutcomes:
    """Outcome per order for one side, implementing `OUTCOME_RULE`.

    `inherit` is the recorded competition's pair-to-settlement map from
    `recorded_settlement_by_pair`. **Both** sides are passed the same one, so the rule is
    a property of the auction rather than of the side being evaluated: a winner that won
    for real reads its own outcome out of it, since it held its own pairs.

    An order counts as executed only when its batch landed **in time**. A late settlement is
    treated as a failure with zero surplus even though its orders really did fill — see
    `Settlement.counts_as_executed` for why, and `Analysis.orders_lost_to_lateness` for the
    cost of that choice.

    `pick_winners` gives winners disjoint token pairs and an order fixes its own pair, so
    no two winners can trade the same order. Asserted rather than assumed, and confirmed
    over the 2026-08-01..04 validation window: no auction has an order in two winners' executions, and no solution
    trades the same order twice.
    """
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
            # `inherited`, the slot rule: whatever really happened to the pairs this
            # replacement claims, happens to it. A batch spanning several slots needs all
            # of them to have settled, since one reverting leg would have taken the batch
            # with it.
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
            contributes = order.uid in entry.valuation.order_surplus_native
            outcomes[order.uid] = OrderOutcome(
                executed=executed,
                surplus_native=(
                    entry.valuation.order_surplus_native.get(order.uid, 0)
                    if executed
                    else None
                ),
                contributes=contributes,
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
    """One order, both sides."""

    order_uid: str
    contributes: bool
    executed_base: bool
    executed_loo: bool
    surplus_base: int | None
    surplus_loo: int | None
    solver_base: str | None = None
    solver_loo: str | None = None
    observed_base: bool = False
    observed_loo: bool = False
    """Did that side's outcome come from this solution's own record, rather than being
    inherited from the slot or assumed?

    `observed_base and not executed_base` marks an order the baseline really did lose to a
    reverted batch. Under `inherited` the replacement holding that slot reverts too, so
    the order cancels out of `delta_surplus`.
    """

    late_base: bool = False
    late_loo: bool = False
    """The batch really filled this order, but after its deadline, so it is carried as a
    failure. The one place this analysis knowingly discards surplus a user did receive."""

    volume_base: int | None = None
    volume_loo: int | None = None
    """Native value of the received leg on each executed side. On an order that trades
    on both sides, `delta_surplus / volume_base` is the relative price change the
    counterfactual hands the user — the buy-token price cancels out of the ratio, so a
    wrong native price cannot fabricate it."""
    partially_fillable: bool = False
    """Excluded from the relative-price statistic: the two sides may execute different
    amounts, and Δsurplus over one side's volume is then not a price change."""

    @property
    def unsettled_base(self) -> bool:
        return self.observed_base and not self.executed_base

    @property
    def delta_surplus(self) -> int:
        """Counterfactual minus actual, substituting 0 for an unexecuted order.

        Negative means the user would have received less without the removed solver.
        How a change is turned into a statement about the solver is the reader's step,
        not the pipeline's — deltas describe the scenario.
        """
        return (self.surplus_loo or 0) - (self.surplus_base or 0)

    @property
    def only_with_solver(self) -> bool:
        """Executed only because the removed solver was there — the coverage question."""
        return self.executed_base and not self.executed_loo

    @property
    def only_without_solver(self) -> bool:
        """The reverse: an order that only trades once the solver is gone.

        Not a defect. `pick_winners` takes a solution only if *every* pair it touches is
        unclaimed, so a batch can be blocked outright by a rival holding one of its pairs.
        Remove that rival and the whole batch wins, including the orders no one else bid
        on. Observed on auction 13488369: a two-order batch was blocked by a single-order
        winner, and removing the winner filled both orders.
        """
        return self.executed_loo and not self.executed_base


def diff_outcomes(
    base: Mapping[str, OrderOutcome], loo: Mapping[str, OrderOutcome]
) -> tuple[OrderDiff, ...]:
    """Per-order diff over the union of orders traded by either side's winners."""
    diffs: list[OrderDiff] = []
    for order_uid in sorted(set(base) | set(loo)):
        left = base.get(order_uid, UNEXECUTED)
        right = loo.get(order_uid, UNEXECUTED)
        diffs.append(
            OrderDiff(
                order_uid=order_uid,
                contributes=left.contributes or right.contributes,
                executed_base=left.executed,
                executed_loo=right.executed,
                surplus_base=left.surplus_native,
                surplus_loo=right.surplus_native,
                solver_base=left.solver,
                solver_loo=right.solver,
                observed_base=left.observed,
                observed_loo=right.observed,
                late_base=left.landed_late,
                late_loo=right.landed_late,
                volume_base=left.volume_native,
                volume_loo=right.volume_native,
                # A property of the order itself, so any side that saw it agrees.
                partially_fillable=left.partially_fillable or right.partially_fillable,
            )
        )
    return tuple(diffs)


@dataclass(frozen=True)
class AuctionCounterfactual:
    """One auction, arbitrated twice."""

    auction_id: int
    n_solutions: int
    solver_present: bool
    """Did the removed solver bid at all? When it did not, the two sides are identical by
    construction and the auction is skipped — but it stays in the denominator, so rates
    are over the whole window rather than over the solver's own auctions."""
    solver_won_baseline: bool = False
    solver_won_db: bool = False
    """Whether the removed solver won by our own arbitration and by the DB's record.
    The first is what the counterfactual is measured against; the second is the fact
    about reality the comparison table reports."""
    baseline_winner_uids: frozenset[int] = frozenset()
    loo_winner_uids: frozenset[int] = frozenset()
    baseline_winning_total: int = 0
    loo_winning_total: int = 0
    """Sum of the winners' totals — recorded score in score mode. The reward formula
    starts here."""
    baseline_reference_scores: dict[str, int] = field(default_factory=dict[str, int])
    loo_reference_scores: dict[str, int] = field(default_factory=dict[str, int])
    solver_set_reference_for: frozenset[str] = frozenset()
    """Solvers whose baseline reference score the removed solver contributed to."""
    baseline_rewards: dict[str, SolverReward] = field(default_factory=dict[str, SolverReward])
    loo_rewards: dict[str, SolverReward] = field(default_factory=dict[str, SolverReward])
    """Rewards per winning solver on each side: uncapped always, capped when the
    recorded caps were supplied. Score mode only — in surplus mode the totals are not
    scores, so the formula's quantities do not exist and both dicts stay empty. The
    settlement decision inside `observed_score` is the same one the order outcomes
    above were given, so the surplus and reward sides always agree on which winners
    delivered (D5)."""
    cap_double_inherited: int = 0
    """Displaced winners whose cap two different replacements inherited, so it is
    double-counted in the capped estimate."""
    cap_orphans_loo: frozenset[int] = frozenset()
    """LOO winners whose cap could not even be estimated; their solvers' capped
    rewards are `None` and the auction drops out of the capped aggregate."""
    price_suspect_uids: frozenset[int] = frozenset()
    """Solutions whose sell-side and buy-side trade values disagree by more than
    `PRICE_IMBALANCE_THRESHOLD`, meaning a native price is probably wrong and every
    native-denominated number this auction contributes is fabricated with it."""
    block_deadline: int = 0
    """Deadline block, carried so Δrewards can be converted native -> COW at the
    auction's own accounting-period rate."""
    executed_volume_base: int = 0
    executed_orders_base: int = 0
    executed_orders_all_base: int = 0
    """What the auction really traded under the outcome rule
    (`recorded_executed_volume`): received-leg value and count of the recorded
    winners' executed fill-or-kill user orders, and the count of *every* executed
    user order, partially fillable included. Computed whether or not the removed
    solver bid — the window sums are the denominators of "averaged over all traded
    volume" and of the coverage share (D24)."""
    un_filtered_uids: frozenset[int] = frozenset()
    un_filtered_winner_uids: frozenset[int] = frozenset()
    order_diffs: tuple[OrderDiff, ...] = ()
    replacements_base: frozenset[int] = frozenset()
    replacements_loo: frozenset[int] = frozenset()
    """Winners that never won for real, so nothing was recorded about their settlement.
    Non-empty on the baseline side only where our arbitration disagrees with the DB about
    who won."""
    inherited_reverts_loo: frozenset[int] = frozenset()
    """Replacements the `inherited` rule takes as reverting, because the batch that really
    held their token pairs reverted."""
    orphans_base: frozenset[int] = frozenset()
    orphans_loo: frozenset[int] = frozenset()
    """Replacements whose settlement had to be assumed rather than derived — the slot
    rule's mapping failures."""
    valuation_failures: tuple[tuple[int, str], ...] = ()
    baseline_matches_db: bool = True
    """Does our baseline winner set equal `is_winner`? Where it does not, the two sides
    are still comparable to each other, but the baseline is not the recorded auction —
    Validation measured this at 5 auctions in 7,745."""

    @property
    def winner_set_changed(self) -> bool:
        return self.baseline_winner_uids != self.loo_winner_uids

    @property
    def filter_relaxed(self) -> bool:
        return bool(self.un_filtered_uids)

    @property
    def reference_scores_moved(self) -> bool:
        """Did removing the solver change any surviving winner's reference score?

        This happens **without** the winner set changing, and often: one of the removed
        solver's *non-winning* solutions can be a winner of the without-`s` pick inside
        `compute_reference_scores`, so `s`'s reference score falls when the solver goes even
        though nobody's win moves. Reference scores are the denominator of the uncapped
        reward, so the reward side needs these auctions kept.
        """
        return self.baseline_reference_scores != self.loo_reference_scores

    @property
    def anything_moved(self) -> bool:
        """Is this auction worth retaining rather than just counting?

        Δsurplus only moves when the winner set does, but rewards also move on a reference
        score alone, so retention is deliberately wider than `winner_set_changed`.
        """
        return (
            self.winner_set_changed or self.filter_relaxed or self.reference_scores_moved
        )

    @property
    def delta_surplus(self) -> int:
        """Change in native user surplus in the counterfactual: without-solver minus
        with-solver, over user orders. Negative means users would have received less."""
        return sum(d.delta_surplus for d in self.order_diffs if d.contributes)

    @property
    def surplus_base(self) -> int:
        return sum(d.surplus_base or 0 for d in self.order_diffs if d.contributes)

    @property
    def surplus_loo(self) -> int:
        return sum(d.surplus_loo or 0 for d in self.order_diffs if d.contributes)

    @property
    def rewards_base(self) -> int:
        return sum(r.uncapped_reward for r in self.baseline_rewards.values())

    @property
    def rewards_loo(self) -> int:
        return sum(r.uncapped_reward for r in self.loo_rewards.values())

    @property
    def delta_rewards(self) -> int:
        """Change in uncapped native rewards in the counterfactual: without-solver minus
        with-solver.

        Positive means the protocol would pay more without the solver — the usual case,
        since rivals' reference scores fall when its solutions leave the without-`s`
        pick, so their rewards grow."""
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
        return self._capped_total(self.baseline_rewards)

    @property
    def rewards_loo_capped(self) -> Decimal | None:
        return self._capped_total(self.loo_rewards)

    @property
    def delta_rewards_capped(self) -> Decimal | None:
        """The capped estimate of `delta_rewards` — the payout-scale answer. `None`
        when either side has a winner whose cap could not be estimated."""
        base, loo = self.rewards_base_capped, self.rewards_loo_capped
        if base is None or loo is None:
            return None
        return loo - base

    @property
    def price_suspect(self) -> bool:
        return bool(self.price_suspect_uids)


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
) -> AuctionCounterfactual:
    """Arbitrate one auction with and without `removed`, and diff the two.

    `solution_caps` (recorded winners only) switches on the capped-reward estimate;
    without it only uncapped rewards are computed."""
    settled = settled or {}
    db_winner_uids = frozenset(b.uid for b in bundle.bids if b.is_winner)
    present = any(b.solver in removed for b in bundle.bids)
    suspects = frozenset(
        b.uid for b in bundle.bids if price_imbalanced(b, bundle.native_prices)
    )

    valued, failures = build_solutions(
        bundle.bids, bundle.native_prices, weth, mode=mode
    )

    executed_volume = executed_orders = executed_orders_all = 0
    if not failures:
        executed_volume, executed_orders, executed_orders_all = recorded_executed_volume(
            valued, db_winner_uids, outcome_rule=outcome_rule, settled=settled
        )

    if failures or not present:
        # A valuation failure means we cannot arbitrate this auction faithfully at all.
        # Validation measured zero over three days, so it is reported rather than tolerated.
        return AuctionCounterfactual(
            auction_id=bundle.auction_id,
            n_solutions=len(bundle.bids),
            solver_present=present,
            solver_won_db=bool(removed & {b.solver for b in bundle.bids if b.is_winner}),
            valuation_failures=tuple(sorted(failures.items())),
            block_deadline=bundle.block_deadline,
            price_suspect_uids=suspects,
            executed_volume_base=executed_volume,
            executed_orders_base=executed_orders,
            executed_orders_all_base=executed_orders_all,
        )

    solutions = [v.solution for v in valued]
    by_uid = {v.bid.uid: v for v in valued}

    baseline = arbitrate(solutions, max_winners)
    loo = leave_one_out(solutions, removed, max_winners)
    relaxed = un_filtered(baseline, loo, removed)

    # One inheritance source for both sides, so the settlement of a slot is a fact about
    # the auction and not about which side is being evaluated.
    inherit = (
        recorded_settlement_by_pair(by_uid, db_winner_uids, settled)
        if outcome_rule == "inherited"
        else None
    )

    base = side_outcomes(
        [by_uid[s.solution_uid] for s in baseline.winners],
        outcome_rule=outcome_rule,
        settled=settled,
        recorded_winner_uids=db_winner_uids,
        inherit=inherit,
    )
    loo_side = side_outcomes(
        [by_uid[s.solution_uid] for s in loo.winners],
        outcome_rule=outcome_rule,
        settled=settled,
        recorded_winner_uids=db_winner_uids,
        inherit=inherit,
    )

    baseline_references = compute_reference_outcomes(baseline, max_winners)
    baseline_reference_scores = {
        solver: ref.score for solver, ref in baseline_references.items()
    }
    loo_reference_scores = compute_reference_scores(loo, max_winners)

    # Rewards exist only in score mode: the formula's quantities are scores, and in
    # surplus mode `total` is user surplus instead. The settled flag fed to
    # `observed_score` is the same per-solution decision the order outcomes got, so
    # both sides of one auction, and the surplus and reward views of it, are always
    # consistent under whichever outcome rule is in force.
    baseline_rewards: dict[str, SolverReward] = {}
    loo_rewards: dict[str, SolverReward] = {}
    cap_double_inherited = 0
    cap_orphans: frozenset[int] = frozenset()
    if mode == "score":
        # The caps come from the record regardless of the outcome rule: own cap for a
        # recorded winner, the displaced slot's for a replacement. Auction-level cap
        # facts (lower cap, exclusion) ride on every recorded winner's row.
        caps: Mapping[int, SolutionCap] = solution_caps if solution_caps else {}
        any_cap = next(iter(caps.values()), None)
        lower_cap = any_cap.lower if any_cap else None
        excluded = any_cap.excluded if any_cap else False
        cap_by_pair = recorded_caps_by_pair(by_uid, db_winner_uids, caps)

        base_caps = winner_caps(
            [by_uid[s.solution_uid] for s in baseline.winners],
            db_winner_uids, caps, cap_by_pair,
        )
        loo_caps = winner_caps(
            [by_uid[s.solution_uid] for s in loo.winners],
            db_winner_uids, caps, cap_by_pair,
        )
        cap_double_inherited = base_caps.double_inherited + loo_caps.double_inherited
        cap_orphans = loo_caps.orphans

        baseline_rewards = uncapped_rewards(
            [
                Win(
                    s.solver,
                    s.total,
                    base.solution_executed[s.solution_uid],
                    base_caps.by_uid[s.solution_uid],
                )
                for s in baseline.winners
            ],
            baseline_reference_scores,
            lower_cap=lower_cap,
            excluded=excluded,
            caps_inherited=base_caps.inherited_solvers,
        )
        loo_rewards = uncapped_rewards(
            [
                Win(
                    s.solver,
                    s.total,
                    loo_side.solution_executed[s.solution_uid],
                    loo_caps.by_uid[s.solution_uid],
                )
                for s in loo.winners
            ],
            loo_reference_scores,
            lower_cap=lower_cap,
            excluded=excluded,
            caps_inherited=loo_caps.inherited_solvers,
        )

    return AuctionCounterfactual(
        auction_id=bundle.auction_id,
        n_solutions=len(bundle.bids),
        solver_present=True,
        solver_won_baseline=any(s.solver in removed for s in baseline.winners),
        solver_won_db=bool(removed & {b.solver for b in bundle.bids if b.is_winner}),
        baseline_winner_uids=baseline.winner_uids,
        loo_winner_uids=loo.winner_uids,
        baseline_winning_total=baseline.winning_score,
        loo_winning_total=loo.winning_score,
        baseline_reference_scores=baseline_reference_scores,
        loo_reference_scores=loo_reference_scores,
        solver_set_reference_for=frozenset(
            solver for solver, ref in baseline_references.items() if ref.setters & removed
        ),
        baseline_rewards=baseline_rewards,
        loo_rewards=loo_rewards,
        cap_double_inherited=cap_double_inherited,
        cap_orphans_loo=cap_orphans,
        price_suspect_uids=suspects,
        block_deadline=bundle.block_deadline,
        executed_volume_base=executed_volume,
        executed_orders_base=executed_orders,
        executed_orders_all_base=executed_orders_all,
        un_filtered_uids=relaxed,
        un_filtered_winner_uids=relaxed & loo.winner_uids,
        order_diffs=diff_outcomes(base.orders, loo_side.orders),
        replacements_base=base.replacements,
        replacements_loo=loo_side.replacements,
        inherited_reverts_loo=loo_side.inherited_reverts,
        orphans_base=base.orphans,
        orphans_loo=loo_side.orphans,
        baseline_matches_db=baseline.winner_uids == db_winner_uids,
    )


@dataclass
class Analysis:
    """Window aggregate.

    `auctions` counts every auction in the window, including the ones the removed solver
    never bid in — those stay in the denominator (D10) so the rates describe the window
    rather than the solver's own subset.
    """

    solver: str = ""
    addresses: frozenset[str] = frozenset()
    mode: Mode = "score"
    outcome_rule: OutcomeRule = "inherited"

    auctions: int = 0
    auctions_with_solver: int = 0
    auctions_skipped: int = 0
    """Auctions abandoned because a bid could not be valued."""
    auctions_solver_won_baseline: int = 0
    auctions_solver_won_db: int = 0
    """`auctions_solver_won_db` is counted over every auction, since `is_winner` is always
    available; `auctions_solver_won_baseline` only over the auctions actually arbitrated. The
    two are comparable exactly when `auctions_skipped` is 0."""
    auctions_winner_set_changed: int = 0
    auctions_filter_relaxed: int = 0
    auctions_newly_kept_won: int = 0
    auctions_baseline_differs_from_db: int = 0

    surplus_base: int = 0
    surplus_loo: int = 0

    window_volume_base: int = 0
    window_orders_base: int = 0
    window_order_executions_base: int = 0
    """What the window's recorded winners executed under the outcome rule:
    received-leg value and count of every fill-or-kill user order — the denominator
    of "averaged over all traded volume" — and the count of every executed user
    order, partially fillable included, which is the denominator of the coverage
    share (D24). The last counts order *executions*: an order is counted once per
    auction it executes in, so a partially fillable order weighs as often as it
    trades. Unlike `surplus_base`, all three are summed over **every** clean analysed
    auction, present solver or not (D10's denominator logic): an auction the solver
    never bid in has zero price change but its volume still happened."""

    rewards_base: int = 0
    rewards_loo: int = 0
    """Uncapped native rewards summed over every winning solver of every arbitrated
    auction — i.e. over the auctions the removed solver bid in, since the others cancel
    identically and are skipped. Score mode only; zero in surplus mode."""
    removed_reward_base: int = 0
    """The removed solver's own share of `rewards_base`. The rest of the delta is the
    change in rivals' rewards, which is usually negative — without the solver their
    reference scores fall, so the protocol pays them more."""
    auctions_rewards_moved: int = 0
    """Auctions where any solver's uncapped reward differs between the sides. Wider
    than `auctions_winner_set_changed` (D8): a reference score moving alone moves a
    reward."""
    negative_rewards_base: int = 0
    negative_rewards_loo: int = 0
    negative_reward_sum_base: int = 0
    negative_reward_sum_loo: int = 0
    """(count, native sum) of negative uncapped rewards on each side. The real payout
    clamps these at `lower_reward_cap` (-0.01 ETH on mainnet), and the uncapped penalty
    for a failed settlement is `-reference_score` — orders of magnitude larger. Reported
    so the cost of stopping at uncapped rewards stays visible."""

    capped_estimate: bool = False
    """Did the caller supply recorded caps? Set by the CLI in score mode. The capped
    quantities below are only aggregated when this is on."""
    rewards_base_capped: Decimal = Decimal(0)
    rewards_loo_capped: Decimal = Decimal(0)
    auctions_capped: int = 0
    auctions_capped_skipped: int = 0
    """Auctions dropped from the capped aggregate because some winner's cap could not
    be estimated. Both sides are dropped together, so the capped delta stays a
    like-for-like comparison over `auctions_capped`."""
    cap_double_inherited: int = 0
    cap_orphans: int = 0

    exclude_price_suspect: bool = True
    """Auctions carrying a solution whose two trade-value sides disagree by more than
    `PRICE_IMBALANCE_THRESHOLD` have a wrong native price fabricating every
    native-denominated number they touch, so by default they are **excluded from every
    statistic** — they stay in `auctions` and are named in `price_suspect_auctions`,
    and contribute nothing else. Measured on the 2026-08-01..04 validation window it is 44 of
    7,745 auctions (0.6%), but those 44 carried 82% of Sector's Δsurplus and 97% of
    its uncapped Δrewards, all fabricated. Switching this off (`--include-price-suspects`)
    keeps them in every number instead; the flagged ids are reported either way."""
    price_suspect_auctions: list[int] = field(default_factory=list[int])

    missing_data_auctions: list[int] = field(default_factory=list[int])
    """Auctions excluded before arbitration because a traded order is in neither order
    table (D17) — `jit_orders` only records the JIT orders of *settled* batches, so an
    unsettled solution's JIT legs are unrecoverable and even their pair claims are
    unknown. Filled by `extract.load_auctions`, never by `add`: unlike price suspects
    these auctions are not counted in `auctions` because they were never arbitrated.
    Measured over 2026-07-12..08-12 mainnet: ~600 of 78,271 auctions (0.8%)."""

    orders_compared: int = 0
    orders_only_with_solver: int = 0
    orders_only_without_solver: int = 0
    orders_unsettled_base: int = 0
    """User orders the baseline lost to a settlement failure. Under `inherited` a
    replacement holding the same slot loses them too, so they cancel out of
    `delta_surplus`; under `assume-settled` neither side loses them."""
    orders_lost_to_lateness: int = 0
    """Of those, the ones whose batch *did* fill, just after its deadline. These carry real
    user surplus that `Settlement.counts_as_executed` deliberately discards, so this is the
    price of aligning the surplus side with the reward side. Reported rather than folded in,
    because it is the only figure here that is knowingly not what happened."""
    jit_orders_only_with_solver: int = 0
    jit_orders_only_without_solver: int = 0
    """Non-contributing orders, counted apart from user orders: their surplus accrues to
    a market maker, so they are not orders a user would otherwise have lost."""

    replacements_base: int = 0
    replacements_loo: int = 0
    """Winning solutions that never won for real, so nothing was recorded about their
    settlement. On the counterfactual side this is most of them, by construction."""
    inherited_reverts_loo: int = 0
    """Of those, how many the `inherited` rule takes as reverting because the batch that
    really held their token pairs reverted."""
    orphans_base: int = 0
    orphans_loo: int = 0
    """Replacements whose settlement had to be assumed rather than derived, because no
    recorded winner held any of their pairs — the slot rule's mapping-failure rate. It
    is the honest measure of how far the `inherited` rule falls back on assumption."""

    reference_influence: int = 0
    """(auction, solver) pairs where the removed solver helped set another solver's
    baseline reference score — i.e. where its absence moves a reward."""

    valuation_failures: list[tuple[int, int, str]] = field(
        default_factory=list[tuple[int, int, str]]
    )
    changed: list[AuctionCounterfactual] = field(default_factory=list[AuctionCounterfactual])
    """Auctions where the winner set changed or the filter relaxed. Auctions where
    nothing moved carry no information beyond their counters, so they are not kept."""

    @property
    def delta_surplus(self) -> int:
        """Counterfactual minus actual: negative means users would have received less
        without the solver. All deltas describe the scenario's change; turning a change
        into a statement about the solver is deliberately left to the reader."""
        return self.surplus_loo - self.surplus_base

    @property
    def delta_rewards(self) -> int:
        """Counterfactual minus actual: positive means the protocol would pay more
        without the solver."""
        return self.rewards_loo - self.rewards_base

    @property
    def delta_rewards_capped(self) -> Decimal:
        """The capped estimate of `delta_rewards`, over `auctions_capped`."""
        return self.rewards_loo_capped - self.rewards_base_capped

    def add(self, result: AuctionCounterfactual) -> None:
        self.auctions += 1
        if result.price_suspect:
            self.price_suspect_auctions.append(result.auction_id)
            if self.exclude_price_suspect:
                # Excluded from everything, including the recorded-win count: every
                # remaining statistic then describes the same, clean auction set.
                return
        if result.valuation_failures:
            self.auctions_skipped += 1
            self.valuation_failures.extend(
                (result.auction_id, uid, error) for uid, error in result.valuation_failures
            )
        self.auctions_solver_won_db += result.solver_won_db
        if result.valuation_failures:
            return
        # Before the solver-present return: the window denominator covers every clean
        # analysed auction, not just the ones the solver bid in.
        self.window_volume_base += result.executed_volume_base
        self.window_orders_base += result.executed_orders_base
        self.window_order_executions_base += result.executed_orders_all_base
        if not result.solver_present:
            return

        self.auctions_with_solver += 1
        self.auctions_solver_won_baseline += result.solver_won_baseline
        self.auctions_winner_set_changed += result.winner_set_changed
        self.auctions_filter_relaxed += result.filter_relaxed
        self.auctions_newly_kept_won += bool(result.un_filtered_winner_uids)
        self.auctions_baseline_differs_from_db += not result.baseline_matches_db
        self.surplus_base += result.surplus_base
        self.surplus_loo += result.surplus_loo
        self.rewards_base += result.rewards_base
        self.rewards_loo += result.rewards_loo
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
            base_capped = result.rewards_base_capped
            loo_capped = result.rewards_loo_capped
            if base_capped is None or loo_capped is None:
                self.auctions_capped_skipped += 1
            else:
                self.auctions_capped += 1
                self.rewards_base_capped += base_capped
                self.rewards_loo_capped += loo_capped
        self.cap_double_inherited += result.cap_double_inherited
        self.cap_orphans += len(result.cap_orphans_loo)

        self.replacements_base += len(result.replacements_base)
        self.replacements_loo += len(result.replacements_loo)
        self.inherited_reverts_loo += len(result.inherited_reverts_loo)
        self.orphans_base += len(result.orphans_base)
        self.orphans_loo += len(result.orphans_loo)
        self.reference_influence += len(result.solver_set_reference_for)

        for diff in result.order_diffs:
            if diff.contributes:
                self.orders_compared += 1
                self.orders_only_with_solver += diff.only_with_solver
                self.orders_only_without_solver += diff.only_without_solver
                self.orders_unsettled_base += diff.unsettled_base
                self.orders_lost_to_lateness += diff.late_base
            else:
                self.jit_orders_only_with_solver += diff.only_with_solver
                self.jit_orders_only_without_solver += diff.only_without_solver

        if result.anything_moved:
            self.changed.append(result)

    def largest_moves(self, limit: int = 10) -> list[AuctionCounterfactual]:
        return sorted(self.changed, key=lambda r: -abs(r.delta_surplus))[:limit]
