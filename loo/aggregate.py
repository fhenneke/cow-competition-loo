"""Aggregate `analyse` reports into a per-solver comparison.

Consumes the JSON written by `loo analyse --out` rather than re-running the pipeline:
a full-window run takes ~5 minutes, and the report already carries every changed
auction's deltas, so window aggregation, medians and USD conversion are pure
post-processing.

Three shapes of statement come out of one report, and the table keeps them apart:

- **Sums** answer the window question but are whale-dominated — one auction carried 82%
  of Sector's Δsurplus — so every sum is reported with the median non-zero auction and
  the largest auction's share next to it.
- **The outcome rule is part of the number.** The headline is `inherited` — settlement
  attaches to the slot, the one scenario grounded in the record; `assume-settled`
  (everything lands in time) is the alternative reading. A figure quoted without its
  rule would be a choice disguised as a result.
- **Two reward figures, never one.** Uncapped is the mechanism's exact accounting,
  three orders of magnitude away from payouts; capped is the payout answer but an
  estimate. The net-change row is always labelled with which one it nets against.

USD figures are display only. The analytics DB has no USD price table, so the rate is
implied per auction by the median stablecoin price in the auction's own price vector
(`primitives.USD_REFERENCE_TOKENS`); auctions without one use the window median.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, getcontext
from statistics import median
from typing import Any

# Capped rewards arrive as ~40-digit Decimal strings and are summed over thousands of
# auctions; Python's default 28-digit context silently rounds those sums, which the
# consistency check in `load_report` then misreads as file corruption. 78 digits covers
# a full uint256 — the same setting, for the same reason, as `loo.rewards`. Set here
# too because a notebook can import this module without ever touching that one.
getcontext().prec = 78

WEI = Decimal(10) ** 18

HEADLINE_RULE = "inherited"
ALTERNATIVE_RULES = ("assume-settled",)

REPORT_FORMAT = 13
"""Version marker of the `analyse --out` payload, required by `load_report`. Bumped
when the shape changes, so a stale file fails with "re-run analyse" instead of a
KeyError deep inside a comparison. Format 13 splits the fee: the fee deltas are computed
with the recorded quotes on both sides and the quote's own fee effect is carried apart, per
auction and per recharged order (`quote_fee_recharge*`, D28 revised). Format 12 links
orders to the competitions the gold tables do not hold (recovered from the raw layer) and
carries the per-source match count.
Format 11 recounts `quote_substituted_orders` as
distinct executed orders, adds the quote-only auction count and the two limit checks
(D33, D34), and retains fee-only auctions in `changed` — a format-10 file's per-auction
fee moves are short by the quote effect wherever nothing else moved. Within format 11
the optional `quote_shift_auctions` id list was added later and is read with a default,
so the one file shape that predates it still loads. Format 10 added
the quote side (M15). Format 8 added
`auctions_filter_guess_could_matter` — whether a guessed filter verdict could have moved
an auction's winners, the counterfactual's one filter number with no ground truth behind
it. Format 7 moved the fairness filter onto per-pair
**scores** (M14) — a behaviour change rather than a new key, and the reason the marker
moved: a format-6 report was filtered on user surplus, so tabulating it beside a
format-7 one would compare two different mechanisms. It also carries the filter's own
exactness (`multi_pair_solutions`, `multi_pair_filter_proxied`). Format 6 added the
protocol-fee deltas — total, partner share and the DAO's own revenue (M13). Format 5
added the window's executed order count
including partially fillable orders — the denominator of the coverage share (D24).
Format 4 added the window's recorded executed volume — the denominator of "averaged
over all traded volume". Format 3 added per-order volumes and `partially_fillable`
to the order diffs; format 2 derived its keys from the `Analysis` and
`AuctionCounterfactual` field names; format 1 (unmarked) was the hand-written
`*_wei`-suffixed shape."""

SIGN_CONVENTION_ID = "counterfactual-minus-actual"
"""Written into every `analyse --out` JSON and required by `load_report`, so a report
from before the convention flip fails loudly instead of being tabulated with every
sign silently inverted."""

SIGN_CONVENTION = (
    "every delta is counterfactual minus actual — negative Δsurplus means users would "
    "have received less without the solver, positive Δrewards means the protocol would "
    "have paid more"
)
"""Stated on every rendering rather than assumed: the same numbers under the equally
sensible with-minus-without convention would flip every sign."""

CAVEATS = (
    "No behavioural response: the remaining solvers' bids are held fixed, so nothing "
    "is claimed about how they would bid if the removed solver actually left. The "
    "competition's cap on solutions per solver is also applied before winners are "
    "picked, so a rival solution suppressed by that cap cannot step in either.",
    "Settlement risk: winning an auction does not guarantee the batch lands on-chain, "
    "so the counterfactual must assume something about settlement. The headline rule "
    "(`inherited`) keeps each auction slot's recorded outcome — a batch that actually "
    "reverted stays reverted whoever wins it; the alternative (`assume-settled`) "
    "assumes every winner lands in time. The choice is first-order: on the mainnet "
    "calibration window, 14.9% of winners never settled in time and they carried "
    "50.2% of winning score.",
    "Filter: the competition's fairness filter is re-run on per-token-pair scores, "
    "recomputed from the recorded fee policies and anchored on each solution's recorded "
    "score. Baselines are exact by construction; a pair whose own fees are unrecorded "
    "falls back to user surplus, which is a lower bound on its score, so the filter can "
    "only ever be stricter than the protocol was. The table reports how often that "
    "fallback actually decided something. On the mainnet calibration window the filter "
    "reproduces the recorded competition exactly "
    "(docs/winner-selection.md#the-filter-runs-on-score).",
    "The quote side, when run, is a second removal: the solver is taken out of the "
    "quote competition too, the pick is re-run for every order created in the window "
    "that could be linked to its competition, and the price-improvement fee of the "
    "affected orders is recharged against the replacement quote in every auction that "
    "executed them, whether or not the solver bid there. That recharge is reported as "
    "its own row and is NOT added to the fee rows, which are computed with the recorded "
    "quotes on both sides: a worse quote raises the price-improvement baseline "
    "mechanically, so folding it in would read as the protocol gaining from losing a good "
    "quoter. One rule decides what moves: everything the solvers did — bids, executions, "
    "ranking — is held as recorded; the user-price effect is the shown-price change on "
    "the quote side, never a surplus recomputed against a limit nobody signed, which "
    "would rise exactly when the user was shown a worse price. Three limits ride "
    "with it. The pick is reproduced, not recorded: the table reports how often "
    "re-running it returns the recorded winner. The order to request match is a "
    "heuristic (tokens, kind, amount, time) — placed against the gold request table and, "
    "for the competitions that table does not hold, against the raw estimates — of which "
    "about four in five are corroborated by the winning estimate's own amount, and only "
    "corroborated matches are allowed to move a fee. And holding "
    "executions and ranking fixed is checked, not assumed: the "
    "table counts recharged orders whose execution would not have cleared the limit the "
    "replacement quote implies, and auctions where shifting every score by that limit "
    "change would move the winner set.",
    "Quote rewards (CIP-72) are excluded: the counterfactual quote's eligibility is "
    "not evaluated, so no reward figure on either side includes them.",
    "Conversion is out of scope: a worse quote means some users would not have placed "
    "the order at all, and there is no counterfactual for an order nobody created. The "
    "quote figures describe the price shown on orders that *were* created.",
    "Two reward figures: uncapped is the mechanism's exact accounting but not what is "
    "paid out; capped is at payout scale but an estimate (a replacement winner "
    "inherits the reward cap of the slot it takes). The net-change row nets against "
    "the figure named in its label.",
    "Price-suspect auctions are excluded from every statistic and counted in the "
    "table: native token prices in the auction data are occasionally wrong by orders "
    "of magnitude, and one such fabricated price once supplied 82% of a solver's "
    "headline (docs/analytics-db.md#native-prices-can-be-plain-wrong).",
    "Auctions with a traded order recorded in neither order table are excluded and "
    "counted in the table: solver-provided (JIT) orders are only recorded for settled "
    "batches, so an unsettled solution's JIT orders are unrecoverable and its auction "
    "cannot be replayed faithfully. The exclusions are not random — they are "
    "JIT-heavy and reverted-winner auctions.",
    "Coverage has two denominators, and they answer different questions. \"Of N "
    "compared\" is relative to orders traded in auctions the solver bid in — it "
    "describes the solver's own turf and overstates window-level dependence for a "
    "selective bidder. The share of the window's order executions divides the same "
    "count by every user order the recorded winners executed in any clean analysed "
    "auction, partially fillable included — the service-degradation reading. Both "
    "count per auction-order execution, so a partially fillable order weighs as often "
    "as it trades; the distinct-orders row decomposes the count into actual orders, "
    "with a share of its own only for the fill-or-kill part, whose window order count "
    "exists (it is the averaged-price-change denominator) and for which distinct "
    "orders and executions coincide.",
    "The relative price change is over fill-or-kill user orders executed on both sides "
    "whose surplus moved: partially fillable orders are excluded because the two sides "
    "can execute different amounts, which makes Δsurplus a mixture of price and "
    "quantity, and orders re-executed identically carry no price information. Within "
    "an order the bps ratio is immune to a wrong native price — surplus and volume are "
    "valued through the same buy-token price, which cancels — but the volume weighting "
    "across orders is not. The companion figure averaged over all traded volume "
    "spreads the same moved-order Δsurplus across the window's whole executed "
    "fill-or-kill volume (the recorded winners' executions under the outcome rule, "
    "every analysed auction), so unmoved orders — and orders that stop trading, whose "
    "loss is the coverage figure — count as zero price change.",
    "USD figures are display-only conversions at each auction's own stablecoin-implied "
    "rate; they inherit every caveat of the native-token figure they restate.",
)
"""Rendered with every comparison — the numbers are not supposed to travel without
them. The rationale behind each lives in PLAN.md's decision table and docs/."""


