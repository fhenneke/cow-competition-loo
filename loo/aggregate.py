"""Aggregate `analyse --out` reports into a per-solver comparison table.
USD is display only: each auction's rate is the median stablecoin price in its own price
vector (`primitives.USD_REFERENCE_TOKENS`), else the window median.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, getcontext
from statistics import median
from typing import Any

# Capped rewards are ~40-digit Decimals; 28 digits would round their sums and fail
# `load_report`'s totals check.
getcontext().prec = 78

WEI = Decimal(10) ** 18

HEADLINE_RULE = "inherited"
ALTERNATIVE_RULES = ("assume-settled",)

REPORT_FORMAT = 15
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
    "Quote rewards are excluded: the counterfactual quote's eligibility is "
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
    moves: tuple[AuctionMove, ...]
    quote: QuoteSide | None = None
    """`None` when the run had no quote pass, which is not the same as all zeroes."""

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

    # The derived only-with figures assume every such order lies in a `changed` auction.
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
    fallback: Decimal

    def rate(self, auction_id: int) -> Decimal:
        return self.rates.get(auction_id, self.fallback)


def usd_context(rates: Mapping[int, Decimal]) -> UsdContext | None:
    """`None` when no auction had a rate."""
    if not rates:
        return None
    return UsdContext(rates=dict(rates), fallback=median(rates.values()))


def usd_total(deltas: Mapping[int, int | Decimal], usd: UsdContext) -> Decimal:
    return sum(
        (Decimal(delta) * usd.rate(auction_id) for auction_id, delta in deltas.items()),
        Decimal(0),
    ) / WEI


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


def eth(wei: int | Decimal, places: int = 6) -> str:
    """Native wei as decimal text, rounding half away from zero; Decimals truncate to wei."""
    wei = int(wei)
    sign = "-" if wei < 0 else ""
    scaled = (abs(wei) * 10**places + 10**18 // 2) // 10**18
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
    cell = f"{signed_eth(total)} ETH"
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
            # Surplus mode judges every solution on surplus by design.
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
                quote_link,
            ):
                cells.append("no quote data")
        else:
            quote_price.append(
                f"{bps(quote.window_bps, 3)} of {eth(quote.window_volume_native, 2)} "
                f"ETH executed ({quote.window_orders:,} quoted orders, "
                f"{eth(quote.window_quoted_volume_native, 2)} ETH quoted"
                + (
                    f"; {quote.price_suspects:,} failing a price check left out — "
                    f"{quote.price_suspects_one_sided:,} one-sided, "
                    f"{quote.price_suspects_two_sided:,} two-sided — "
                    f"{eth(quote.price_suspect_volume_native, 2)} ETH at the api's price"
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
    """Δsurplus − capped Δrewards: only the capped figure is at payout scale."""
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
        if quote is not None and quote.matched < quote.linkable:
            warnings.append(
                f"{label}: {quote.matched:,} of {quote.linkable:,} orders created "
                f"in the quote window could be linked to a quote competition "
                f"({quote.matched / quote.linkable:.1%}, trivial unwraps aside); the "
                f"unlinked ones contribute zero to every quote figure"
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
        # Markdown drops leading spaces.
        shown = "&nbsp;&nbsp;" + label.strip() if label.startswith("  ") else label
        lines.append("| " + shown + " | " + " | ".join(cells) + " |")
    lines.append(f"\n*Signs: {SIGN_CONVENTION}.*")
    for warning in table.warnings:
        lines.append(f"\n**Warning:** {warning}")
    return "\n".join(lines)
