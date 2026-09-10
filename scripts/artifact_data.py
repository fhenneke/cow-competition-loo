"""Emit the `DATA` object an artifact page renders, from a run's report files.

Every figure on a published page has to trace to a report file for the run being
published, so the page's data comes from here rather than from a hand-transcribed table.
Reads the same reports `loo compare` does, fetches the same per-auction USD rates, and
prints one JSON object.

    uv run python scripts/artifact_data.py out/jul2026/*-inherited.json \
        --alt out/jul2026/*-assume-settled.json > artifacts/data/jul2026-mainnet.json

`--alt` reports are the `assume-settled` scenario: not a column, but the settlement
caveat needs them to say how far the figures move.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from loo import aggregate, db, extract
from loo.primitives import ONE_ETH

NATIVE_SYMBOL = {
    "mainnet": "ETH",
    "bnb": "BNB",
    "xdai": "xDAI",
    "base": "ETH",
    "arbitrum-one": "ETH",
    "avalanche": "AVAX",
    "polygon": "POL",
}


def usd_contexts(
    reports: Sequence[aggregate.Report], *, allow_missing: bool = False
) -> dict[str, aggregate.UsdContext]:
    """Per-network USD rates for every auction the reports moved.

    A network without rates is an error unless `allow_missing` is set: the page renders
    USD in its focus columns, and a silent fall-through would produce a plausible-looking
    page of $0 rows rather than a build failure.
    """
    by_network: dict[str, set[int]] = {}
    for report in reports:
        by_network.setdefault(report.network, set()).update(
            move.auction_id for move in report.moves
        )
    contexts: dict[str, aggregate.UsdContext] = {}
    for network, auction_ids in sorted(by_network.items()):
        conn = db.connect(network)
        try:
            rates = extract.load_usd_rates(conn, sorted(auction_ids), network)
        except KeyError as error:
            if not allow_missing:
                raise SystemExit(
                    f"no USD rates for {network}: {error}; pass --allow-missing-usd to "
                    f"build the page with $0 in that network's USD figures"
                ) from error
            print(f"note: no USD rates for {network}: {error}", file=sys.stderr)
            continue
        finally:
            conn.close()
        context = aggregate.usd_context(rates)
        if context is not None:
            contexts[network] = context
            print(
                f"USD rates for {len(rates)} {network} auctions, window median "
                f"{aggregate.usd_amount(context.fallback)}/native",
                file=sys.stderr,
            )
    return contexts


def _usd(value: int | Decimal, context: aggregate.UsdContext | None) -> float:
    """A window-level native amount in USD, at the window-median rate.

    Only for figures that have no per-auction breakdown. Anything summed over auctions
    goes through `_usd_per_auction` instead, which converts at each auction's own rate
    exactly as `compare` does — a window straddling a price move otherwise reports a
    different total than the table it is supposed to reproduce.
    """
    if context is None:
        return 0.0
    return float(Decimal(value) * context.fallback / ONE_ETH)


def _usd_per_auction(
    deltas: Mapping[int, int | Decimal], context: aggregate.UsdContext | None
) -> float:
    if context is None:
        return 0.0
    return float(aggregate.usd_total(deltas, context))


def _native(value: int | Decimal) -> float:
    return float(Decimal(value) / ONE_ETH)


def row(
    report: aggregate.Report, context: aggregate.UsdContext | None
) -> dict[str, Any]:
    """One solver-chain row, in the shape the page's `aggregate()` consumes.

    USD figures are converted per auction where the delta is per auction (surplus,
    fees), and at the window-median rate for the window-level volumes the price columns
    divide by — the same split `aggregate.usd_total` makes.
    """
    impact = report.price_impact
    moved = {m.auction_id: m.delta_surplus for m in report.moves}
    largest_move = max(report.moves, key=lambda m: abs(m.delta_surplus), default=None)
    largest = largest_move.delta_surplus if largest_move else 0
    # The largest auction's USD at its own auction's rate, so the concentration share
    # is USD over USD on one basis rather than a window-median numerator over a
    # per-auction denominator.
    largest_usd = (
        _usd_per_auction({largest_move.auction_id: largest}, context)
        if largest_move
        else 0.0
    )
    # The same concentration check on the gross fee delta: a single recharged order with
    # a price-improvement-kind partner policy can be most of a quoting solver's fee row.
    largest_fee_move = max(
        report.moves,
        key=lambda m: abs(m.delta_protocol_and_partner_fees),
        default=None,
    )
    largest_fee_usd = (
        _usd_per_auction(
            {largest_fee_move.auction_id: largest_fee_move.delta_protocol_and_partner_fees},
            context,
        )
        if largest_fee_move
        else 0.0
    )
    total_fee_usd = _usd_per_auction(
        {m.auction_id: m.delta_protocol_and_partner_fees for m in report.moves}, context
    )
    quote = report.quote
    return {
        "chain": report.network,
        "sym": NATIVE_SYMBOL.get(report.network, "native"),
        "rate": float(context.fallback / ONE_ETH) if context else 0.0,
        # --- the focus four
        "usd_s": _usd_per_auction(moved, context),
        "native_s": _native(report.delta_surplus),
        "pi_delta": impact.delta_surplus,
        "pi_volume": impact.volume_base,
        "window_volume": report.window_volume,
        "median": float(impact.median_bps) if impact.median_bps is not None else None,
        "moved": impact.orders_moved,
        "moved_auctions": len(report.moves),
        "usd_f": _usd_per_auction(
            {m.auction_id: m.delta_protocol_and_partner_fees for m in report.moves},
            context,
        ),
        "native_f": _native(report.delta_protocol_and_partner_fees),
        "usd_pr": _usd_per_auction(
            {m.auction_id: m.delta_protocol_fees for m in report.moves}, context
        ),
        "usd_pa": _usd_per_auction(
            {m.auction_id: m.delta_partner_fees for m in report.moves}, context
        ),
        "unpriced": report.auctions_fees_incomplete,
        "orders": report.orders_only_with_solver,
        "executions": report.window_executions,
        "usd_only": _usd_per_auction(
            {m.auction_id: m.delta_surplus_only_with for m in report.moves}, context
        ),
        # The page's `cells()` reads these derived fields directly on a chain row — the
        # single-chain path renders the chain row flat rather than through `aggregate()`,
        # so they have to be here or the Δprice and coverage columns come out "n/a".
        # Native ratios, computed from native sums: both legs are the same token, so the
        # ratio is exact and needs no USD conversion.
        "bps": (impact.delta_surplus * 10_000 / report.window_volume)
        if report.window_volume
        else None,
        "still": (impact.delta_surplus * 10_000 / impact.volume_base)
        if impact.volume_base
        else None,
        "cov": (report.orders_only_with_solver / report.window_executions)
        if report.window_executions
        else None,
        "share": (abs(largest_usd) / abs(_usd_per_auction(moved, context)))
        if _usd_per_auction(moved, context)
        else None,
        # --- muted
        "won": report.auctions_solver_won,
        "bid": report.auctions_with_solver,
        "analysed": report.analysed,
        "largest_usd": largest_usd,
        "largest_fee_usd": largest_fee_usd,
        "fee_share": abs(largest_fee_usd) / abs(total_fee_usd) if total_fee_usd else None,
        # --- the quote side
        "quote": None
        if quote is None
        else {
            "ranking": quote.ranking,
            "start": quote.start,
            "end": quote.end,
            "window_orders": quote.window_orders,
            "window_volume": _native(quote.window_volume_native),
            "matched": quote.matched,
            "matched_corroborated": quote.matched_corroborated,
            "matched_raw": quote.matched_raw,
            "orders_total": quote.orders_total,
            "wins": quote.quote_wins,
            "wins_corroborated": quote.quote_wins_corroborated,
            "no_quote": quote.no_quote,
            "no_quote_all": quote.no_quote_all,
            "no_quote_share": float(quote.no_quote_share)
            if quote.no_quote_share is not None
            else None,
            "class_flips": quote.class_flips,
            "degraded": quote.degraded,
            "window_bps": float(quote.window_bps)
            if quote.window_bps is not None
            else None,
            "window_bps_all": float(quote.window_bps_all_tiers)
            if quote.window_bps_all_tiers is not None
            else None,
            "conditional_bps": float(quote.conditional_bps)
            if quote.conditional_bps is not None
            else None,
            "median_bps": float(quote.median_bps)
            if quote.median_bps is not None
            else None,
            "largest_share": float(quote.largest_share)
            if quote.largest_share is not None
            else None,
            "largest_bps": float(quote.largest_bps)
            if quote.largest_bps is not None
            else None,
            "nonpositive": quote.nonpositive,
            "implausible": quote.implausible,
            "price_suspects": quote.price_suspects,
            "quoted_volume": _native(quote.window_quoted_volume_native),
            "substituted": quote.substituted_orders,
            "fee_recharge_usd": _usd_per_auction(
                {m.auction_id: m.quote_fee_recharge for m in report.moves}, context
            ),
            "fee_recharge_protocol_usd": _usd_per_auction(
                {m.auction_id: m.quote_fee_recharge_protocol for m in report.moves},
                context,
            ),
            "fee_recharge_partner_usd": _usd_per_auction(
                {m.auction_id: m.quote_fee_recharge_partner for m in report.moves}, context
            ),
            "fee_recharge_native": _native(quote.fee_recharge_protocol),
            "fee_recharge_median_usd": (
                _usd(quote.fee_recharge_median, context)
                if quote.fee_recharge_median is not None
                else None
            ),
            "fee_recharge_largest_share": (
                float(quote.fee_recharge_largest_share)
                if quote.fee_recharge_largest_share is not None
                else None
            ),
            "substituted_auctions": quote.substituted_auctions,
            "checked_auctions": quote.checked_auctions,
            "quote_only_auctions": quote.quote_only_auctions,
            "limit_unclearable": quote.limit_unclearable,
            "shift_changed_auctions": quote.shift_changed_auctions,
            "pick_rate": float(quote.pick_rate)
            if quote.pick_rate is not None
            else None,
            "pick_requests": quote.pick_requests,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", help="inherited-rule report JSONs")
    parser.add_argument(
        "--alt", nargs="*", default=[], help="assume-settled report JSONs"
    )
    parser.add_argument(
        "--allow-missing-usd",
        action="store_true",
        help="build even if a network has no USD rates (its USD figures read $0)",
    )
    args = parser.parse_args(argv)

    reports = [aggregate.load_report(path) for path in args.reports]
    alternatives = [aggregate.load_report(path) for path in args.alt]
    contexts = usd_contexts(
        [*reports, *alternatives], allow_missing=args.allow_missing_usd
    )

    solvers: dict[str, dict[str, Any]] = {}
    for report in reports:
        entry = solvers.setdefault(report.solver, {"name": report.solver, "chains": {}})
        entry["chains"][report.network] = row(report, contexts.get(report.network))
    for report in alternatives:
        entry = solvers.get(report.solver)
        if entry is None:
            continue
        chain = entry["chains"].get(report.network)
        if chain is not None:
            context = contexts.get(report.network)
            chain["alt_usd_s"] = _usd_per_auction(
                {m.auction_id: m.delta_surplus for m in report.moves}, context
            )
            chain["alt_native_s"] = _native(report.delta_surplus)

    chains = sorted({r.network for r in reports})
    windows = sorted({(r.start, r.end) for r in reports})
    data = {
        "chains": [{"id": chain} for chain in chains],
        "solvers": sorted(
            solvers.values(),
            key=lambda entry: -max(
                abs(chain["usd_s"]) for chain in entry["chains"].values()
            ),
        ),
        "meta": {
            "windows": [{"start": start, "end": end} for start, end in windows],
            "reports": len(reports) + len(alternatives),
            "format": aggregate.REPORT_FORMAT,
            "price_suspects": {
                r.network: r.price_suspects for r in reports if r.price_suspects
            },
            "missing_data": {
                r.network: r.missing_data for r in reports if r.missing_data
            },
        },
    }
    json.dump(data, sys.stdout, indent=1)
    print(file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