@dataclass(frozen=True)
class AuctionMove:
    """One changed auction's deltas, from a report's `changed` array."""

    auction_id: int
    delta_surplus: int
    delta_surplus_only_with: int
    """The part of `delta_surplus` from orders executed only with the solver — orders
    that do not trade at all on the LOO side, so the whole of their surplus is lost
    rather than moved to a worse price."""
    delta_rewards: int
    delta_rewards_capped: Decimal | None
    delta_protocol_and_partner_fees: int = 0
    delta_partner_fees: int = 0
    delta_protocol_fees: int = 0
    """Per-auction fee deltas, so the USD conversion uses each auction's own rate rather
    than a window average — the same treatment Δsurplus gets. Computed with the recorded
    quotes on both sides; the quote's own fee effect is the recharge below."""
    quote_fee_recharge: int = 0
    quote_fee_recharge_partner: int = 0
    quote_fee_recharge_protocol: int = 0
    quote_fee_recharges: tuple[int, ...] = ()
    """The price-improvement fee the counterfactual executions pay against the replacement
    quotes minus against the recorded ones — gross, the partners' part, the DAO's — and
    the per-order values behind them, for the median and the concentration check."""


@dataclass(frozen=True)
class QuoteSide:
    """The quote competition's part of one report.

    Three figures lead, one per question the quote side exists to answer, and the fee
    consequence is deliberately not among them: it is inside `delta_protocol_fees`,
    because the counterfactual quote is what the recorded execution's
    `priceimprovement` fee was recomputed against. One fee number, not two.
    """

    ranking: str
    start: str
    end: str
    """The window the quote side covers. Shorter than the run's when the quote data
    begins later — reported rather than left to look like a thin market."""
    orders_total: int
    """Every order created in the quote window that carries a recorded quote — the base
    the match rate is over, before the price-suspect exclusion."""
    window_orders: int
    window_volume_native: int
    """Orders created in the window carrying a recorded quote **and** passing the
    price cross-check, and the **executed** sell-side value of those orders in native
    wei — the denominators of `no_quote_share` and `window_bps`. Executed rather than
    quoted: probe orders that never trade would otherwise be most of the denominator."""
    window_quoted_volume_native: int
    price_suspects: int
    """What was quoted whether it traded or not, and the orders excluded because their
    two native price feeds disagree."""
    matched: int
    matched_corroborated: int
    matched_raw: int
    """How many of those the ladder could be attached to, how many of those the winning
    estimate's own amount corroborates, and how many rest on competitions the gold tables
    do not hold (recovered from the raw layer). Everything below is over `matched`."""
    quote_wins: int
    quote_wins_corroborated: int
    no_quote: int
    no_quote_all: int
    no_quote_share: Decimal | None
    class_flips: int
    """Wins where the replacement is unverified while the removed winner was verified:
    the quote survives and may even read better in bps, while its quality falls. Its own
    count, never netted into the bps figures."""
    degraded: int
    unpriced: int
    nonpositive: int
    implausible: int
    """Wins whose replacement quote is more than 2x off the recorded one in either
    direction — a broken estimate promoted to winner, excluded from every figure."""
    window_bps: Decimal | None
    window_bps_all_tiers: Decimal | None
    conditional_bps: Decimal | None
    """The quote-side price change, spread over the window's whole quoted volume and —
    the conditional reading — over the volume of the orders whose quote moved. Both are
    labelled; neither substitutes for the other (D23's rule, applied to quotes)."""
    median_bps: Decimal | None
    largest_share: Decimal | None
    largest_bps: Decimal | None
    """The distribution behind those averages, because it is heavy-tailed and the
    averages are dominated by their tail: the median moved order, and the largest single
    order's share of the numerator with its own bps. Measured on July mainnet, one order
    was 86.6% of one solver's figure and 84.6% of another's — the same concentration
    check the solve side runs on Δsurplus, and the same reason it exists. Derived from
    the stored diffs at load time, so no re-run is needed to revise the conditioning."""
    fee_recharge: int
    fee_recharge_partner: int
    fee_recharge_protocol: int
    fee_recharge_median: Decimal | None
    fee_recharge_largest_share: Decimal | None
    """The quote's own fee effect: the price-improvement fee the counterfactual
    executions pay against the replacement quotes minus against the recorded ones —
    gross, the partners' part, the DAO's — with the median recharged order and the
    largest single order's share of the total, from the per-order values in the moves.
    Positive for a solver whose quotes were good, and **not** added to the fee deltas:
    a worse quote raises the fee baseline mechanically, which is not revenue the protocol
    would gain (D28 revised)."""
    substituted_orders: int
    substituted_auctions: int
    checked_auctions: int
    quote_only_auctions: int
    """Distinct executed orders whose price-improvement fee the counterfactual side
    charged against a replacement quote, the auctions holding them, every auction where
    any bid's order carried a replacement quote at all, and how many of those the solver
    never bid in (D33). The quote effect visible inside `delta_protocol_fees`."""
    limit_unclearable: int
    shift_changed_auctions: int
    shift_changed_auction_ids: tuple[int, ...]
    """The two checks behind holding executions and ranking fixed under a replaced
    quote (D34): recharged orders whose execution would not have cleared the limit the
    replacement implies, and auctions where shifting every score by the limit change
    would move the winner set. Both are expected near zero; either being large is a
    finding about the design, not a number to adjust by."""
    pick_rate: Decimal | None
    pick_requests: int
    """The gate: how often re-running the pick returns the estimator the competition
    recorded, over the matched requests that had a recorded winner."""


