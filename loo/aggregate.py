"""Aggregate `analyse --out` reports into a per-solver comparison table.
USD is display only: each auction's rate is the median stablecoin price in its own price
vector (`primitives.USD_REFERENCE_TOKENS`), else the window median.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, getcontext
from statistics import median
from typing import Any, NamedTuple

from .payouts import COMPONENTS, Component, RecordedRewards
from .primitives import SOLANA, native_decimals

# Capped rewards are ~40-digit Decimals; 28 digits would round their sums and fail
# `load_report`'s totals check.
getcontext().prec = 78

WEI = Decimal(10) ** 18

HEADLINE_RULE = "inherited"
ALTERNATIVE_RULES = ("assume-settled",)

REPORT_FORMAT = 19
"""Bump whenever the `analyse --out` payload shape changes."""

SIGN_CONVENTION_ID = "counterfactual-minus-actual"

SIGN_CONVENTION = (
    "every delta is counterfactual minus actual — negative Δsurplus means users would "
    "have received less without the solver, positive Δrewards means the protocol would "
    "have paid more"
)

CAVEATS = (
    "No behavioural response: the remaining solvers' bids are held fixed, so nothing "
    "is claimed about how they would bid if the removed solver actually left. The "
    "competition's cap on solutions per solver is also applied before winners are "
    "picked, so a rival solution suppressed by that cap cannot step in either.",
    "Runner-up: the margin rows measure how far the other winners' margin over their "
    "reference score — their reward before caps — moves when the solver's bids are "
    "taken away and every other bid stays as it was: room the solver's losing bids left "
    "them. That a winner bidding against its reference could shade its bid by that much "
    "is the theory behind the row, not something the counterfactual models, so the row "
    "is neither a price change nor a bound on one. It counts only winners that win the "
    "same solutions on both sides; a margin can also fall, when a solution the solver's "
    "bids kept out (filtered, or blocked by its win) raises a reference. The part above "
    "the reward cap counts only winners whose batch settled: above its cap a winner's "
    "reward no longer depends on its bid, so that part is score it could withhold from "
    "users at no cost in reward, while below the cap a worse bid costs it reward one for "
    "one and the reward comes out of the same weekly pool as the consistency budget. "
    "Δother winners' rewards (capped) needs no such theory: it is what the mechanism "
    "would pay those winners more without the solver's bids, which the consistency "
    "budget then lacks, their caps being the same on both sides. Consistency rewards are "
    "set against the margin of the winners that settled, uncapped: the rent the "
    "solver's losing bids take from those winners, whoever pays it — the protocol as "
    "reward below the cap, users above it — so it does not depend on how rewards are "
    "paid. It takes the whole margin, as if a winner knew the second-best bid: the most "
    "the runner-up protects, not a user loss.",
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
    "re-running it returns the recorded winner. An order is linked to its competition "
    "by the id of the quote it was placed with, and only where the linked estimate "
    "reproduces the stored amount to the atom; the table says how many were. And holding "
    "executions and ranking fixed is checked, not assumed: the "
    "table counts recharged orders whose execution would not have cleared the limit the "
    "replacement quote implies, and auctions where shifting every score by that limit "
    "change would move the winner set.",
    "Quote rewards in the deltas: the counterfactual quote reward is not evaluated (the "
    "replacement quote's eligibility and share are not computed), so no Δ figure "
    "includes quote rewards. The quote rewards recorded as paid to the solver are "
    "reported, under the rewards-paid rows.",
    "Rewards paid to the solver are recorded amounts, not counterfactual deltas, over "
    "every auction in the window — price-suspect and fast-path auctions included, which "
    "Δsurplus excludes — and gross of the solver service fee. Performance rewards are the "
    "per-auction payouts. Consistency and quote rewards are weekly; each is linear in a "
    "per-auction metric (relative surplus, quote reward share), so the window's part of "
    "a week's reward is that reward times the window's share of the solver's metric over "
    "the week. A week not yet paid out has no figure and its auctions are counted as not "
    "yet accounted, never as zero. Both weekly pools are budgets tied to protocol "
    "revenue: without the solver its share would go to the remaining solvers, so these "
    "rows say what the solver received, not what the protocol would save. The "
    "consistency budget is the sum of upper cap minus capped reward and is paid out in "
    "full, so Δconsistency budget (estimate) offsets the capped Δrewards, and their sum "
    "is the change in summed upper caps.",
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
    "Fast-path auctions are excluded and counted in the table: one order is settled "
    "straight from its quote competition, so there is no solver competition to "
    "replay. Solver-provided (JIT) orders are ignored throughout, as the mechanism "
    "ignores them: they earn no score and claim no token pair.",
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


@dataclass(frozen=True)
class AuctionMove:
    auction_id: int
    delta_surplus: int
    delta_surplus_only_with: int
    """Part of `delta_surplus` from orders that trade only with the solver."""
    delta_rewards: int
    delta_rewards_capped: Decimal | None
    delta_protocol_and_partner_fees: int = 0
    delta_partner_fees: int = 0
    delta_protocol_fees: int = 0
    """Fee deltas use the recorded quotes on both sides; the quote's effect is the recharge."""
    quote_fee_recharge: int = 0
    quote_fee_recharge_partner: int = 0
    quote_fee_recharge_protocol: int = 0
    quote_fee_recharges: tuple[int, ...] = ()
    """Price-improvement fee against replacement minus recorded quotes: gross, partner,
    DAO, per order."""
    delta_reference_margin: int = 0
    """The pressed winners' margin over their reference, counterfactual − actual."""
    delta_margin_above_cap: Decimal | None = None
    """Its part above the reward cap, settled winners only; None without caps."""
    delta_pressed_rewards_capped: Decimal | None = None
    """Those winners' capped rewards, counterfactual − actual; None without caps."""
    delta_settled_margin: int | None = None
    """The margin of those that settled, uncapped; None where the two above are."""
    delta_score: int = 0
    """Δsurplus + Δprotocol and partner fees (fee leg where every fee is priced)."""
    delta_consistency_budget: Decimal | None = None
    """Σ (upper cap − capped reward), loo minus base; None where the capped delta is."""


