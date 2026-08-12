"""M4: aggregate `analyse` reports into a per-solver comparison.

Consumes the JSON written by `loo analyse --out` rather than re-running the pipeline:
a full-window run takes ~5 minutes, and the report already carries every changed
auction's deltas, so window aggregation, medians and USD conversion are pure
post-processing.

Three shapes of statement come out of one report, and the table keeps them apart:

- **Sums** answer the window question but are whale-dominated — one auction carried 82%
  of Sector's Δsurplus — so every sum is reported with the median non-zero auction and
  the largest auction's share next to it.
- **The outcome rule is part of the number.** The headline is `inherited`; `observed`
  rides along as the provable lower bound and `assume-settled` as the usual upper one.
  A figure quoted without its rule would be a choice disguised as a result (PLAN §7).
- **Two reward figures, never one.** Uncapped is the mechanism's exact accounting,
  three orders of magnitude away from payouts; capped is the payout answer but an
  estimate. Net value is always labelled with which one it nets against.

USD figures are display only. The analytics DB has no USD price table, so the rate is
implied per auction by the median stablecoin price in the auction's own price vector
(`primitives.USD_REFERENCE_TOKENS`); auctions without one use the window median.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, getcontext
from statistics import median
from typing import Mapping, Sequence

# Capped rewards arrive as ~40-digit Decimal strings and are summed over thousands of
# auctions; Python's default 28-digit context silently rounds those sums, which the
# consistency check in `load_report` then misreads as file corruption. 78 digits covers
# a full uint256 — the same setting, for the same reason, as `loo.rewards`. Set here
# too because a notebook can import this module without ever touching that one.
getcontext().prec = 78

WEI = Decimal(10) ** 18

HEADLINE_RULE = "inherited"
BOUND_RULES = ("observed", "assume-settled")

CAVEATS = (
    "No behavioural response: rivals' bids are held fixed, and "
    "max_solutions_per_solver is applied before arbitration, so no suppressed rival "
    "solution can step in.",
    "Settlement risk: the headline attaches settlement to the slot (`inherited`); "
    "`observed` is the provable lower bound. 14.9% of winners never settled and they "
    "carry 50.2% of winning score, so the rule is first-order, not a footnote.",
    "Filter proxy: the fairness filter is fed per-pair surplus, measured against the "
    "recorded filter at 7 differences in 7,745 auctions (M1, PLAN §4.1).",
    "Quote rewards are excluded: no data on counterfactual quoting.",
    "Two reward figures: uncapped is exact accounting but not a payout; capped is the "
    "payout answer but an estimate (a replacement inherits the displaced slot's cap). "
    "Net value here nets against the figure named in its row label.",
    "Price-suspect auctions are excluded from every statistic and counted in the "
    "table; they carried 82% of Sector's pre-exclusion Δsurplus (D14).",
    "USD figures are display-only conversions at each auction's own stablecoin-implied "
    "rate; they inherit every caveat of the ETH figure they restate.",
)
"""PLAN §7's caveats, in the order given there, plus the USD one M4 adds. Rendered
with every comparison — the numbers are not supposed to travel without them."""


@dataclass(frozen=True)
class AuctionMove:
    """One changed auction's deltas, from a report's `changed` array."""

    auction_id: int
    delta_surplus: int
    delta_rewards: int
    delta_rewards_capped: Decimal | None


@dataclass(frozen=True)
class Report:
    """One `analyse --out` JSON, with the wei strings turned back into numbers.

    Every delta is baseline minus counterfactual: positive Δsurplus means users were
    better off with the solver, positive Δrewards means the protocol paid more with it.
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
    auctions: int
    auctions_with_solver: int
    auctions_solver_won: int
    auctions_winner_set_changed: int
    auctions_filter_relaxed: int
    auctions_newly_kept_won: int
    delta_surplus: int
    delta_rewards: int | None
    delta_rewards_capped: Decimal | None
    auctions_capped_skipped: int
    delta_rewards_cow: Decimal | None
    delta_rewards_capped_cow: Decimal | None
    cow_auctions_without_rate: int
    orders_compared: int
    orders_only_with_solver: int
    orders_only_without_solver: int
    moves: tuple[AuctionMove, ...]

    @property
    def analysed(self) -> int:
        """Auctions every statistic is over — the clean set when suspects are excluded."""
        if self.price_suspects_excluded:
            return self.auctions - self.price_suspects
        return self.auctions

    @property
    def window(self) -> tuple[str, str, str, str]:
        return (self.network, self.start, self.end, self.mode)