@dataclass(frozen=True)
class Report:
    """One `analyse --out` JSON, reduced to what the comparison consumes.

    Every delta is counterfactual minus actual (`SIGN_CONVENTION_ID`, checked at load):
    negative Δsurplus means users would have received less without the solver, positive
    Δrewards means the protocol would have paid more.
    """

    path: str
    solver: str
    network: str
    start: str
    end: str
    mode: str
    outcome_rule: str
    price_suspects_excluded: bool
    price_suspects: int
    missing_data: int
    """Auctions excluded before arbitration for missing order data (D17). Unlike price
    suspects these are not part of `auctions`; the window total is their sum."""
    auctions: int
    auctions_with_solver: int
    auctions_solver_won: int
    auctions_winner_set_changed: int
    auctions_filter_relaxed: int
    auctions_newly_kept_won: int
    multi_pair_solutions: int
    multi_pair_filter_proxied: int
    """The fairness filter's exactness, over the auctions actually arbitrated — the ones
    the solver bid in, not the whole window. Multi-pair solutions the filter judged, and
    how many of those rested on the surplus fallback rather than on a recorded score or a
    surplus that cleared its baseline outright."""
    auctions_filter_guess_could_matter: int
    """Of those auctions, how many where flipping the guesses would move the winners.
    The number to read: a guess on a solution that loses either way costs nothing."""
    delta_surplus: int
    delta_protocol_and_partner_fees: int
    delta_partner_fees: int
    delta_protocol_fees: int
    protocol_fees_base: int
    auctions_fees_incomplete: int
    """How the protocol fee changes without the solver, **with the recorded quotes on both
    sides** — what the removal does to fees through who executes; the quote's own fee
    effect is `quote.fee_recharge`, kept apart (D28 revised). `delta_protocol_and_partner_fees` is
    the whole fee the mechanism charges and is the headline; `delta_partner_fees` is the
    share passed on to partners, and `delta_protocol_fees` the remainder the DAO keeps.
    Partner fees are most of the *level* and almost none of the movement — see
    docs/analytics-db.md. All three are over the **union** of orders traded by either
    side, so an order that stops being executed contributes its whole fee.
    `auctions_fees_incomplete` counts auctions left out of all three because some
    executed order's policies are unrecorded (M13)."""
    delta_rewards: int | None
    delta_rewards_capped: Decimal | None
    auctions_capped_skipped: int
    delta_rewards_cow: Decimal | None
    delta_rewards_capped_cow: Decimal | None
    cow_auctions_without_rate: int
    orders_compared: int
    orders_only_with_solver: int
    orders_only_without_solver: int
    orders_only_with_fok: int
    orders_only_with_partial: int
    """Distinct orders behind `orders_only_with_solver` — which counts per-auction
    executions — split fill-or-kill / partially fillable. The fill-or-kill count over
    `window_orders` is `coverage_share_fok`, the distinct-order reading; the
    partially fillable count has no distinct-order share because a partially fillable
    order executes many times and a distinct count would understate it exactly where
    it trades most — its weight is in `coverage_share` instead. Derived from the
    changed auctions' order diffs at load time, like `delta_surplus_only_with`."""
    delta_surplus_only_with: int
    """The part of `delta_surplus` from the `orders_only_with_solver` orders — the
    coverage slice. Without the solver these orders do not trade at all, so users lose
    their surplus but are not handed a worse price; the rest of `delta_surplus` is
    price movement on orders that still trade. Derived from the changed auctions'
    order diffs at load time, so stored reports carry it without a re-run."""
    price_impact: PriceImpact
    """The price-movement slice in relative terms: bps of traded volume on the
    fill-or-kill orders that trade on both sides. Derived at load time, like
    `delta_surplus_only_with`."""
    window_volume: int
    window_orders: int
    """The window's recorded executed fill-or-kill user volume (received leg) and
    order count, over every clean analysed auction — solver present or not. The
    denominator of `overall_bps`; the order count also anchors `coverage_share_fok`."""
    window_executions: int
    """Every executed user order of the window's recorded winners, partially fillable
    included, counted once per auction it executes in — order executions. The
    denominator of `coverage_share` (D24)."""
    moves: tuple[AuctionMove, ...]
    quote: QuoteSide | None = None
    """The quote side of the same scenario, or `None` when the run had no quote pass —
    which is the permanent state of a network with no quote data (Q-D10). `None` and
    "all zeroes" are different findings and the comparison renders them differently."""

    @property
    def overall_bps(self) -> Decimal | None:
        """The moved-order Δsurplus averaged over all traded volume: what the window's
        average traded unit of value loses, with unmoved and no-longer-trading orders
        counting as zero price change. `None` when the window traded nothing."""
        if not self.window_volume:
            return None
        return (
            Decimal(self.price_impact.delta_surplus) * 10_000
            / Decimal(self.window_volume)
        )

    @property
    def coverage_share(self) -> Decimal | None:
        """Only-with executions over every user-order execution the window traded —
        the service-degradation reading of the coverage count, the full picture with
        partially fillable orders in. The "of N compared" companion is relative to
        auctions the solver bid in, so it overstates window-level dependence for a
        selective bidder; this share is over the whole window instead. Both sides
        count per auction-order execution, so an execution lost is an execution lost
        whatever the order's fillability. `None` when the window traded nothing."""
        if not self.window_executions:
            return None
        return Decimal(self.orders_only_with_solver) / Decimal(self.window_executions)

    @property
    def coverage_share_fok(self) -> Decimal | None:
        """The distinct-order companion of `coverage_share`, where "order" keeps its
        plain meaning: distinct fill-or-kill only-with orders over the window's
        fill-or-kill order count (D23's denominator — it holds no partially fillable
        orders, which is why the distinct reading exists only for fill-or-kill).
        `None` when the window traded nothing."""
        if not self.window_orders:
            return None
        return Decimal(self.orders_only_with_fok) / Decimal(self.window_orders)

    @property
    def analysed(self) -> int:
        """Auctions every statistic is over — the clean set when suspects are excluded."""
        if self.price_suspects_excluded:
            return self.auctions - self.price_suspects
        return self.auctions

    @property
    def window(self) -> tuple[str, str, str, str]:
        return (self.network, self.start, self.end, self.mode)


@dataclass(frozen=True)
class PriceImpact:
    """Relative price change on the orders that trade on both sides.

    The relative counterpart of the absolute Δsurplus: how much worse (or better) the
    execution price gets on the orders the scenario still fills. Restricted to
    fill-or-kill orders — a partially fillable order can execute different amounts on
    the two sides, and Δsurplus over one side's volume is then a mixture of price and
    quantity rather than a price change.
    """

    orders_still_traded: int
    """Contributing fill-or-kill orders executed on both sides."""
    orders_partially_fillable: int
    """Contributing both-sides orders excluded for being partially fillable."""
    orders_moved: int
    """Still-traded orders whose surplus changed — the set both bps figures are over.
    The conditioning is deliberate: an order re-executed identically carries no price
    information and would only dilute the average toward zero."""
    delta_surplus: int
    volume_base: int
    """Δsurplus and baseline received-leg volume, summed over the moved orders."""
    median_bps: Decimal | None
    """Signed median of the per-order relative changes — the typical moved order,
    beside a volume-weighted figure that whales can dominate."""

    @property
    def weighted_bps(self) -> Decimal | None:
        """Σ Δsurplus / Σ baseline volume over the moved orders, in basis points."""
        if not self.volume_base:
            return None
        return Decimal(self.delta_surplus) * 10_000 / Decimal(self.volume_base)