@dataclass(frozen=True)
class QuoteSide:
    """The quote competition's part of one report."""

    ranking: str
    start: str
    end: str
    orders_total: int
    """Window orders with a recorded quote, before the price checks."""
    trivial_orders: int
    """Unwraps with no competition (`quotes.TRIVIAL_QUOTE_SOLVER`); never linked."""
    link: LinkSummary
    window_orders: int
    window_volume_native: int
    """Orders passing both price checks and their executed (not quoted) sell-side value:
    the denominators of `no_quote_share` and `window_bps`."""
    window_quoted_volume_native: int
    price_suspects: int
    price_suspects_one_sided: int
    price_suspects_two_sided: int
    price_suspect_volume_native: int
    """Orders failing either / the one-sided / the two-sided price check, and their
    executed volume at the api's price."""
    matched: int
    """Linked orders; the counts below are over these."""
    quote_wins: int
    no_quote: int
    no_quote_share: Decimal | None
    class_flips: int
    """Wins whose replacement is unverified where the winner was verified; not in bps."""
    degraded: int
    unpriced: int
    nonpositive: int
    implausible: int
    """Wins whose replacement is >2x off the recorded quote; excluded."""
    unrecorded: int
    """Wins on a stream dropped before the user's answer arrived; excluded."""
    stream_wins: int
    commit_dependent: int
    """Stream wins whose replacement depends on when the user committed."""
    window_bps: Decimal | None
    conditional_bps: Decimal | None
    """Over the window's executed volume, and over the moved orders' volume only."""
    delta_volume_native: Decimal
    """Σ Δ effective quote rate × executed volume over the moved orders: `window_bps` as
    an amount, counterfactual − actual."""
    median_bps: Decimal | None
    largest_share: Decimal | None
    largest_bps: Decimal | None
    fee_recharge_partner: int
    fee_recharge_protocol: int
    fee_recharge_median: Decimal | None
    fee_recharge_largest_share: Decimal | None
    """Never added to the fee deltas: a worse quote raises the fee baseline mechanically."""
    substituted_orders: int
    substituted_auctions: int
    checked_auctions: int
    quote_only_auctions: int
    """Recharged orders, their auctions, auctions with any replacement quote, and of
    those the ones the solver never bid in."""
    limit_unclearable: int
    shift_changed_auctions: int
    shift_changed_auction_ids: tuple[int, ...]
    """Checks that executions and ranking hold under a replaced quote: orders missing the
    replacement's limit, auctions whose winners move under the limit shift."""
    pick_rate: Decimal | None
    pick_requests: int
    pick_tied: int
    pick_tie_mismatches: int
    pick_streams: int
    pick_streams_reproduced: int
    """Pick reproduction over matched requests with a recorded winner; tie mismatches
    went to an equivalent estimate and are not failures."""

    @property
    def linkable(self) -> int:
        return self.orders_total - self.trivial_orders


@dataclass(frozen=True)
class LinkSummary:
    """The quote-id link over a quote window, as the report stores `quotes.LinkStats`."""

    orders: int
    linked: int
    requoted: int
    """Ethflow orders linked via the backend's re-quote at indexing, not their quote id."""
    streamed: int
    streamed_early: int
    """Orders from a streamed quote, and those that took an answer before its final best."""
    unlinked: tuple[tuple[str, int], ...]
    """`(reason, orders)`, largest first."""

    @property
    def rate(self) -> Decimal | None:
        return Decimal(self.linked) / self.orders if self.orders else None


@dataclass(frozen=True)
class Report:
    """One `analyse --out` JSON. Every delta is counterfactual − actual."""

    path: str
    solver: str
    network: str
    start: str
    end: str
    mode: str
    outcome_rule: str
    price_suspects_excluded: bool
    price_suspects: int
    fast_path: int
    """Not part of `auctions`, unlike price suspects."""
    auctions: int
    auctions_with_solver: int
    auctions_solver_won: int
    auctions_winner_set_changed: int
    auctions_filter_relaxed: int
    auctions_newly_kept_won: int
    multi_pair_solutions: int
    multi_pair_filter_proxied: int
    """Over auctions the solver bid in: verdicts resting on the surplus fallback."""
    auctions_filter_guess_could_matter: int
    """Auctions where flipping the surplus-fallback verdicts would move the winners."""
    delta_surplus: int
    delta_protocol_and_partner_fees: int
    delta_partner_fees: int
    delta_protocol_fees: int
    protocol_fees_base: int
    auctions_fees_incomplete: int
    """Fee deltas use the recorded quotes on both sides (quote effect: `quote.fee_recharge_*`)
    over the union of orders either side traded; protocol = whole − partner.
    `auctions_fees_incomplete`: auctions left out for unrecorded fee policies."""
    delta_rewards: int | None
    delta_rewards_capped: Decimal | None
    delta_consistency_budget: Decimal | None
    """Over the capped delta's auctions; the two sum to Δ Σ upper caps."""
    auctions_capped_skipped: int
    delta_rewards_cow: Decimal | None
    delta_rewards_capped_cow: Decimal | None
    cow_auctions_without_rate: int
    orders_compared: int
    orders_only_with_solver: int
    orders_only_without_solver: int
    orders_only_with_fok: int
    orders_only_with_partial: int
    """Distinct orders behind `orders_only_with_solver` (per-auction executions)."""
    delta_surplus_only_with: int
    """Part of `delta_surplus` from `orders_only_with_solver`; the rest is price movement."""
    price_impact: PriceImpact
    window_volume: int
    window_orders: int
    """Recorded executed fill-or-kill user volume (received leg) and order count over
    every clean analysed auction, solver present or not."""
    window_executions: int
    """Executed user orders of the recorded winners, partially fillable included, once
    per auction they execute in."""
    auctions_runner_up: int
    """Auctions the solver bid in where a losing bid of its set another winner's reference."""
    delta_reference_margin: int
    reference_margin_rises: int
    reference_margin_falls: int
    auctions_reference_margin_moved: int
    reference_margin_volume: int
    """The other winners' margin over their reference score, counterfactual − actual, in
    the mode's currency: (auction, winner) pairs that rose / fell, the auctions holding
    them, and the volume of those winners' winning solutions there."""
    delta_margin_above_cap: Decimal | None
    """Its part above the reward cap, over the winners that settled; None in surplus mode."""
    delta_pressed_rewards_capped: Decimal | None
    """Those winners' capped rewards, counterfactual − actual: what the solver's bids held
    back from them, which the consistency budget gets instead. None in surplus mode."""
    delta_settled_margin: int | None
    """The margin of the winners that settled, uncapped, counterfactual − actual: the rent
    the solver's losing bids take from the winners, whoever pays it. Over the auctions of
    the two above; None in surplus mode."""
    auctions_margin_cap_unknown: int
    delta_score: int
    """Δsurplus + Δprotocol and partner fees."""
    moves: tuple[AuctionMove, ...]
    quote: QuoteSide | None = None
    """`None` when the run had no quote pass, which is not the same as all zeroes."""
    paid: RecordedRewards | None = None
    """Rewards recorded as paid to the solver: amounts, not deltas; rule-independent."""

    @property
    def overall_bps(self) -> Decimal | None:
        """Moved-order Δsurplus over all traded volume; unmoved orders count as zero."""
        if not self.window_volume:
            return None
        return (
            Decimal(self.price_impact.delta_surplus) * 10_000
            / Decimal(self.window_volume)
        )

    @property
    def reference_margin_bps(self) -> Decimal | None:
        """Δmargin over the volume it sits on: score (or surplus) bps, not a price change."""
        if not self.reference_margin_volume:
            return None
        return (
            Decimal(self.delta_reference_margin) * 10_000
            / Decimal(self.reference_margin_volume)
        )

    @property
    def coverage_share(self) -> Decimal | None:
        if not self.window_executions:
            return None
        return Decimal(self.orders_only_with_solver) / Decimal(self.window_executions)

    @property
    def coverage_share_fok(self) -> Decimal | None:
        if not self.window_orders:
            return None
        return Decimal(self.orders_only_with_fok) / Decimal(self.window_orders)

    @property
    def analysed(self) -> int:
        if self.price_suspects_excluded:
            return self.auctions - self.price_suspects
        return self.auctions

    @property
    def window(self) -> tuple[str, str, str, str]:
        return (self.network, self.start, self.end, self.mode)


