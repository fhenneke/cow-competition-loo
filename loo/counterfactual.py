"""M2: re-run one auction with a solver removed, and diff the two outcomes.

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

from dataclasses import dataclass, field
from typing import Iterable, Literal, Mapping

from .extract import AuctionBundle, Settlement
from .primitives import MAX_WINNERS, Pair
from .valuation import Mode, ValuedBid, build_solutions
from .winner_selection import (
    Ranking,
    Solution,
    arbitrate,
    compute_reference_outcomes,
    compute_reference_scores,
)

OutcomeRule = Literal["inherited", "observed", "assume-settled"]

OUTCOME_RULE = {
    "inherited": (
        "settlement belongs to the slot rather than to the solver holding it: a winner "
        "that also won for real takes its own recorded outcome, and a replacement "
        "inherits the outcome of the recorded winner(s) that held its token pairs — so "
        "if the batch that really occupied those pairs reverted, the replacement reverts "
        "too"
    ),
    "observed": (
        "settlement belongs to the solution: a winner that won for real takes its own "
        "recorded outcome, and a replacement is assumed to settle because there is no "
        "record to consult"
    ),
    "assume-settled": (
        "every winning solution's orders execute at its proposed amounts on both "
        "sides, so settlement risk is excluded from the comparison entirely"
    ),
}
"""How a winner's orders are given an outcome. `inherited` is the default.

All three apply to **both** sides identically, which is what makes the difference
meaningful: a solution that wins in the baseline and again without the removed solver gets
the same outcome in both, so it cancels exactly and only genuinely changed orders move
`delta_surplus`.

The three differ only in what a *replacement* — a winner that never won for real, so
nothing was ever recorded about it — is taken to do. That is not a detail: 1,533 of the
10,301 winners in the M1 window never settled, so the assumption is load-bearing.

- `inherited` reads settlement off the auction's token pairs. Whatever really happened to
  the pairs a replacement claims, happens to the replacement. Settlement therefore cancels
  out of `delta_surplus` entirely and the result measures the competition's *decision*,
  which is the question M2 asks.
- `observed` credits every replacement with settling. It charges the baseline for real
  reverts while assuming the counterfactual never reverts, so it is biased against the
  removed solver — it is kept only as the pessimistic bound.
- `assume-settled` credits *everyone* with settling, baseline included, so it overstates what
  users really received wherever a winner reverted.

`delta_surplus` under `observed` is a lower bound on `inherited` **by construction**: the
two differ only on reverted slots, where `observed` credits the replacement with a positive
surplus and `inherited` credits it with nothing, and the baseline is zero either way.
`assume-settled` is *usually* the upper bound but not provably so — a reverted slot whose
replacement carries more user surplus than the winner it displaced pushes it below
`inherited`, which score-mode ranking permits since score is not surplus. Measured on the
window, the three came out ordered: 2.89 / 8.01 / 8.13 ETH for Sector.
"""


class MissingSettlementError(Exception):
    """A recorded winner has no settlement outcome under the `observed` rule.

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


UNEXECUTED = OrderOutcome(executed=False, surplus_native=None, contributes=False)


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
    """Replacements whose settlement had to be assumed rather than derived — PLAN.md
    section 5's "record how often the mapping fails". Under `inherited` these are the
    replacements claiming a pair no recorded winner held, so there was nothing to inherit;
    under `observed` every replacement is one. Empty under `assume-settled`, which does not
    consult the record at all."""


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
    over the M1 window: no auction has an order in two winners' executions, and no solution
    trades the same order twice.
    """
    outcomes: dict[str, OrderOutcome] = {}
    replacements: set[int] = set()
    inherited_reverts: set[int] = set()
    orphans: set[int] = set()
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
        elif outcome_rule == "inherited":
            # The slot rule: whatever really happened to the pairs this replacement
            # claims, happens to it. A batch spanning several slots needs all of them to
            # have settled, since one reverting leg would have taken the batch with it.
            displaced = [inherit[p] for p in entry.solution.winner_pairs if p in inherit]
            executed, observed = (all(displaced) if displaced else True), False
            if not displaced:
                orphans.add(uid)
            elif not executed:
                inherited_reverts.add(uid)
        else:
            # `observed`: nothing was ever recorded about a replacement, so it is
            # assumed to settle — the asymmetry that makes this rule the lower bound.
            executed, observed = True, False
            orphans.add(uid)

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
            )

    return SideOutcomes(
        orders=outcomes,
        replacements=frozenset(replacements),
        inherited_reverts=frozenset(inherited_reverts),
        orphans=frozenset(orphans),
    )


@dataclass(frozen=True)
class OrderDiff:
    """One order, both sides. The field names are PLAN.md section 5's."""

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
    reverted batch. Under `inherited` the replacement holding that slot reverts too, so the
    order cancels out of `delta_surplus`; under `observed` the replacement is credited with
    settling and the order becomes a spurious gain from removing the solver.
    """

    late_base: bool = False
    late_loo: bool = False
    """The batch really filled this order, but after its deadline, so it is carried as a
    failure. The one place this analysis knowingly discards surplus a user did receive."""

    @property
    def unsettled_base(self) -> bool:
        return self.observed_base and not self.executed_base

    @property
    def delta_surplus(self) -> int:
        """Baseline minus counterfactual, substituting 0 for an unexecuted order.

        Positive means users were better off *with* the removed solver in the auction.
        """
        return (self.surplus_base or 0) - (self.surplus_loo or 0)

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

        Under `observed` a settlement failure is a second route into this, since the
        baseline winner is only credited if it really settled while its replacement is
        assumed to. `inherited` closes that route.
        """
        return self.executed_loo and not self.executed_base