def _price_impact(changed: Sequence[Mapping[str, Any]]) -> PriceImpact:
    """One report's `PriceImpact`, from the changed auctions' order diffs.

    Derived at load time like `_only_with_surplus`, and complete for the same reason:
    an order still trading in an unchanged auction is executed by the same solution on
    both sides, so its price cannot have moved. The per-order ratio uses the baseline
    volume — the denominator is what actually happened, matching the sign convention.
    A moved order whose baseline volume rounds to 0 wei (dust) has no denominator and
    stays out of `orders_moved`.
    """
    still_traded = partial = moved = 0
    delta_total = volume_total = 0
    per_order: list[Decimal] = []
    for row in changed:
        for diff in row["order_diffs"]:
            if not (
                diff["contributes"] and diff["executed_base"] and diff["executed_loo"]
            ):
                continue
            if diff["partially_fillable"]:
                partial += 1
                continue
            still_traded += 1
            delta = (diff["surplus_loo"] or 0) - (diff["surplus_base"] or 0)
            volume = diff["volume_base"] or 0
            if not delta or not volume:
                continue
            moved += 1
            delta_total += delta
            volume_total += volume
            per_order.append(Decimal(delta) * 10_000 / Decimal(volume))
    return PriceImpact(
        orders_still_traded=still_traded,
        orders_partially_fillable=partial,
        orders_moved=moved,
        delta_surplus=delta_total,
        volume_base=volume_total,
        median_bps=median(per_order) if per_order else None,
    )