@dataclass(frozen=True)
class PriceImpact:
    """Relative price change on fill-or-kill orders trading on both sides; partially
    fillable ones can execute different amounts per side, mixing price and quantity."""

    orders_still_traded: int
    orders_partially_fillable: int
    orders_moved: int
    """Still-traded orders whose surplus changed; both bps figures are over these."""
    delta_surplus: int
    volume_base: int
    """Baseline received-leg volume of the moved orders."""
    median_bps: Decimal | None

    @property
    def weighted_bps(self) -> Decimal | None:
        if not self.volume_base:
            return None
        return Decimal(self.delta_surplus) * 10_000 / Decimal(self.volume_base)


def _price_impact(changed: Sequence[Mapping[str, Any]]) -> PriceImpact:
    """Complete from `changed` alone: an unchanged auction keeps its solution, so no
    price there moves. Per-order bps are over baseline volume; zero-volume dust is skipped."""
    still_traded = partial = moved = 0
    delta_total = volume_total = 0
    per_order: list[Decimal] = []
    for row in changed:
        for diff in row["order_diffs"]:
            if not (
                diff["executed_base"] and diff["executed_loo"]
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
    return sum(
        (diff["surplus_loo"] or 0) - (diff["surplus_base"] or 0)
        for diff in order_diffs
        if diff["executed_base"] and not diff["executed_loo"]
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _quote_distribution(
    diffs: Sequence[Mapping[str, Any]],
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """`(median_bps, largest_share, largest_bps)` over the degraded diffs. The share is
    over the signed total, so offsetting contributions can push it past 100%."""
    rows = [d for d in diffs if d["leaf"] == "degraded"]
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
    return Decimal(median(values))


def _quote_side(payload: Mapping[str, Any]) -> QuoteSide | None:
    """`None` when the run had no quote pass."""
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
        trivial_orders=window["trivial_orders"],
        link=LinkSummary(
            orders=window["link"]["orders"],
            linked=window["link"]["linked"],
            requoted=window["link"]["requoted"],
            streamed=window["link"]["streamed"],
            streamed_early=window["link"]["streamed_early"],
            unlinked=tuple(
                sorted(window["link"]["unlinked"].items(), key=lambda item: -item[1])
            ),
        ),
        window_orders=quote["window_orders"],
        window_volume_native=quote["window_volume_native"],
        window_quoted_volume_native=window["quoted_volume_native"],
        price_suspects=window["price_suspects"],
        price_suspects_one_sided=window["price_suspects_one_sided"],
        price_suspects_two_sided=window["price_suspects_two_sided"],
        price_suspect_volume_native=window["price_suspect_volume_native"],
        matched=quote["matched"],
        quote_wins=quote["quote_wins"],
        no_quote=quote["no_quote"],
        no_quote_share=_decimal_or_none(quote["no_quote_share"]),
        class_flips=quote["class_flips"],
        degraded=quote["degraded"],
        unpriced=quote["unpriced"],
        nonpositive=quote["nonpositive"],
        implausible=quote["implausible"],
        unrecorded=quote["unrecorded"],
        stream_wins=quote["stream_wins"],
        commit_dependent=quote["commit_dependent"],
        window_bps=_decimal_or_none(quote["window_bps"]),
        conditional_bps=_decimal_or_none(quote["conditional_bps"]),
        delta_volume_native=Decimal(str(quote["delta_volume_native"])),
        median_bps=median,
        largest_share=largest_share,
        largest_bps=largest_bps,
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
        shift_changed_auction_ids=tuple(payload.get("quote_shift_auctions", ())),
        pick_rate=_decimal_or_none(window["pick_rate"]),
        pick_requests=window["pick_requests"],
        pick_tied=window["pick_tied"],
        pick_tie_mismatches=window["pick_tie_mismatches"],
        pick_streams=window["pick_streams"],
        pick_streams_reproduced=window["pick_streams_reproduced"],
    )


def load_report(path: str) -> Report:
    """Parse one `analyse --out` JSON; its totals must equal the sums over `changed`."""
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
            # The window's fee totals leave out auctions with an unpriced fee, so the
            # per-auction legs that the USD cells sum must too.
            delta_protocol_and_partner_fees=(
                row["delta_protocol_and_partner_fees"] if row["fees_complete"] else 0
            ),
            delta_partner_fees=row["delta_partner_fees"] if row["fees_complete"] else 0,
            delta_protocol_fees=row["delta_protocol_fees"] if row["fees_complete"] else 0,
            quote_fee_recharge=row["quote_fee_recharge"],
            quote_fee_recharge_partner=row["quote_fee_recharge_partner"],
            quote_fee_recharge_protocol=row["quote_fee_recharge_protocol"],
            quote_fee_recharges=tuple(row["quote_fee_recharges"].values()),
            delta_reference_margin=row["delta_reference_margin"],
            delta_margin_above_cap=_decimal_or_none(row["delta_margin_above_cap"]),
            delta_pressed_rewards_capped=_decimal_or_none(
                row["delta_pressed_rewards_capped"]
            ),
            delta_settled_margin=row["delta_settled_margin"],
            delta_score=row["delta_score"],
            delta_consistency_budget=_decimal_or_none(row["delta_consistency_budget"]),
        )
        for row in payload["changed"]
    )
    cow = payload["cow"]
    paid = payload["paid"]

    only_with = 0
    fok_uids: set[str] = set()
    partial_uids: set[str] = set()
    for row in payload["changed"]:
        for diff in row["order_diffs"]:
            if diff["executed_base"] and not diff["executed_loo"]:
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
        fast_path=len(payload["fast_path_auctions"]),
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
        delta_consistency_budget=(
            Decimal(payload["delta_consistency_budget"]) if capped else None
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
        auctions_runner_up=payload["auctions_runner_up"],
        delta_reference_margin=payload["delta_reference_margin"],
        reference_margin_rises=payload["reference_margin_rises"],
        reference_margin_falls=payload["reference_margin_falls"],
        auctions_reference_margin_moved=payload["auctions_reference_margin_moved"],
        reference_margin_volume=payload["reference_margin_volume"],
        delta_margin_above_cap=_decimal_or_none(payload["delta_margin_above_cap"]),
        delta_pressed_rewards_capped=_decimal_or_none(
            payload["delta_pressed_rewards_capped"]
        ),
        delta_settled_margin=payload["delta_settled_margin"],
        auctions_margin_cap_unknown=payload["auctions_margin_cap_unknown"],
        delta_score=payload["delta_score"],
        moves=moves,
        quote=_quote_side(payload),
        paid=RecordedRewards.from_payload(paid, path) if paid is not None else None,
    )

    # The derived only-with figures assume every such order lies in a `changed` auction.
    if only_with != report.orders_only_with_solver:
        raise ValueError(
            f"{path}: orders_only_with_solver is {report.orders_only_with_solver} but "
            f"the changed auctions' order diffs hold {only_with}; the file is "
            f"truncated, edited, or from an incompatible analyse version"
        )

    checks: list[tuple[str, int | Decimal, int | Decimal]] = [
        ("delta_surplus", report.delta_surplus, sum(m.delta_surplus for m in moves)),
        ("delta_score", report.delta_score, sum(m.delta_score for m in moves)),
        (
            "delta_protocol_and_partner_fees",
            report.delta_protocol_and_partner_fees,
            sum(m.delta_protocol_and_partner_fees for m in moves),
        ),
        (
            "delta_reference_margin",
            report.delta_reference_margin,
            sum(m.delta_reference_margin for m in moves),
        ),
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
    if report.delta_pressed_rewards_capped is not None:
        checks.append(
            (
                "delta_pressed_rewards_capped",
                report.delta_pressed_rewards_capped,
                sum(
                    (
                        m.delta_pressed_rewards_capped
                        for m in moves
                        if m.delta_pressed_rewards_capped is not None
                    ),
                    Decimal(0),
                ),
            )
        )
    if report.delta_settled_margin is not None:
        checks.append(
            (
                "delta_settled_margin",
                report.delta_settled_margin,
                sum(
                    m.delta_settled_margin
                    for m in moves
                    if m.delta_settled_margin is not None
                ),
            )
        )
    if report.delta_margin_above_cap is not None:
        checks.append(
            (
                "delta_margin_above_cap",
                report.delta_margin_above_cap,
                sum(
                    (
                        m.delta_margin_above_cap
                        for m in moves
                        if m.delta_margin_above_cap is not None
                    ),
                    Decimal(0),
                ),
            )
        )
    if report.delta_consistency_budget is not None:
        checks.append(
            (
                "delta_consistency_budget",
                report.delta_consistency_budget,
                sum(
                    (
                        m.delta_consistency_budget
                        for m in moves
                        if m.delta_consistency_budget is not None
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
        if not self.total or self.largest is None:
            return None
        return float(abs(self.largest) / abs(self.total))


def distribution(deltas: Mapping[int, int | Decimal]) -> Distribution:
    """Distribution of `{auction_id: delta}`, zero deltas excluded."""
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
    """Per-auction USD rates for one network; `fallback` is the window median."""

    rates: Mapping[int, Decimal]
    """USD per whole native token."""
    fallback: Decimal
    unit: Decimal = WEI
    """Native amounts per whole native token: 1e18 wei, 1e9 lamports."""

    def rate(self, auction_id: int) -> Decimal:
        return self.rates.get(auction_id, self.fallback)


def usd_context(rates: Mapping[int, Decimal], network: str = "mainnet") -> UsdContext | None:
    """`None` when no auction had a rate."""
    if not rates:
        return None
    return UsdContext(
        rates=dict(rates),
        fallback=median(rates.values()),
        unit=Decimal(10) ** native_decimals(network),
    )


def usd_total(deltas: Mapping[int, int | Decimal], usd: UsdContext) -> Decimal:
    return sum(
        (Decimal(delta) * usd.rate(auction_id) for auction_id, delta in deltas.items()),
        Decimal(0),
    ) / usd.unit


@dataclass(frozen=True)
class SolverWindow:
    """All outcome-rule runs of one solver over one window: one table column."""

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
    """Group reports by (solver, window); each group must contain `HEADLINE_RULE`."""
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


# --- formatting (also used by cli.py) ---------------------------------------------


def eth(wei: int | Decimal, places: int = 6, decimals: int = 18) -> str:
    """Native wei (or another native token's smallest unit, `decimals` to the whole
    token) as decimal text, rounding half away from zero; Decimals truncate to wei."""
    wei = int(wei)
    sign = "-" if wei < 0 else ""
    whole = 10**decimals
    scaled = (abs(wei) * 10**places + whole // 2) // whole
    return f"{sign}{scaled // 10**places}.{scaled % 10**places:0{places}d}"


def pct(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "n/a"


def bps(value: Decimal, places: int = 2) -> str:
    return ("+" if value > 0 else "") + f"{value:.{places}f} bps"


def cow_amount(wei: Decimal, places: int = 2) -> str:
    quantum = Decimal(1).scaleb(-places)
    return str((wei / Decimal(10**18)).quantize(quantum))


def usd_amount(usd: Decimal) -> str:
    magnitude = abs(usd)
    places = 2 if magnitude >= Decimal("0.01") or not magnitude else 4
    return f"{'-' if usd < 0 else ''}${magnitude:,.{places}f}"


def signed_eth(wei: int | Decimal, places: int = 4, decimals: int = 18) -> str:
    return ("+" if int(wei) > 0 else "") + eth(wei, places, decimals)


class NativeFormat(NamedTuple):
    """How one network's native amounts read: SOL on Solana, and ETH on every EVM
    network whatever its token, as these tables have always labelled them."""

    decimals: int
    unit: str

    @classmethod
    def of(cls, network: str) -> NativeFormat:
        return cls(native_decimals(network), "SOL" if network == SOLANA else "ETH")

    def __call__(self, amount: int | Decimal, places: int = 6) -> str:
        return eth(amount, places, self.decimals)

    def signed(self, amount: int | Decimal, places: int = 4) -> str:
        return signed_eth(amount, places, self.decimals)


# --- the comparison table ----------------------------------------------------------


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    rows: tuple[tuple[str, tuple[str, ...]], ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...] = ()
    """Legend lines, rendered under the sign convention."""
    right_aligned: bool = False
    """Cells are figures: right-align them."""


def _column_title(window: SolverWindow, windows_differ: bool) -> str:
    if not windows_differ:
        return window.solver
    return f"{window.solver} {window.start}..{window.end} ({window.network}, {window.mode})"


def _surplus_cell(report: Report, usd: UsdContext | None) -> str:
    native = NativeFormat.of(report.network)
    cell = f"{native.signed(report.delta_surplus)} {native.unit}"
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
    native = NativeFormat.of(report.network)
    cell = f"{native.signed(total)} {native.unit}"
    if usd:
        deltas = {move.auction_id: per_auction(move) for move in report.moves}
        cell += f" ({usd_amount(usd_total(deltas, usd))})"
    return cell


def comparison(
    windows: Sequence[SolverWindow],
    usd_by_network: Mapping[str, UsdContext] | None = None,
) -> Table:
    """The headline table: one column per solver-window."""
    usd_by_network = usd_by_network or {}
    windows_differ = len({w.headline.window for w in windows}) > 1
    columns = tuple(_column_title(w, windows_differ) for w in windows)
    warnings: list[str] = []
    rows: list[tuple[str, list[str]]] = []

    def row(label: str) -> list[str]:
        cells: list[str] = []
        rows.append((label, cells))
        return cells

    # Row order is display order; indented labels qualify the row above.
    auctions = row("auctions analysed")
    bid = row("solver bid")
    won = row("solver won")

    # --- headline
    surplus = row("Δsurplus (inherited)")
    alternatives = {rule: row(f"  {rule}") for rule in ALTERNATIVE_RULES}
    protocol_and_partner_fees = row("Δprotocol and partner fees")
    score = row("Δscore (Δsurplus + Δprotocol and partner fees)")
    saved_share = row("orders no longer executed")

    # --- second tier
    price_change = row("relative price change, still-traded orders")
    price_median = row("  median moved order")
    partner_fees = row("Δpartner fee")
    protocol_fees = row("Δprotocol fee")

    # --- quote side; its fee recharge is never added to the fee rows above
    quote_price = row("quote price change, all executed volume")
    quote_price_conditional = row("  on the orders whose quote moved")
    quote_missing = row("orders that would not have been quoted")
    quote_wins_row = row("  quote wins in the window")
    quote_fee = row("Δprotocol fee from the quote recharge")
    quote_substituted = row("  orders whose fee used a replacement quote")
    quote_checks = row("  limit and ranking checks")
    quote_pick = row("  quote pick reproduced the recorded winner")
    quote_link = row("  order↔competition link")

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
    consistency_budget = row("  Δconsistency budget (estimate)")
    upper_caps = row("  sum = Δ Σ upper reward caps (estimate)")
    net = row("net change (Δsurplus − capped Δrewards)")
    # --- recorded amounts, not deltas: what the solver was paid in the window
    paid_total = row("rewards paid to the solver (recorded)")
    paid_parts: dict[Component, list[str]] = {
        c: row(f"  {c} rewards (recorded)") for c in COMPONENTS
    }
    paid_ratio = row("  rewards paid ÷ −Δsurplus")
    affected = row("auctions where anything moved")
    relaxed = row("fairness filter relaxed")
    filter_exact = row("  filter verdicts resting on surplus, in those auctions")

    # --- the solver as runner-up: room its losing bids left the other winners, bids fixed
    runner_up = row("runner-up to another winner")
    pressed_rewards = row("Δother winners' rewards (capped)")
    margin = row("Δother winners' margin over their reference")
    margin_settled = row("  winners that settled")
    margin_above = row("  above the reward cap, winners that settled")
    margin_ratio = row("  consistency rewards paid ÷ margin of winners that settled")
    margin_bps = row("  over their winning solutions' volume")
    margin_whale = row("  largest single auction of those")

    for window in windows:
        report = window.headline
        usd = usd_by_network.get(report.network)
        native = NativeFormat.of(report.network)

        exclusions: list[str] = []
        if report.price_suspects and report.price_suspects_excluded:
            exclusions.append(f"{report.price_suspects} price-suspect")
        if report.fast_path:
            exclusions.append(f"{report.fast_path} fast-path")
        excluded = f" ({', '.join(exclusions)} excluded)" if exclusions else ""
        window_total = report.auctions + report.fast_path
        auctions.append(f"{report.analysed:,} of {window_total:,}{excluded}")
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
            cell = f"{native(moved.median_abs)} {native.unit}"
            if usd:
                cell += f" ({usd_amount(moved.median_abs * usd.fallback / usd.unit)})"
            surplus_median.append(f"{cell} over {moved.n_nonzero:,} auctions")
            surplus_signs.append(
                f"{moved.n_positive:,} ({native.signed(moved.sum_positive)} {native.unit}) / "
                f"{moved.n_negative:,} ({native.signed(moved.sum_negative)} {native.unit})"
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
                f"{native.signed(moved.largest)} {native.unit}{share} "
                f"(auction {moved.largest_auction_id})"
            )
        else:
            surplus_whale.append("n/a")

        headline = _fee_cell(
            report, usd, report.delta_protocol_and_partner_fees,
            lambda m: m.delta_protocol_and_partner_fees,
        )
        if report.auctions_fees_incomplete:
            headline += f" ({report.auctions_fees_incomplete:,} auctions unpriced)"
        protocol_and_partner_fees.append(headline)
        score.append(_score_cell(report, usd))
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
        protocol_fees_base_row.append(f"{native(report.protocol_fees_base, 4)} {native.unit}")

        rewards_uncapped.append(_rewards_cell(report, usd, capped=False))
        rewards_capped.append(_rewards_cell(report, usd, capped=True))
        consistency_budget.append(_budget_cell(report, usd, with_capped=False))
        upper_caps.append(_budget_cell(report, usd, with_capped=True))
        net.append(_net_cell(report, usd))
        paid = report.paid
        if paid is None:
            for cells in (paid_total, *paid_parts.values(), paid_ratio):
                cells.append("not recorded")
        else:
            paid_total.append(
                _paid_cell(
                    paid.total,
                    paid.totals_by_auction(),
                    paid.unaccounted_any,
                    usd,
                    native,
                    none_accounted=min(paid.unaccounted.values()) >= paid.auctions,
                )
            )
            for component, cells in paid_parts.items():
                cells.append(
                    _paid_cell(
                        paid.amount(component),
                        paid.amounts(component),
                        paid.unaccounted[component],
                        usd,
                        native,
                        none_accounted=paid.unaccounted[component] >= paid.auctions,
                    )
                )
            paid_ratio.append(_paid_ratio_cell(report.delta_surplus, paid))

        saved.append(
            f"{pct(report.orders_only_with_solver, report.orders_compared)} "
            f"of {report.orders_compared:,} compared"
        )
        saved_cell = f"{native.signed(report.delta_surplus_only_with)} {native.unit}"
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
                f"{bps(weighted)} volume-weighted over {native(impact.volume_base, 2)} {native.unit} "
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
                f"{bps(overall, 3)} of {native(report.window_volume, 2)} {native.unit} traded "
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
            # Surplus mode judges every solution on surplus by design.
            "n/a (surplus mode)"
            if report.mode != "score"
            else f"{report.multi_pair_filter_proxied:,} of "
            f"{report.multi_pair_solutions:,} "
            f"({pct(report.multi_pair_filter_proxied, report.multi_pair_solutions)}), "
            f"could have moved winners in "
            f"{report.auctions_filter_guess_could_matter:,}"
        )

        runner_up.append(
            f"{report.auctions_runner_up:,} auctions "
            f"({pct(report.auctions_runner_up, report.auctions_with_solver)} of the "
            f"{report.auctions_with_solver:,} the solver bid in)"
        )
        margin.append(_margin_cell(report, usd))
        pressed_rewards.append(_pressed_rewards_cell(report, usd))
        margin_settled.append(_settled_margin_cell(report, usd))
        margin_above.append(_above_cap_cell(report, usd))
        margin_ratio.append(_settled_margin_ratio_cell(report))
        margin_bps.append(
            f"{bps(report.reference_margin_bps)} of "
            f"{native(report.reference_margin_volume, 2)} {native.unit} "
            f"({report.mode}, not a price change)"
            if report.reference_margin_bps is not None
            else "n/a"
        )
        spread = distribution(
            {m.auction_id: m.delta_reference_margin for m in report.moves}
        )
        if spread.largest is not None:
            share = (
                f" — {spread.largest_share:.0%} of the total"
                if spread.largest_share is not None
                else ""
            )
            margin_whale.append(
                f"{native.signed(spread.largest)} {native.unit}{share} "
                f"(auction {spread.largest_auction_id})"
            )
        else:
            margin_whale.append("n/a")

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
                quote_link,
            ):
                cells.append("no quote data")
        else:
            quote_price.append(
                f"{bps(quote.window_bps, 3)} of {native(quote.window_volume_native, 2)} "
                f"{native.unit} executed ({quote.window_orders:,} quoted orders, "
                f"{native(quote.window_quoted_volume_native, 2)} {native.unit} quoted"
                + (
                    f"; {quote.price_suspects:,} failing a price check left out — "
                    f"{quote.price_suspects_one_sided:,} one-sided, "
                    f"{quote.price_suspects_two_sided:,} two-sided — "
                    f"{native(quote.price_suspect_volume_native, 2)} {native.unit} at the api's price"
                    if quote.price_suspects
                    else ""
                )
                + ")"
                if quote.window_bps is not None
                else "n/a — the window's quoted orders traded nothing"
            )
            excluded = ", ".join(
                part
                for part in (
                    f"{quote.class_flips:,} lost verification" if quote.class_flips else "",
                    f"{quote.nonpositive:,} gas over input" if quote.nonpositive else "",
                    f"{quote.implausible:,} implausible replacement" if quote.implausible else "",
                    f"{quote.unrecorded:,} on a dropped stream" if quote.unrecorded else "",
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
                f"{quote.quote_wins:,} in {quote.matched:,} matched orders"
                + (
                    f"; {quote.stream_wins:,} on streamed quotes, "
                    f"{quote.commit_dependent:,} of them commit-dependent"
                    if quote.stream_wins
                    else ""
                )
            )
            recharge_detail = (
                f"; median recharged order {native.signed(quote.fee_recharge_median, 6)} {native.unit}, "
                f"largest {quote.fee_recharge_largest_share:.0%} of the total"
                if quote.fee_recharge_median is not None
                and quote.fee_recharge_largest_share is not None
                else ""
            )
            quote_fee.append(
                f"{native.signed(quote.fee_recharge_protocol)} {native.unit} to the DAO, "
                f"{native.signed(quote.fee_recharge_partner)} {native.unit} to partners — not added to "
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
                f"({quote.ranking}; {quote.pick_tied:,} ties decided by arrival order"
                + (
                    f", {quote.pick_tie_mismatches:,} of them to an equivalent estimate"
                    if quote.pick_tie_mismatches
                    else ""
                )
                + ")"
                + (
                    f"; stream replay {quote.pick_streams_reproduced:,} of "
                    f"{quote.pick_streams:,}"
                    if quote.pick_streams
                    else ""
                )
                if quote.pick_rate is not None
                else "n/a"
            )
            quote_link.append(_link_cell(quote))

        warnings.extend(_report_warnings(window, usd))
        warnings.extend(_paid_warnings(report, usd))

    return Table(
        columns=columns,
        rows=tuple((label, tuple(cells)) for label, cells in rows),
        warnings=tuple(warnings),
    )


def _rewards_cell(report: Report, usd: UsdContext | None, *, capped: bool) -> str:
    native = NativeFormat.of(report.network)
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
    return f"{native.signed(delta)} {native.unit}{detail}"


def _margin_cell(report: Report, usd: UsdContext | None) -> str:
    native = NativeFormat.of(report.network)
    moved = report.reference_margin_rises + report.reference_margin_falls
    if not moved:
        return "no other winner's margin moved"
    cell = f"{native.signed(report.delta_reference_margin)} {native.unit}"
    if usd:
        deltas = {m.auction_id: m.delta_reference_margin for m in report.moves}
        cell += f" ({usd_amount(usd_total(deltas, usd))})"
    return (
        f"{cell} over {moved:,} winners in "
        f"{report.auctions_reference_margin_moved:,} auctions "
        f"({report.reference_margin_rises:,} rose, {report.reference_margin_falls:,} fell)"
    )


def _score_cell(report: Report, usd: UsdContext | None) -> str:
    native = NativeFormat.of(report.network)
    cell = f"{native.signed(report.delta_score)} {native.unit}"
    if usd:
        deltas = {m.auction_id: m.delta_score for m in report.moves}
        cell += f" ({usd_amount(usd_total(deltas, usd))})"
    if report.auctions_fees_incomplete:
        cell += f"; fees unpriced in {report.auctions_fees_incomplete:,} auctions"
    return cell


def _pressed_rewards_cell(report: Report, usd: UsdContext | None) -> str:
    native = NativeFormat.of(report.network)
    delta = report.delta_pressed_rewards_capped
    if delta is None:
        return "not computed (surplus mode)"
    cell = f"{native.signed(delta)} {native.unit}"
    if usd:
        deltas: dict[int, int | Decimal] = {
            m.auction_id: m.delta_pressed_rewards_capped
            for m in report.moves
            if m.delta_pressed_rewards_capped is not None
        }
        cell += f" ({usd_amount(usd_total(deltas, usd))})"
    if report.auctions_margin_cap_unknown:
        cell += f"; {report.auctions_margin_cap_unknown:,} auctions with an unknown cap left out"
    return cell


def _above_cap_cell(report: Report, usd: UsdContext | None) -> str:
    native = NativeFormat.of(report.network)
    above = report.delta_margin_above_cap
    if above is None:
        return "not computed (surplus mode)"
    cell = f"{native.signed(above)} {native.unit}"
    if usd:
        deltas: dict[int, int | Decimal] = {
            m.auction_id: m.delta_margin_above_cap
            for m in report.moves
            if m.delta_margin_above_cap is not None
        }
        cell += f" ({usd_amount(usd_total(deltas, usd))})"
    if report.auctions_margin_cap_unknown:
        cell += f"; {report.auctions_margin_cap_unknown:,} auctions with an unknown cap left out"
    return cell


def _settled_margin_cell(report: Report, usd: UsdContext | None) -> str:
    native = NativeFormat.of(report.network)
    margin = report.delta_settled_margin
    if margin is None:
        return "not computed (surplus mode)"
    cell = f"{native.signed(margin)} {native.unit}"
    if usd:
        deltas: dict[int, int | Decimal] = {
            m.auction_id: v for m in report.moves if (v := m.delta_settled_margin) is not None
        }
        cell += f" ({usd_amount(usd_total(deltas, usd))})"
    if report.auctions_margin_cap_unknown:
        cell += f"; {report.auctions_margin_cap_unknown:,} auctions with an unknown cap left out"
    return cell


def _settled_margin_ratio_cell(report: Report) -> str:
    margin, paid = report.delta_settled_margin, report.paid
    if margin is None or paid is None:
        return "n/a"
    missing = paid.unaccounted["consistency"]
    if missing and missing >= paid.auctions:
        return "pending"
    ratio = _consistency_ratio(paid.amount("consistency"), Decimal(margin))
    return ratio + ("; consistency paid incomplete" if missing else "")


def _net_cell(report: Report, usd: UsdContext | None) -> str:
    """Δsurplus − capped Δrewards: only the capped figure is at payout scale."""
    native = NativeFormat.of(report.network)
    if report.delta_rewards_capped is None:
        return "n/a without a capped estimate"
    net = Decimal(report.delta_surplus) - report.delta_rewards_capped
    cell = f"{native.signed(net)} {native.unit}"
    if usd:
        surplus = {m.auction_id: m.delta_surplus for m in report.moves}
        capped = {
            m.auction_id: m.delta_rewards_capped
            for m in report.moves
            if m.delta_rewards_capped is not None
        }
        cell += f" ({usd_amount(usd_total(surplus, usd) - usd_total(capped, usd))})"
    return cell


def _budget_cell(report: Report, usd: UsdContext | None, *, with_capped: bool) -> str:
    """Δconsistency budget, or its sum with the capped Δrewards (= Δ Σ upper caps)."""
    native = NativeFormat.of(report.network)
    budget, capped = report.delta_consistency_budget, report.delta_rewards_capped
    if budget is None or capped is None:
        return "not computed"
    total = budget + capped if with_capped else budget
    cell = f"{native.signed(total)} {native.unit}"
    if usd:
        deltas: dict[int, int | Decimal] = {
            m.auction_id: m.delta_consistency_budget
            + (m.delta_rewards_capped if with_capped else 0)
            for m in report.moves
            if m.delta_consistency_budget is not None
            and m.delta_rewards_capped is not None
        }
        cell += f" ({usd_amount(usd_total(deltas, usd))})"
    return cell


def _paid_cell(
    amount: Decimal,
    per_auction: Mapping[int, Decimal],
    unaccounted: int,
    usd: UsdContext | None,
    native: NativeFormat,
    *,
    none_accounted: bool = False,
) -> str:
    """A recorded amount: unsigned, so it does not read as a counterfactual delta. With
    nothing accounted yet there is no amount to show, not a zero."""
    if unaccounted and none_accounted:
        return f"not yet accounted ({unaccounted:,} auctions)"
    cell = f"{native(amount, 4)} {native.unit}"
    if usd:
        cell += f" ({usd_amount(usd_total(per_auction, usd))})"
    if unaccounted:
        cell += f"; incomplete: {unaccounted:,} auctions not yet accounted"
    return cell


def _paid_ratio_cell(delta_surplus: int, paid: RecordedRewards) -> str:
    if delta_surplus >= 0:
        return "n/a — Δsurplus is not negative"
    ratio = paid.total / Decimal(-delta_surplus)
    return f"{ratio:.2f}" + ("; rewards paid incomplete" if paid.unaccounted_any else "")


def _paid_warnings(report: Report, usd: UsdContext | None) -> list[str]:
    paid = report.paid
    if paid is None:
        return []
    warnings: list[str] = []
    label = f"{report.solver} ({report.network})"
    if paid.mismatches:
        sample = "; ".join(
            f"{m.component} {m.period} {m.solver[:8]}: int table {m.int_table:.6g}, "
            f"mart {m.mart:.6g}"
            for m in paid.mismatches[:3]
        )
        warnings.append(
            f"{label}: for {len(paid.mismatches)} (week, solver) pairs the int table's "
            f"full-week sum disagrees with the weekly mart's own total ({sample}) — one "
            f"was rebuilt after the other; the recorded rewards row splits the paid "
            f"reward over the week by the int table"
        )
    if usd:
        missing = sum(1 for a in paid.per_auction if a not in usd.rates)
        if missing:
            warnings.append(
                f"{label}: {missing} auctions with recorded rewards priced no reference "
                f"stablecoin; converted at the window-median rate"
            )
    return warnings


def _link_cell(quote: QuoteSide) -> str:
    link = quote.link
    parts: list[str] = []
    if link.orders:
        rate = f" ({link.rate:.1%})" if link.rate is not None else ""
        detail = (
            f", {link.requoted:,} via the backend's re-quote" if link.requoted else ""
        ) + (
            f", {link.streamed:,} through a stream ({link.streamed_early:,} before it "
            f"finished)"
            if link.streamed
            else ""
        ) + "".join(f"; {n:,} {why}" for why, n in link.unlinked)
        parts.append(f"{link.linked:,} of {link.orders:,}{rate}{detail}")
    if quote.trivial_orders:
        parts.append(f"{quote.trivial_orders:,} trivial unwraps, no competition")
    return " | ".join(parts) if parts else "no orders"


def _shared_warnings(report: Report, usd: UsdContext | None) -> list[str]:
    """Warnings about figures both the comparison and the summary show."""
    warnings: list[str] = []
    label = f"{report.solver} ({report.network}, {report.outcome_rule})"
    if not report.price_suspects_excluded and report.price_suspects:
        warnings.append(
            f"{label}: ran with --include-price-suspects, so "
            f"{report.price_suspects} auctions with fabricated prices are IN these "
            f"numbers"
        )
    if usd:
        missing = sum(1 for m in report.moves if m.auction_id not in usd.rates)
        if missing:
            warnings.append(
                f"{label}: {missing} changed auctions priced no reference "
                f"stablecoin; converted at the window-median rate"
            )
    return warnings


def _report_warnings(window: SolverWindow, usd: UsdContext | None) -> list[str]:
    warnings: list[str] = []
    for report in window.by_rule.values():
        label = f"{report.solver} ({report.network}, {report.outcome_rule})"
        warnings.extend(_shared_warnings(report, usd))
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
        if quote is not None and quote.matched < quote.linkable:
            warnings.append(
                f"{label}: {quote.matched:,} of {quote.linkable:,} orders created "
                f"in the quote window could be linked to a quote competition "
                f"({quote.matched / quote.linkable:.1%}, trivial unwraps aside); the "
                f"unlinked ones contribute zero to every quote figure"
            )
    return warnings


# --- the summary: one row per solver-window ----------------------------------------


def summary(
    windows: Sequence[SolverWindow],
    usd_by_network: Mapping[str, UsdContext] | None = None,
) -> Table:
    """One row per solver-window, most negative Δsurplus first: the figures for judging
    solvers side by side. Amounts are in USD when every window's network has rates, else
    native."""
    usd_by_network = usd_by_network or {}
    in_usd = bool(windows) and all(w.network in usd_by_network for w in windows)
    windows_differ = len({w.headline.window for w in windows}) > 1

    def amount(
        report: Report, total: int | Decimal, per_auction: Mapping[int, int | Decimal]
    ) -> Decimal:
        """In the table's unit: dollars, or native wei."""
        if in_usd:
            return usd_total(per_auction, usd_by_network[report.network])
        return Decimal(total)

    def text(value: Decimal, *, signed: bool, decimals: int = 18) -> str:
        if not in_usd:
            return signed_eth(value, 4, decimals) if signed else eth(value, 4, decimals)
        whole = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        sign = "-" if whole < 0 else ("+" if signed and whole > 0 else "")
        return f"{sign}${abs(whole):,}"

    entries: list[tuple[Decimal, str, list[str]]] = []
    totals: dict[tuple[str, str, str, str], _SummaryTotal] = {}
    warnings: list[str] = []
    for window in windows:
        report = window.headline
        native = native_decimals(report.network)
        total = totals.setdefault(report.window, _SummaryTotal(decimals=native))
        total.rows += 1
        surplus_moves = {m.auction_id: m.delta_surplus for m in report.moves}
        score_moves = {m.auction_id: m.delta_score for m in report.moves}
        surplus = amount(report, report.delta_surplus, surplus_moves)
        score = amount(report, report.delta_score, score_moves)
        paid = report.paid
        reward: dict[Component, str] = {}
        for component in COMPONENTS:
            missing = paid.unaccounted[component] if paid is not None else 0
            if paid is None:
                reward[component] = "n/a"
                total.incomplete.add(component)
            elif missing and missing >= paid.auctions:
                reward[component] = "pending"
                total.incomplete.add(component)
            else:
                value = amount(report, paid.amount(component), paid.amounts(component))
                reward[component] = text(value, signed=False, decimals=native) + (
                    "*" if missing else ""
                )
                total.paid[component] += value
                if missing:
                    total.incomplete.add(component)

        margin = report.delta_settled_margin
        if margin is None:
            margin_cell = ratio_cell = "n/a"
            total.incomplete.add("margin")
        else:
            margin_moves: dict[int, int | Decimal] = {
                m.auction_id: v for m in report.moves
                if (v := m.delta_settled_margin) is not None
            }
            margin_amount = amount(report, Decimal(margin), margin_moves)
            margin_cell = text(margin_amount, signed=True, decimals=native) + _largest_share(
                margin_moves
            )
            total.margin += margin_amount
            total.margin_native += margin
            if paid is not None:
                total.consistency_native += paid.amount("consistency")
            if report.auctions_margin_cap_unknown:
                total.incomplete.add("margin")
                margin_cell += "*"
            # Native over native: one token, so exact and free of the USD conversion.
            ratio_cell = (
                _consistency_ratio(paid.amount("consistency"), Decimal(margin))
                + ("*" if paid.unaccounted["consistency"] else "")
                if paid is not None and reward["consistency"] != "pending"
                else reward["consistency"]
            )
        quote = report.quote
        if quote is None:
            quote_cell = "not run"
        elif in_usd:
            # One amount for the window, not per auction: the window-median rate.
            rate = usd_by_network[report.network].fallback
            quote_cell = text(
                quote.delta_volume_native * rate / usd_by_network[report.network].unit,
                signed=True,
            )
        else:
            quote_cell = text(quote.delta_volume_native, signed=True, decimals=native)
        cells = [
            pct(report.auctions_solver_won, report.analysed),
            text(surplus, signed=True, decimals=native) + _largest_share(surplus_moves),
            f"{report.orders_only_with_solver:,}",
            # winning
            text(score, signed=True, decimals=native),
            reward["performance"],
            # second-best bids
            f"{report.auctions_runner_up:,}",
            margin_cell,
            reward["consistency"],
            ratio_cell,
            # quotes
            quote_cell,
            reward["quote"],
        ]
        entries.append((surplus, _column_title(window, windows_differ), cells))
        usd = usd_by_network.get(report.network) if in_usd else None
        warnings.extend(_shared_warnings(report, usd))
        warnings.extend(_paid_warnings(report, usd))
    if usd_by_network and not in_usd:
        lacking = sorted({w.network for w in windows} - set(usd_by_network))
        warnings.append(
            f"no USD rates for {', '.join(lacking)}: every amount is in the native token"
        )
    entries.sort(key=lambda entry: entry[0])
    rows = [(label, cells) for _, label, cells in entries]
    for key, total in totals.items():
        if total.rows < 2:
            continue
        label = "total" + (f" ({key[0]} {key[1]}..{key[2]}, {key[3]})" if windows_differ else "")
        rows.append((
            label,
            total.cells(lambda v, signed, d: text(v, signed=signed, decimals=d)),
        ))

    units = sorted({NativeFormat.of(w.network).unit for w in windows}) or ["ETH"]
    unit = (
        "USD, each auction at its own rate" if in_usd
        else f"{units[0]} (the native token)" if len(units) == 1
        else f"each row's native token ({', '.join(units)})"
    )
    notes = (
        f"Sorted by Δsurplus, most negative first. Amounts in {unit}; (n%) is the "
        "largest single auction's share of the total.",
        "won: share of analysed auctions. orders lost: user orders executed only with "
        "the solver.",
        "Three groups, each a contribution with bids fixed, then the reward that pays for "
        "it. Rewards are recorded as paid in the window, every auction included; amounts, "
        "not deltas. * some auctions not yet accounted, pending: none yet.",
        "Winning: Δscore is Δsurplus + Δprotocol and partner fees, what the winning bids "
        "delivered; performance rewards pay for it.",
        "Second-best bids: runner-up counts auctions where a losing bid of the solver sets "
        "another winner's reference score. Δwinners' margin is how far the margin over "
        "their reference of the winners that keep the same solutions and settled rises "
        "without the solver, uncapped: the rent its losing bids take from those winners, "
        "whoever pays it (the protocol as reward below the cap, users above it). It does "
        "not depend on how rewards are paid. consistency ÷ margin is consistency paid "
        "over it, in the native token. * some auctions with an unknown cap left out.",
        "total: the sum of the solver rows of one network and window, for the recorded "
        "rewards and Δwinners' margin; the ratio is of the native sums. Single removals do not "
        "add up to a joint one, and the margins do not add up to the consistency pool: "
        "a margin counts only what one solver's bids hold back. * some auctions or rows "
        "missing.",
        "Quotes: Δquote is the change in the quoted amounts of executed orders without "
        "the solver in the quote competition, at the window's median rate (needs analyse "
        "--quotes). Quote rewards pay for accuracy, not for winning: the five quotes "
        "closest to each executed order's price share it, verified or not.",
    )
    return Table(
        columns=(
            "won", "Δsurplus", "orders lost", "Δscore", "performance", "runner-up",
            "Δwinners' margin", "consistency", "consistency ÷ margin",
            "Δquote", "quote",
        ),
        rows=tuple((label, tuple(cells)) for label, cells in rows),
        warnings=tuple(warnings),
        notes=notes,
        right_aligned=True,
    )


def _consistency_ratio(consistency: Decimal, margin: Decimal) -> str:
    """Consistency rewards paid over Δwinners' margin; n/a unless the margin is positive."""
    if margin <= 0:
        return "n/a"
    return f"{consistency / margin:.2f}"


@dataclass
class _SummaryTotal:
    """The summary's additive columns, summed over one network-window's solver rows."""

    rows: int = 0
    paid: dict[Component, Decimal] = field(
        default_factory=lambda: {c: Decimal(0) for c in COMPONENTS}
    )
    margin: Decimal = Decimal(0)
    margin_native: Decimal = Decimal(0)
    consistency_native: Decimal = Decimal(0)
    """The ratio's two sums in the native token, as each row's ratio; one token per
    network-window."""
    incomplete: set[str] = field(default_factory=set[str])
    """Columns where some row was n/a, pending or partly accounted."""
    decimals: int = 18
    """The network's native decimals, for amounts shown in the native token."""

    def cells(self, text: Callable[[Decimal, bool, int], str]) -> list[str]:
        def mark(column: str) -> str:
            return "*" if column in self.incomplete else ""

        def show(value: Decimal, signed: bool) -> str:
            return text(value, signed, self.decimals)

        ratio = _consistency_ratio(self.consistency_native, self.margin_native)
        return [
            "", "", "", "",
            show(self.paid["performance"], False) + mark("performance"),
            "",
            show(self.margin, True) + mark("margin"),
            show(self.paid["consistency"], False) + mark("consistency"),
            ratio + (mark("consistency") or mark("margin")),
            "",
            show(self.paid["quote"], False) + mark("quote"),
        ]


def _largest_share(deltas: Mapping[int, int | Decimal]) -> str:
    share = distribution(deltas).largest_share
    return f" ({share:.0%})" if share is not None else ""


def render_text(table: Table) -> str:
    label_width = max(len(label) for label, _ in table.rows)
    widths = [
        max(len(column), max(len(row[1][i]) for row in table.rows))
        for i, column in enumerate(table.columns)
    ]

    def line(label: str, cells: Sequence[str]) -> str:
        pad = str.rjust if table.right_aligned else str.ljust
        return (
            label.ljust(label_width)
            + "  "
            + "  ".join(pad(c, w) for c, w in zip(cells, widths, strict=True))
        )

    lines = [line("", table.columns)]
    lines.extend(line(label, cells) for label, cells in table.rows)
    lines.append(f"\nsigns: {SIGN_CONVENTION}")
    lines.extend(f"\n{note}" for note in table.notes)
    for warning in table.warnings:
        lines.append(f"\nWARNING: {warning}")
    return "\n".join(lines)


def render_markdown(table: Table) -> str:
    header = "| | " + " | ".join(table.columns) + " |"
    cell = " ---: |" if table.right_aligned else " --- |"
    divider = "| --- |" + cell * len(table.columns)
    lines = [header, divider]
    for label, cells in table.rows:
        # Markdown drops leading spaces.
        shown = "&nbsp;&nbsp;" + label.strip() if label.startswith("  ") else label
        lines.append("| " + shown + " | " + " | ".join(cells) + " |")
    lines.append(f"\n*Signs: {SIGN_CONVENTION}.*")
    lines.extend(f"\n{note}" for note in table.notes)
    for warning in table.warnings:
        lines.append(f"\n**Warning:** {warning}")
    return "\n".join(lines)