def diff_outcomes(
    base: Mapping[str, OrderOutcome], loo: Mapping[str, OrderOutcome]
) -> tuple[OrderDiff, ...]:
    """Per-order diff over the union of orders traded by either side's winners."""
    diffs = []
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
    about reality that PLAN.md section 7 reports."""
    baseline_winner_uids: frozenset[int] = frozenset()
    loo_winner_uids: frozenset[int] = frozenset()
    baseline_winning_total: int = 0
    loo_winning_total: int = 0
    """Sum of the winners' totals — recorded score in score mode. M3 starts here."""
    baseline_reference_scores: dict[str, int] = field(default_factory=dict)
    loo_reference_scores: dict[str, int] = field(default_factory=dict)
    solver_set_reference_for: frozenset[str] = frozenset()
    """Solvers whose baseline reference score the removed solver contributed to."""
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
    """Replacements whose settlement had to be assumed rather than derived — PLAN.md
    section 5's mapping failures."""
    valuation_failures: tuple[tuple[int, str], ...] = ()
    baseline_matches_db: bool = True
    """Does our baseline winner set equal `is_winner`? Where it does not, the two sides
    are still comparable to each other, but the baseline is not the recorded auction —
    M1 measured this at 5 auctions in 7,745."""

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
        reward, so M3 needs these auctions kept.
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
        """Native surplus users got with the solver, minus without it, over user orders."""
        return sum(d.delta_surplus for d in self.order_diffs if d.contributes)

    @property
    def surplus_base(self) -> int:
        return sum(d.surplus_base or 0 for d in self.order_diffs if d.contributes)

    @property
    def surplus_loo(self) -> int:
        return sum(d.surplus_loo or 0 for d in self.order_diffs if d.contributes)


def analyse_auction(
    bundle: AuctionBundle,
    weth: str,
    removed: frozenset[str],
    *,
    mode: Mode = "score",
    max_winners: int = MAX_WINNERS,
    outcome_rule: OutcomeRule = "inherited",
    settled: Mapping[int, Settlement] | None = None,
) -> AuctionCounterfactual:
    """Arbitrate one auction with and without `removed`, and diff the two."""
    settled = settled or {}
    db_winner_uids = frozenset(b.uid for b in bundle.bids if b.is_winner)
    present = any(b.solver in removed for b in bundle.bids)

    valued, failures = build_solutions(
        bundle.bids, bundle.native_prices, weth, mode=mode
    )

    if failures or not present:
        # A valuation failure means we cannot arbitrate this auction faithfully at all.
        # M1 measured zero over three days, so it is reported rather than tolerated.
        return AuctionCounterfactual(
            auction_id=bundle.auction_id,
            n_solutions=len(bundle.bids),
            solver_present=present,
            solver_won_db=bool(removed & {b.solver for b in bundle.bids if b.is_winner}),
            valuation_failures=tuple(sorted(failures.items())),
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
        baseline_reference_scores={
            solver: ref.score for solver, ref in baseline_references.items()
        },
        loo_reference_scores=compute_reference_scores(loo, max_winners),
        solver_set_reference_for=frozenset(
            solver for solver, ref in baseline_references.items() if ref.setters & removed
        ),
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
    never bid in — PLAN.md section 5 keeps those in the denominator so the rates describe
    the window rather than the solver's own subset.
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

    orders_compared: int = 0
    orders_only_with_solver: int = 0
    orders_only_without_solver: int = 0
    orders_unsettled_base: int = 0
    """User orders the baseline lost to a settlement failure. Under `inherited` a
    replacement holding the same slot loses them too, so they cancel; under `observed` the
    replacement is credited with settling and they are exactly where `delta_surplus` is
    biased against the removed solver."""
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
    really held their token pairs reverted. This is the count `observed` would instead have
    credited with settling."""
    orphans_base: int = 0
    orphans_loo: int = 0
    """Replacements whose settlement had to be assumed rather than derived, because no
    recorded winner held any of their pairs — PLAN.md section 5's mapping-failure rate. It
    is the honest measure of how far the `inherited` rule falls back on assumption."""

    reference_influence: int = 0
    """(auction, solver) pairs where the removed solver helped set another solver's
    baseline reference score — i.e. where its absence moves a reward in M3."""

    valuation_failures: list[tuple[int, int, str]] = field(default_factory=list)
    changed: list[AuctionCounterfactual] = field(default_factory=list)
    """Auctions where the winner set changed or the filter relaxed. Auctions where
    nothing moved carry no information beyond their counters, so they are not kept."""

    @property
    def delta_surplus(self) -> int:
        """Positive means users were better off with the solver in the competition."""
        return self.surplus_base - self.surplus_loo

    def add(self, result: AuctionCounterfactual) -> None:
        self.auctions += 1
        if result.valuation_failures:
            self.auctions_skipped += 1
            self.valuation_failures.extend(
                (result.auction_id, uid, error) for uid, error in result.valuation_failures
            )
        self.auctions_solver_won_db += result.solver_won_db
        if not result.solver_present or result.valuation_failures:
            return

        self.auctions_with_solver += 1
        self.auctions_solver_won_baseline += result.solver_won_baseline
        self.auctions_winner_set_changed += result.winner_set_changed
        self.auctions_filter_relaxed += result.filter_relaxed
        self.auctions_newly_kept_won += bool(result.un_filtered_winner_uids)
        self.auctions_baseline_differs_from_db += not result.baseline_matches_db
        self.surplus_base += result.surplus_base
        self.surplus_loo += result.surplus_loo
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