def _only_with_surplus(order_diffs: Sequence[Mapping[str, Any]]) -> int:
    """One changed auction's Δsurplus over its orders executed only with the solver.

    The same per-order delta the pipeline sums into `delta_surplus`, restricted to
    contributing orders with `executed_base and not executed_loo` — the predicate
    behind the `orders_only_with_solver` count. An unexecuted side has no surplus, so
    each term is minus the order's baseline surplus."""
    return sum(
        (diff["surplus_loo"] or 0) - (diff["surplus_base"] or 0)
        for diff in order_diffs
        if diff["contributes"] and diff["executed_base"] and not diff["executed_loo"]
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _quote_distribution(
    diffs: Sequence[Mapping[str, Any]],
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """`(median_bps, largest_share, largest_bps)` over the diffs the headline is built
    from — degraded, corroborated.

    The share is the largest single order's contribution over the signed total, so a row
    whose contributions offset can exceed 100%, exactly as the solve side's
    largest-auction share can. It is a prompt to open the order, not a discount factor.
    """
    rows = [
        d
        for d in diffs
        if d["leaf"] == "degraded" and d["tier"] == "corroborated"
    ]
    if not rows:
        return None, None, None
    contributions = [
        (Decimal(str(d["delta_bps"])) / 10_000 * Decimal(d["volume_native"]), d)
        for d in rows
    ]
    total = sum((c for c, _ in contributions), Decimal(0))
    largest, row = max(contributions, key=lambda item: abs(item[0]))
    middle = median(Decimal(str(d["delta_bps"])) for d in rows)
    share = abs(largest) / abs(total) if total else None
    return middle, share, Decimal(str(row["delta_bps"]))


def median_of(values: Sequence[Decimal]) -> Decimal:
    """`statistics.median` for `Decimal`s, kept exact on odd counts."""
    return Decimal(median(values))


def _quote_side(payload: Mapping[str, Any]) -> QuoteSide | None:
    """The quote block of a report, or `None` when the run had no quote pass.

    `quote_window` carries what the window could see and `quote` what one scenario did
    with it, so both are needed: the pick rate is a property of the window and belongs
    beside every solver's figures, since a poor one invalidates all of them.
    """
    quote = payload.get("quote")
    window = payload.get("quote_window")
    if quote is None or window is None:
        return None
    median, largest_share, largest_bps = _quote_distribution(quote["diffs"])
    recharges = [
        Decimal(v) for row in payload["changed"] for v in row["quote_fee_recharges"].values()
    ]
    recharge_total = sum(recharges, Decimal(0))
    recharge_median = median_of(recharges) if recharges else None
    recharge_share = (
        max((abs(v) for v in recharges), default=Decimal(0)) / abs(recharge_total)
        if recharges and recharge_total
        else None
    )
    return QuoteSide(
        ranking=quote["ranking"],
        start=window["start"],
        end=window["end"],
        orders_total=window["orders"],
        window_orders=quote["window_orders"],
        window_volume_native=quote["window_volume_native"],
        window_quoted_volume_native=window["quoted_volume_native"],
        price_suspects=window["price_suspects"],
        matched=quote["matched"],
        matched_corroborated=quote["matched_corroborated"],
        matched_raw=window["matched_raw"],
        quote_wins=quote["quote_wins"],
        quote_wins_corroborated=quote["quote_wins_corroborated"],
        no_quote=quote["no_quote"],
        no_quote_all=quote["no_quote_all"],
        no_quote_share=_decimal_or_none(quote["no_quote_share"]),
        class_flips=quote["class_flips"],
        degraded=quote["degraded"],
        unpriced=quote["unpriced"],
        nonpositive=quote["nonpositive"],
        implausible=quote["implausible"],
        window_bps=_decimal_or_none(quote["window_bps"]),
        window_bps_all_tiers=_decimal_or_none(quote["window_bps_all_tiers"]),
        conditional_bps=_decimal_or_none(quote["conditional_bps"]),
        median_bps=median,
        largest_share=largest_share,
        largest_bps=largest_bps,
        fee_recharge=payload["quote_fee_recharge"],
        fee_recharge_partner=payload["quote_partner_fee_recharge"],
        fee_recharge_protocol=payload["quote_fee_recharge_protocol"],
        fee_recharge_median=recharge_median,
        fee_recharge_largest_share=recharge_share,
        substituted_orders=payload["quote_substituted_orders"],
        substituted_auctions=payload["auctions_quote_substituted"],
        checked_auctions=payload["auctions_quote_checked"],
        quote_only_auctions=payload["auctions_quote_only"],
        limit_unclearable=payload["quote_limit_unclearable_orders"],
        shift_changed_auctions=payload["auctions_quote_shift_would_change_winners"],
        # Optional: added after format 11 was cut. A file without it loads with no ids
        # rather than failing, since nothing else about it differs.
        shift_changed_auction_ids=tuple(payload.get("quote_shift_auctions", ())),
        pick_rate=_decimal_or_none(window["pick_rate"]),
        pick_requests=window["pick_requests"],
    )


def load_report(path: str) -> Report:
    """Parse one `analyse --out` JSON, checking it is internally consistent.

    The sums are re-derived from the per-auction moves and must equal the report's own
    totals: an auction where nothing moved cancels identically, so the whole delta lives
    in `changed`. A mismatch means the file is truncated, hand-edited or from an
    incompatible version of `analyse`, and aggregating it would misstate the window.
    """
    with open(path) as handle:
        payload = json.load(handle)

    if payload.get("format") != REPORT_FORMAT:
        raise ValueError(
            f"{path}: report format is {payload.get('format')!r}, expected "
            f"{REPORT_FORMAT} — the file predates the current report shape; "
            f"re-run analyse"
        )
    convention = payload.get("sign_convention")
    if convention != SIGN_CONVENTION_ID:
        # Tabulating an old report would present every number with its sign silently
        # inverted — the one corruption a totals check cannot catch, since both sides
        # of it flip together.
        raise ValueError(
            f"{path}: sign convention is {convention!r}, expected "
            f"{SIGN_CONVENTION_ID!r} — re-run analyse"
        )

    uncapped = payload["mode"] == "score"
    capped = bool(payload["capped_estimate"])
    moves = tuple(
        AuctionMove(
            auction_id=row["auction_id"],
            delta_surplus=row["delta_surplus"],
            delta_surplus_only_with=_only_with_surplus(row["order_diffs"]),
            delta_rewards=row["delta_rewards"],
            delta_rewards_capped=(
                Decimal(row["delta_rewards_capped"])
                if row["delta_rewards_capped"] is not None
                else None
            ),
            delta_protocol_and_partner_fees=row["delta_protocol_and_partner_fees"],
            delta_partner_fees=row["delta_partner_fees"],
            delta_protocol_fees=row["delta_protocol_fees"],
            quote_fee_recharge=row["quote_fee_recharge"],
            quote_fee_recharge_partner=row["quote_fee_recharge_partner"],
            quote_fee_recharge_protocol=row["quote_fee_recharge_protocol"],
            quote_fee_recharges=tuple(row["quote_fee_recharges"].values()),
        )
        for row in payload["changed"]
    )
    cow = payload["cow"]

    # One pass over the only-with diffs serves two ends: the distinct-order split
    # behind `coverage_share`, and the instance count that must equal the report's own
    # total (checked below).
    only_with = 0
    fok_uids: set[str] = set()
    partial_uids: set[str] = set()
    for row in payload["changed"]:
        for diff in row["order_diffs"]:
            if diff["contributes"] and diff["executed_base"] and not diff["executed_loo"]:
                only_with += 1
                uids = partial_uids if diff["partially_fillable"] else fok_uids
                uids.add(diff["order_uid"])

    report = Report(
        path=path,
        solver=payload["solver"],
        network=payload["network"],
        start=payload["start"],
        end=payload["end"],
        mode=payload["mode"],
        outcome_rule=payload["outcome_rule"],
        price_suspects_excluded=bool(payload["exclude_price_suspect"]),
        price_suspects=len(payload["price_suspect_auctions"]),
        missing_data=len(payload["missing_data_auctions"]),
        auctions=payload["auctions"],
        auctions_with_solver=payload["auctions_with_solver"],
        delta_protocol_and_partner_fees=payload["delta_protocol_and_partner_fees"],
        delta_partner_fees=payload["delta_partner_fees"],
        delta_protocol_fees=payload["delta_protocol_fees"],
        protocol_fees_base=payload["protocol_fees_base"],
        auctions_fees_incomplete=payload["auctions_fees_incomplete"],
        auctions_solver_won=payload["auctions_solver_won_baseline"],
        auctions_winner_set_changed=payload["auctions_winner_set_changed"],
        auctions_filter_relaxed=payload["auctions_filter_relaxed"],
        auctions_newly_kept_won=payload["auctions_newly_kept_won"],
        multi_pair_solutions=payload["multi_pair_solutions"],
        multi_pair_filter_proxied=payload["multi_pair_filter_proxied"],
        auctions_filter_guess_could_matter=payload[
            "auctions_filter_guess_could_matter"
        ],
        delta_surplus=payload["delta_surplus"],
        delta_rewards=payload["delta_rewards"] if uncapped else None,
        delta_rewards_capped=(
            Decimal(payload["delta_rewards_capped"]) if capped else None
        ),
        auctions_capped_skipped=payload["auctions_capped_skipped"],
        delta_rewards_cow=(
            Decimal(cow["cow_wei"]) if uncapped and cow is not None else None
        ),
        delta_rewards_capped_cow=(
            Decimal(cow["cow_wei_capped"]) if capped and cow is not None else None
        ),
        cow_auctions_without_rate=(
            cow["auctions_without_rate"] if cow is not None else 0
        ),
        orders_compared=payload["orders_compared"],
        orders_only_with_solver=payload["orders_only_with_solver"],
        orders_only_without_solver=payload["orders_only_without_solver"],
        orders_only_with_fok=len(fok_uids),
        orders_only_with_partial=len(partial_uids),
        delta_surplus_only_with=sum(m.delta_surplus_only_with for m in moves),
        price_impact=_price_impact(payload["changed"]),
        window_volume=payload["window_volume_base"],
        window_orders=payload["window_orders_base"],
        window_executions=payload["window_order_executions_base"],
        moves=moves,
        quote=_quote_side(payload),
    )

    # An order executed only with the solver implies its winning solution changed, so
    # every such order lives in a `changed` auction and the count re-derived there must
    # equal the report's own total — the completeness assumption that
    # `delta_surplus_only_with` and the distinct-order split rest on.
    if only_with != report.orders_only_with_solver:
        raise ValueError(
            f"{path}: orders_only_with_solver is {report.orders_only_with_solver} but "
            f"the changed auctions' order diffs hold {only_with}; the file is "
            f"truncated, edited, or from an incompatible analyse version"
        )

    checks: list[tuple[str, int | Decimal, int | Decimal]] = [
        ("delta_surplus", report.delta_surplus, sum(m.delta_surplus for m in moves))
    ]
    if report.delta_rewards is not None:
        checks.append(
            ("delta_rewards", report.delta_rewards, sum(m.delta_rewards for m in moves))
        )
    if report.delta_rewards_capped is not None:
        checks.append(
            (
                "delta_rewards_capped",
                report.delta_rewards_capped,
                sum(
                    (
                        m.delta_rewards_capped
                        for m in moves
                        if m.delta_rewards_capped is not None
                    ),
                    Decimal(0),
                ),
            )
        )
    for name, total, from_moves in checks:
        if total != from_moves:
            raise ValueError(
                f"{path}: {name} is {total} but its changed auctions sum to "
                f"{from_moves}; the file is truncated, edited, or from an "
                f"incompatible analyse version"
            )
    return report


@dataclass(frozen=True)
class Distribution:
    """How one delta is spread over a report's changed auctions.

    Medians ride beside every sum because the sums are whale-dominated;
    `largest_share` is the whale check itself."""

    total: Decimal
    n_nonzero: int
    n_positive: int
    n_negative: int
    sum_positive: Decimal
    sum_negative: Decimal
    median_abs: Decimal | None
    largest: Decimal | None
    largest_auction_id: int | None

    @property
    def largest_share(self) -> float | None:
        """|largest| / |total| — the share of the headline one auction supplies."""
        if not self.total or self.largest is None:
            return None
        return float(abs(self.largest) / abs(self.total))


def distribution(deltas: Mapping[int, int | Decimal]) -> Distribution:
    """Distribution of `{auction_id: delta}`; zero deltas carry no information and are
    excluded, so the median is the table's "median non-zero auction"."""
    nonzero = {a: Decimal(d) for a, d in deltas.items() if d}
    total = sum(nonzero.values(), Decimal(0))
    largest_id = max(nonzero, key=lambda a: abs(nonzero[a]), default=None)
    return Distribution(
        total=total,
        n_nonzero=len(nonzero),
        n_positive=sum(1 for d in nonzero.values() if d > 0),
        n_negative=sum(1 for d in nonzero.values() if d < 0),
        sum_positive=sum((d for d in nonzero.values() if d > 0), Decimal(0)),
        sum_negative=sum((d for d in nonzero.values() if d < 0), Decimal(0)),
        median_abs=median(abs(d) for d in nonzero.values()) if nonzero else None,
        largest=nonzero[largest_id] if largest_id is not None else None,
        largest_auction_id=largest_id,
    )


@dataclass(frozen=True)
class UsdContext:
    """Per-auction USD rates for one network, with the window median as fallback."""

    rates: Mapping[int, Decimal]
    fallback: Decimal

    def rate(self, auction_id: int) -> Decimal:
        return self.rates.get(auction_id, self.fallback)


def usd_context(rates: Mapping[int, Decimal]) -> UsdContext | None:
    """`None` when no auction had a rate at all — USD is then simply not offered."""
    if not rates:
        return None
    return UsdContext(rates=dict(rates), fallback=median(rates.values()))


def usd_total(deltas: Mapping[int, int | Decimal], usd: UsdContext) -> Decimal:
    """Σ delta × the auction's own rate, in USD. Exact per-auction conversion, same
    shape as D13's per-auction COW conversion and for the same reason: one window
    rate is wrong whenever the window is long enough to be worth analysing."""
    return sum(
        (Decimal(delta) * usd.rate(auction_id) for auction_id, delta in deltas.items()),
        Decimal(0),
    ) / WEI


@dataclass(frozen=True)
class SolverWindow:
    """All outcome-rule runs of one solver over one window — one table column."""

    solver: str
    network: str
    start: str
    end: str
    mode: str
    by_rule: dict[str, Report]

    @property
    def headline(self) -> Report:
        return self.by_rule[HEADLINE_RULE]


def group_reports(reports: Sequence[Report]) -> list[SolverWindow]:
    """Group reports by (solver, window); each group must contain the headline rule.

    Refusing to tabulate a group without `inherited` is deliberate: the alternative is
    a headline number whose rule is whatever happened to be on disk.
    `assume-settled` is optional; its row reads "not run" when absent."""
    grouped: dict[tuple[str, str, str, str, str], dict[str, Report]] = {}
    for report in reports:
        key = (report.solver, *report.window)
        rules = grouped.setdefault(key, {})
        if report.outcome_rule in rules:
            raise ValueError(
                f"two reports for {report.solver} {report.start}..{report.end} with "
                f"outcome rule {report.outcome_rule!r}: {rules[report.outcome_rule].path} "
                f"and {report.path}"
            )
        rules[report.outcome_rule] = report

    windows: list[SolverWindow] = []
    for (solver, network, start, end, mode), by_rule in grouped.items():
        if HEADLINE_RULE not in by_rule:
            have = ", ".join(sorted(by_rule))
            raise ValueError(
                f"{solver} {start}..{end}: no {HEADLINE_RULE!r} report (only {have}); "
                f"the headline must be the inherited rule, not whichever file exists"
            )
        windows.append(
            SolverWindow(
                solver=solver, network=network, start=start, end=end, mode=mode,
                by_rule=by_rule,
            )
        )
    return windows


# --- formatting -------------------------------------------------------------------
# Shared with the CLI (cli.py imports these); display only, integer arithmetic.


def eth(wei: int | Decimal, places: int = 6) -> str:
    """Format native wei as a decimal string, by integer arithmetic only, rounding
    half away from zero — so a 4-place quote agrees with the same number quoted at 6.
    Decimals (the capped path) are truncated to whole wei first — display only."""
    wei = int(wei)
    sign = "-" if wei < 0 else ""
    scaled = (abs(wei) * 10**places + 10**18 // 2) // 10**18
    return f"{sign}{scaled // 10**places}.{scaled % 10**places:0{places}d}"


def pct(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "n/a"


def bps(value: Decimal, places: int = 2) -> str:
    """Signed basis points; "+" only when positive, matching `signed_eth`."""
    return ("+" if value > 0 else "") + f"{value:.{places}f} bps"


def cow_amount(wei: Decimal, places: int = 2) -> str:
    """Format COW wei (a Decimal) as whole COW."""
    quantum = Decimal(1).scaleb(-places)
    return str((wei / Decimal(10**18)).quantize(quantum))


def usd_amount(usd: Decimal) -> str:
    """$ with a sign and thousands separators; more places the smaller the amount,
    since the median auction moves fractions of a cent."""
    magnitude = abs(usd)
    places = 2 if magnitude >= Decimal("0.01") or not magnitude else 4
    return f"{'-' if usd < 0 else ''}${magnitude:,.{places}f}"


def signed_eth(wei: int | Decimal, places: int = 4) -> str:
    return ("+" if int(wei) > 0 else "") + eth(wei, places)


# --- the comparison table ----------------------------------------------------------


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    rows: tuple[tuple[str, tuple[str, ...]], ...]
    warnings: tuple[str, ...]


def _column_title(window: SolverWindow, windows_differ: bool) -> str:
    if not windows_differ:
        return window.solver
    return f"{window.solver} {window.start}..{window.end} ({window.network}, {window.mode})"


def _surplus_cell(report: Report, usd: UsdContext | None) -> str:
    cell = f"{signed_eth(report.delta_surplus)} ETH"
    if usd:
        deltas = {m.auction_id: m.delta_surplus for m in report.moves}
        cell += f" ({usd_amount(usd_total(deltas, usd))})"
    return cell


def _fee_cell(
    report: Report,
    usd: UsdContext | None,
    total: int,
    per_auction: Callable[[AuctionMove], int],
) -> str:
    """A fee delta with its USD restatement, converted **per auction** at that auction's
    own rate — the same treatment `_surplus_cell` gives Δsurplus, which matters because
    a month of a volatile chain is not well described by one rate."""
    cell = f"{signed_eth(total)} ETH"
    if usd:
        deltas = {move.auction_id: per_auction(move) for move in report.moves}
        cell += f" ({usd_amount(usd_total(deltas, usd))})"
    return cell


def comparison(
    windows: Sequence[SolverWindow],
    usd_by_network: Mapping[str, UsdContext] | None = None,
) -> Table:
    """The headline table: one column per solver-window, `inherited` leading, the
    other rules as bounds, medians and whale shares beside every sum."""
    usd_by_network = usd_by_network or {}
    windows_differ = len({w.headline.window for w in windows}) > 1
    columns = tuple(_column_title(w, windows_differ) for w in windows)
    warnings: list[str] = []
    rows: list[tuple[str, list[str]]] = []

    def row(label: str) -> list[str]:
        cells: list[str] = []
        rows.append((label, cells))
        return cells

    # Row order is the reading order: the three headline numbers first, the two
    # second-tier ones next, then the detail behind each. Sub-rows are indented and
    # follow the row they qualify.
    auctions = row("auctions analysed")
    bid = row("solver bid")
    won = row("solver won")

    # --- headline
    surplus = row("Δsurplus (inherited)")
    alternatives = {rule: row(f"  {rule}") for rule in ALTERNATIVE_RULES}
    protocol_and_partner_fees = row("Δprotocol and partner fees")
    saved_share = row("orders no longer executed")

    # --- second tier
    price_change = row("relative price change, still-traded orders")
    price_median = row("  median moved order")
    partner_fees = row("Δpartner fee")
    protocol_fees = row("Δprotocol fee")

    # --- the quote side. Three figures, one per question it answers: how much worse
    # the quotes, how often no quote at all, and what that does to the price-improvement
    # fee — reported here, never added to the fee rows above (D28 revised). The pick rate
    # rides along because it validates all three.
    quote_price = row("quote price change, all executed volume")
    quote_price_conditional = row("  on the orders whose quote moved")
    quote_missing = row("orders that would not have been quoted")
    quote_wins_row = row("  quote wins in the window")
    quote_fee = row("Δprotocol fee from the quote recharge")
    quote_substituted = row("  orders whose fee used a replacement quote")
    quote_checks = row("  limit and ranking checks")
    quote_pick = row("  quote pick reproduced the recorded winner")

    # --- detail
    surplus_median = row("  median non-zero auction")
    surplus_signs = row("  auctions moved + / −")
    surplus_whale = row("  largest single auction")
    saved_surplus = row("  Δsurplus from orders no longer executed")
    saved = row("  share of orders in auctions the solver bid in")
    saved_distinct = row("  of which distinct orders")
    only_without = row("orders executed only without")
    price_overall = row("  price change over all traded volume")
    protocol_fees_base_row = row("  DAO fee revenue with the solver")
    rewards_uncapped = row("Δrewards uncapped")
    rewards_capped = row("Δrewards capped (estimate)")
    net = row("net change (Δsurplus − capped Δrewards)")
    affected = row("auctions where anything moved")
    relaxed = row("fairness filter relaxed")
    filter_exact = row("  filter verdicts resting on surplus, in those auctions")

    for window in windows:
        report = window.headline
        usd = usd_by_network.get(report.network)

        exclusions: list[str] = []
        if report.price_suspects and report.price_suspects_excluded:
            exclusions.append(f"{report.price_suspects} price-suspect")
        if report.missing_data:
            exclusions.append(f"{report.missing_data} missing-data")
        excluded = f" ({', '.join(exclusions)} excluded)" if exclusions else ""
        auctions.append(
            f"{report.analysed:,} of {report.auctions + report.missing_data:,}{excluded}"
        )
        bid.append(
            f"{report.auctions_with_solver:,} "
            f"({pct(report.auctions_with_solver, report.analysed)})"
        )
        won.append(
            f"{report.auctions_solver_won:,} "
            f"({pct(report.auctions_solver_won, report.analysed)})"
        )

        surplus.append(_surplus_cell(report, usd))
        for rule, cells in alternatives.items():
            alternative = window.by_rule.get(rule)
            cells.append(_surplus_cell(alternative, usd) if alternative else "not run")

        moved = distribution({m.auction_id: m.delta_surplus for m in report.moves})
        if moved.median_abs is not None:
            cell = f"{eth(moved.median_abs)} ETH"
            if usd:
                cell += f" ({usd_amount(moved.median_abs * usd.fallback / WEI)})"
            surplus_median.append(f"{cell} over {moved.n_nonzero:,} auctions")
            surplus_signs.append(
                f"{moved.n_positive:,} ({signed_eth(moved.sum_positive)} ETH) / "
                f"{moved.n_negative:,} ({signed_eth(moved.sum_negative)} ETH)"
            )
        else:
            surplus_median.append("no auction moved")
            surplus_signs.append("no auction moved")
        if moved.largest is not None:
            share = (
                f" — {moved.largest_share:.0%} of the total"
                if moved.largest_share is not None
                else ""
            )
            surplus_whale.append(
                f"{signed_eth(moved.largest)} ETH{share} "
                f"(auction {moved.largest_auction_id})"
            )
        else:
            surplus_whale.append("n/a")

        # The headline fee number is the whole protocol fee, partner share included —
        # that is what the mechanism charges. The split follows one tier down, because
        # partner fees are most of the *level* but almost none of the sensitivity.
        headline = _fee_cell(
            report, usd, report.delta_protocol_and_partner_fees,
            lambda m: m.delta_protocol_and_partner_fees,
        )
        if report.auctions_fees_incomplete:
            headline += f" ({report.auctions_fees_incomplete:,} auctions unpriced)"
        protocol_and_partner_fees.append(headline)
        partner_fees.append(
            _fee_cell(
                report, usd, report.delta_partner_fees, lambda m: m.delta_partner_fees
            )
        )
        protocol_fees.append(
            _fee_cell(
                report, usd, report.delta_protocol_fees, lambda m: m.delta_protocol_fees
            )
        )
        protocol_fees_base_row.append(f"{eth(report.protocol_fees_base, 4)} ETH")

        rewards_uncapped.append(_rewards_cell(report, usd, capped=False))
        rewards_capped.append(_rewards_cell(report, usd, capped=True))
        net.append(_net_cell(report, usd))

        saved.append(
            f"{pct(report.orders_only_with_solver, report.orders_compared)} "
            f"of {report.orders_compared:,} compared"
        )
        saved_cell = f"{signed_eth(report.delta_surplus_only_with)} ETH"
        if usd:
            only_with_deltas = {
                m.auction_id: m.delta_surplus_only_with for m in report.moves
            }
            saved_cell += f" ({usd_amount(usd_total(only_with_deltas, usd))})"
        saved_surplus.append(saved_cell)
        share = report.coverage_share
        if share is None:
            saved_share.append(f"{report.orders_only_with_solver:,} — no window total")
            saved_distinct.append("n/a — the window traded nothing")
        else:
            saved_share.append(
                f"{report.orders_only_with_solver:,} "
                f"({share:.3%} of the window's {report.window_executions:,} executions)"
            )
            distinct = report.orders_only_with_fok + report.orders_only_with_partial
            share_fok = report.coverage_share_fok
            fok_part = f"{report.orders_only_with_fok:,} fill-or-kill" + (
                f" ({share_fok:.3%} of {report.window_orders:,})"
                if share_fok is not None
                else ""
            )
            saved_distinct.append(
                f"{distinct:,} — {fok_part}, "
                f"{report.orders_only_with_partial:,} partially fillable"
            )
        only_without.append(f"{report.orders_only_without_solver:,}")

        impact = report.price_impact
        weighted = impact.weighted_bps
        partial = (
            f"; {impact.orders_partially_fillable:,} partially fillable excluded"
            if impact.orders_partially_fillable
            else ""
        )
        if weighted is not None and impact.median_bps is not None:
            price_change.append(
                f"{bps(weighted)} volume-weighted over {eth(impact.volume_base, 2)} ETH "
                f"({impact.orders_moved:,} of {impact.orders_still_traded:,} orders "
                f"moved{partial})"
            )
            price_median.append(f"{bps(impact.median_bps)} over {impact.orders_moved:,} orders")
        else:
            price_change.append(f"no still-traded order moved{partial}")
            price_median.append("n/a")
        overall = report.overall_bps
        if overall is not None:
            price_overall.append(
                f"{bps(overall, 3)} of {eth(report.window_volume, 2)} ETH traded "
                f"({report.window_orders:,} orders)"
            )
        else:
            price_overall.append("n/a — the window traded nothing")

        affected.append(f"{len(report.moves):,}")
        relaxed.append(
            f"{report.auctions_filter_relaxed} "
            f"(newly kept won {report.auctions_newly_kept_won})"
        )
        filter_exact.append(
            # In surplus mode every solution is on the surplus scale by design, so the
            # count would read as a total failure of a thing that was never attempted.
            "n/a (surplus mode)"
            if report.mode != "score"
            else f"{report.multi_pair_filter_proxied:,} of "
            f"{report.multi_pair_solutions:,} "
            f"({pct(report.multi_pair_filter_proxied, report.multi_pair_solutions)}), "
            f"could have moved winners in "
            f"{report.auctions_filter_guess_could_matter:,}"
        )

        quote = report.quote
        if quote is None:
            for cells in (
                quote_price,
                quote_price_conditional,
                quote_missing,
                quote_wins_row,
                quote_fee,
                quote_substituted,
                quote_checks,
                quote_pick,
            ):
                cells.append("no quote data")
        else:
            quote_price.append(
                f"{bps(quote.window_bps, 3)} of {eth(quote.window_volume_native, 2)} "
                f"ETH executed ({quote.window_orders:,} quoted orders, "
                f"{eth(quote.window_quoted_volume_native, 2)} ETH quoted)"
                if quote.window_bps is not None
                else "n/a — the window's quoted orders traded nothing"
            )
            excluded = ", ".join(
                part
                for part in (
                    f"{quote.class_flips:,} lost verification" if quote.class_flips else "",
                    f"{quote.nonpositive:,} gas over input" if quote.nonpositive else "",
                    f"{quote.price_suspects:,} price-suspect" if quote.price_suspects else "",
                    f"{quote.implausible:,} implausible replacement" if quote.implausible else "",
                )
                if part
            )
            concentration = (
                f"; largest order {bps(quote.largest_bps)} is "
                f"{quote.largest_share:.0%} of it"
                + (" ⚠" if quote.largest_share >= Decimal("0.30") else "")
                if quote.largest_share is not None and quote.largest_bps is not None
                else ""
            )
            median = (
                f", median {bps(quote.median_bps)}"
                if quote.median_bps is not None
                else ""
            )
            quote_price_conditional.append(
                f"{bps(quote.conditional_bps)} over {quote.degraded:,} orders"
                f"{median}{concentration}"
                + (f" ({excluded} excluded)" if excluded else "")
                if quote.conditional_bps is not None
                else "no quote moved"
            )
            quote_missing.append(
                f"{quote.no_quote:,}"
                + (
                    f" ({quote.no_quote_share:.3%} of {quote.window_orders:,})"
                    if quote.no_quote_share is not None
                    else ""
                )
            )
            quote_wins_row.append(
                f"{quote.quote_wins_corroborated:,} corroborated of "
                f"{quote.quote_wins:,} in {quote.matched:,} matched orders "
                f"({quote.matched_raw:,} via the raw layer)"
            )
            recharge_detail = (
                f"; median recharged order {signed_eth(quote.fee_recharge_median, 6)} ETH, "
                f"largest {quote.fee_recharge_largest_share:.0%} of the total"
                if quote.fee_recharge_median is not None
                and quote.fee_recharge_largest_share is not None
                else ""
            )
            quote_fee.append(
                f"{signed_eth(quote.fee_recharge_protocol)} ETH to the DAO, "
                f"{signed_eth(quote.fee_recharge_partner)} ETH to partners — not added to "
                f"the fee rows above{recharge_detail}"
            )
            quote_substituted.append(
                f"{quote.substituted_orders:,} in {quote.substituted_auctions:,} "
                f"auctions, {quote.quote_only_auctions:,} of them without the "
                f"solver's bid"
            )
            sample = ", ".join(str(a) for a in quote.shift_changed_auction_ids[:3])
            quote_checks.append(
                f"{quote.limit_unclearable:,} recharged orders would not clear the "
                f"shifted limit; winners would move in {quote.shift_changed_auctions:,} "
                f"of {quote.checked_auctions:,} auctions holding a replacement quote"
                + (f" (e.g. {sample})" if sample else "")
            )
            quote_pick.append(
                f"{quote.pick_rate:.3%} of {quote.pick_requests:,} requests "
                f"({quote.ranking})"
                if quote.pick_rate is not None
                else "n/a"
            )

        warnings.extend(_report_warnings(window, usd))

    return Table(
        columns=columns,
        rows=tuple((label, tuple(cells)) for label, cells in rows),
        warnings=tuple(warnings),
    )


def _rewards_cell(report: Report, usd: UsdContext | None, *, capped: bool) -> str:
    delta = report.delta_rewards_capped if capped else report.delta_rewards
    if delta is None:
        return "not computed" + ("" if capped else " (surplus mode)")
    cow = report.delta_rewards_capped_cow if capped else report.delta_rewards_cow
    parts: list[str] = []
    if cow is not None and not report.cow_auctions_without_rate:
        parts.append(f"{cow_amount(cow)} COW")
    if usd:
        if capped:
            deltas: dict[int, int | Decimal] = {
                m.auction_id: m.delta_rewards_capped
                for m in report.moves
                if m.delta_rewards_capped is not None
            }
        else:
            deltas = {m.auction_id: m.delta_rewards for m in report.moves}
        parts.append(usd_amount(usd_total(deltas, usd)))
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"{signed_eth(delta)} ETH{detail}"


def _net_cell(report: Report, usd: UsdContext | None) -> str:
    """Net change = Δsurplus − Δrewards, capped by caveat 5: capped is the payout-scale
    figure, and netting user value against an accounting identity would be nonsense.
    Under the counterfactual-minus-actual convention, negative means the scenario
    leaves users and the protocol treasury combined worse off."""
    if report.delta_rewards_capped is None:
        return "n/a without a capped estimate"
    net = Decimal(report.delta_surplus) - report.delta_rewards_capped
    cell = f"{signed_eth(net)} ETH"
    if usd:
        surplus = {m.auction_id: m.delta_surplus for m in report.moves}
        capped = {
            m.auction_id: m.delta_rewards_capped
            for m in report.moves
            if m.delta_rewards_capped is not None
        }
        cell += f" ({usd_amount(usd_total(surplus, usd) - usd_total(capped, usd))})"
    return cell


def _report_warnings(window: SolverWindow, usd: UsdContext | None) -> list[str]:
    warnings: list[str] = []
    for report in window.by_rule.values():
        label = f"{report.solver} ({report.network}, {report.outcome_rule})"
        if not report.price_suspects_excluded and report.price_suspects:
            warnings.append(
                f"{label}: ran with --include-price-suspects, so "
                f"{report.price_suspects} auctions with fabricated prices are IN these "
                f"numbers"
            )
        if report.auctions_capped_skipped:
            warnings.append(
                f"{label}: {report.auctions_capped_skipped} auctions have no capped "
                f"estimate (cap orphans) and are missing from the capped delta"
            )
        if report.cow_auctions_without_rate:
            warnings.append(
                f"{label}: {report.cow_auctions_without_rate} auctions fall in an "
                f"accounting period with no COW rate yet; COW figures omitted"
            )
        quote = report.quote
        if quote is not None and (quote.start, quote.end) != (report.start, report.end):
            warnings.append(
                f"{label}: the quote side covers {quote.start}..{quote.end}, not the "
                f"run's {report.start}..{report.end} — quote data begins later, so "
                f"every quote figure is over that shorter window while the solve "
                f"figures are over the whole one"
            )
        if quote is not None and quote.matched < quote.orders_total:
            warnings.append(
                f"{label}: {quote.matched:,} of {quote.orders_total:,} orders created "
                f"in the quote window could be linked to a quote competition "
                f"({quote.matched / quote.orders_total:.1%}); the unlinked ones "
                f"contribute zero to every quote figure"
            )
        if usd:
            missing = sum(1 for m in report.moves if m.auction_id not in usd.rates)
            if missing:
                warnings.append(
                    f"{label}: {missing} changed auctions priced no reference "
                    f"stablecoin; converted at the window-median rate"
                )
    return warnings


def render_text(table: Table) -> str:
    label_width = max(len(label) for label, _ in table.rows)
    widths = [
        max(len(column), max(len(row[1][i]) for row in table.rows))
        for i, column in enumerate(table.columns)
    ]
    lines = [
        "".rjust(label_width)
        + "  "
        + "  ".join(c.ljust(w) for c, w in zip(table.columns, widths, strict=True))
    ]
    for label, cells in table.rows:
        lines.append(
            label.ljust(label_width)
            + "  "
            + "  ".join(cell.ljust(w) for cell, w in zip(cells, widths, strict=True))
        )
    lines.append(f"\nsigns: {SIGN_CONVENTION}")
    for warning in table.warnings:
        lines.append(f"\nWARNING: {warning}")
    return "\n".join(lines)


def render_markdown(table: Table) -> str:
    header = "| | " + " | ".join(table.columns) + " |"
    divider = "| --- |" + " --- |" * len(table.columns)
    lines = [header, divider]
    for label, cells in table.rows:
        # The two-space indent marking sub-rows is invisible in markdown; use a nested
        # label instead.
        shown = "&nbsp;&nbsp;" + label.strip() if label.startswith("  ") else label
        lines.append("| " + shown + " | " + " | ".join(cells) + " |")
    lines.append(f"\n*Signs: {SIGN_CONVENTION}.*")
    for warning in table.warnings:
        lines.append(f"\n**Warning:** {warning}")
    return "\n".join(lines)