def load_report(path: str) -> Report:
    """Parse one `analyse --out` JSON, checking it is internally consistent.

    The sums are re-derived from the per-auction moves and must equal the report's own
    totals: an auction where nothing moved cancels identically, so the whole delta lives
    in `changed`. A mismatch means the file is truncated, hand-edited or from an
    incompatible version of `analyse`, and aggregating it would misstate the window.
    """
    with open(path) as handle:
        payload = json.load(handle)

    uncapped = bool(payload["rewards_uncapped"])
    capped = bool(payload["capped_estimate"])
    moves = tuple(
        AuctionMove(
            auction_id=row["auction_id"],
            delta_surplus=int(row["delta_surplus_wei"]),
            delta_rewards=int(row["delta_rewards_wei"]),
            delta_rewards_capped=(
                Decimal(row["delta_rewards_capped_wei"])
                if row["delta_rewards_capped_wei"] is not None
                else None
            ),
        )
        for row in payload["changed"]
    )

    report = Report(
        path=path,
        solver=payload["solver"],
        network=payload["network"],
        start=payload["start"],
        end=payload["end"],
        mode=payload["mode"],
        outcome_rule=payload["settlement"],
        price_suspects_excluded=bool(payload["price_suspects_excluded"]),
        price_suspects=len(payload["price_suspect_auctions"]),
        auctions=payload["auctions"],
        auctions_with_solver=payload["auctions_with_solver"],
        auctions_solver_won=payload["auctions_solver_won_baseline"],
        auctions_winner_set_changed=payload["auctions_winner_set_changed"],
        auctions_filter_relaxed=payload["auctions_filter_relaxed"],
        auctions_newly_kept_won=payload["auctions_newly_kept_won"],
        delta_surplus=int(payload["delta_surplus_wei"]),
        delta_rewards=int(payload["delta_rewards_wei"]) if uncapped else None,
        delta_rewards_capped=(
            Decimal(payload["delta_rewards_capped_wei"]) if capped else None
        ),
        auctions_capped_skipped=payload["auctions_capped_skipped"],
        delta_rewards_cow=(
            Decimal(payload["delta_rewards_cow_wei"])
            if uncapped and payload["delta_rewards_cow_wei"] is not None
            else None
        ),
        delta_rewards_capped_cow=(
            Decimal(payload["delta_rewards_capped_cow_wei"])
            if capped and payload["delta_rewards_capped_cow_wei"] is not None
            else None
        ),
        cow_auctions_without_rate=payload["cow_auctions_without_rate"] or 0,
        orders_compared=payload["orders_compared"],
        orders_only_with_solver=payload["orders_only_with_solver"],
        orders_only_without_solver=payload["orders_only_without_solver"],
        moves=moves,
    )

    checks = [("delta_surplus", report.delta_surplus, sum(m.delta_surplus for m in moves))]
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

    PLAN §7 requires medians alongside sums because the sums are whale-dominated;
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
    excluded, so the median is PLAN §5.1's "median non-zero auction"."""
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
    a headline number whose rule is whatever happened to be on disk (PLAN §7)."""
    grouped: dict[tuple, dict[str, Report]] = {}
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

    windows = []
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


def comparison(
    windows: Sequence[SolverWindow],
    usd_by_network: Mapping[str, UsdContext] | None = None,
) -> Table:
    """The PLAN §7 headline table: one column per solver-window, `inherited` leading,
    the other rules as bounds, medians and whale shares beside every sum."""
    usd_by_network = usd_by_network or {}
    windows_differ = len({w.headline.window for w in windows}) > 1
    columns = tuple(_column_title(w, windows_differ) for w in windows)
    warnings: list[str] = []
    rows: list[tuple[str, list[str]]] = []

    def row(label: str) -> list[str]:
        cells: list[str] = []
        rows.append((label, cells))
        return cells

    auctions = row("auctions analysed")
    bid = row("solver bid")
    won = row("solver won")
    surplus = row("Δsurplus (inherited)")
    bounds = {rule: row(f"  {rule}") for rule in BOUND_RULES}
    surplus_median = row("  median non-zero auction")
    surplus_whale = row("  largest single auction")
    rewards_uncapped = row("Δrewards uncapped")
    rewards_capped = row("Δrewards capped (estimate)")
    net = row("net value (Δsurplus − capped Δrewards)")
    saved = row("orders saved")
    only_without = row("orders executed only without")
    affected = row("auctions where anything moved")
    relaxed = row("fairness filter relaxed")

    for window in windows:
        report = window.headline
        usd = usd_by_network.get(report.network)

        excluded = (
            f" ({report.price_suspects} price-suspect excluded)"
            if report.price_suspects and report.price_suspects_excluded
            else ""
        )
        auctions.append(f"{report.analysed:,} of {report.auctions:,}{excluded}")
        bid.append(
            f"{report.auctions_with_solver:,} "
            f"({pct(report.auctions_with_solver, report.analysed)})"
        )
        won.append(
            f"{report.auctions_solver_won:,} "
            f"({pct(report.auctions_solver_won, report.analysed)})"
        )

        surplus.append(_surplus_cell(report, usd))
        for rule, cells in bounds.items():
            bound = window.by_rule.get(rule)
            cells.append(_surplus_cell(bound, usd) if bound else "not run")

        moved = distribution({m.auction_id: m.delta_surplus for m in report.moves})
        if moved.median_abs is not None:
            cell = f"{eth(moved.median_abs)} ETH"
            if usd:
                cell += f" ({usd_amount(moved.median_abs * usd.fallback / WEI)})"
            surplus_median.append(f"{cell} over {moved.n_nonzero:,} auctions")
        else:
            surplus_median.append("no auction moved")
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

        rewards_uncapped.append(_rewards_cell(report, usd, capped=False))
        rewards_capped.append(_rewards_cell(report, usd, capped=True))
        net.append(_net_cell(report, usd))

        saved.append(
            f"{report.orders_only_with_solver:,} "
            f"({pct(report.orders_only_with_solver, report.orders_compared)} "
            f"of {report.orders_compared:,} compared)"
        )
        only_without.append(f"{report.orders_only_without_solver:,}")
        affected.append(f"{len(report.moves):,}")
        relaxed.append(
            f"{report.auctions_filter_relaxed} "
            f"(newly kept won {report.auctions_newly_kept_won})"
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
    parts = []
    if cow is not None and not report.cow_auctions_without_rate:
        parts.append(f"{cow_amount(cow)} COW")
    if usd:
        deltas = {
            m.auction_id: (m.delta_rewards_capped if capped else m.delta_rewards)
            for m in report.moves
            if not capped or m.delta_rewards_capped is not None
        }
        parts.append(usd_amount(usd_total(deltas, usd)))
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"{signed_eth(delta)} ETH{detail}"


def _net_cell(report: Report, usd: UsdContext | None) -> str:
    """Net value = Δsurplus − Δrewards, capped by caveat 5: capped is the payout-scale
    figure, and netting user value against an accounting identity would be nonsense."""
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
    warnings = []
    for report in window.by_rule.values():
        label = f"{report.solver} ({report.outcome_rule})"
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
        + "  ".join(c.ljust(w) for c, w in zip(table.columns, widths))
    ]
    for label, cells in table.rows:
        lines.append(
            label.ljust(label_width)
            + "  "
            + "  ".join(cell.ljust(w) for cell, w in zip(cells, widths))
        )
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
    for warning in table.warnings:
        lines.append(f"\n**Warning:** {warning}")
    return "\n".join(lines)
