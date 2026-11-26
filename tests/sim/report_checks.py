"""What `aggregate` derives from a report file, derived a second time: the price impact,
the only-with slice, the coverage shares, the distribution behind every sum, the table's
detail rows, and the refusal of a file that is not internally consistent."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from loo import aggregate
from loo.counterfactual import Analysis

from . import oracle


def bps_text(value: Decimal, places: int = 2) -> str:
    return ("+" if value > 0 else "") + f"{value:.{places}f} bps"


def pct_text(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "n/a"


def check_report_properties(report: aggregate.Report, payload: dict[str, Any],
                            analysis: Analysis) -> None:
    diffs = [d for row in payload["changed"] for d in row["order_diffs"]]
    both = [d for d in diffs if d["executed_base"] and d["executed_loo"]]
    fok = [d for d in both if not d["partially_fillable"]]
    moved = [d for d in fok if (d["surplus_loo"] or 0) != (d["surplus_base"] or 0)
             and d["volume_base"]]
    impact = report.price_impact
    assert impact.orders_still_traded == len(fok)
    assert impact.orders_partially_fillable == len(both) - len(fok)
    assert impact.orders_moved == len(moved)
    delta = sum((d["surplus_loo"] or 0) - (d["surplus_base"] or 0) for d in moved)
    volume = sum(d["volume_base"] for d in moved)
    assert (impact.delta_surplus, impact.volume_base) == (delta, volume)
    per_order = sorted(Decimal((d["surplus_loo"] or 0) - (d["surplus_base"] or 0)) * 10_000
                       / Decimal(d["volume_base"]) for d in moved)
    if per_order:
        assert impact.median_bps == median(per_order)
        assert impact.weighted_bps == Decimal(delta) * 10_000 / Decimal(volume)
    else:
        assert impact.median_bps is None and impact.weighted_bps is None
    lost = [d for d in diffs if d["executed_base"] and not d["executed_loo"]]
    assert report.orders_only_with_solver == len(lost) == analysis.orders_only_with_solver
    assert report.delta_surplus_only_with == -sum(d["surplus_base"] or 0 for d in lost)
    assert report.orders_only_with_fok == len({d["order_uid"] for d in lost
                                               if not d["partially_fillable"]})
    assert report.orders_only_with_partial == len({d["order_uid"] for d in lost
                                                   if d["partially_fillable"]})
    window = analysis.window_volume_base
    assert report.overall_bps == (Decimal(delta) * 10_000 / Decimal(window) if window else None)
    executions = analysis.window_order_executions_base
    assert report.coverage_share == (
        Decimal(len(lost)) / Decimal(executions) if executions else None)
    orders = analysis.window_orders_base
    assert report.coverage_share_fok == (
        Decimal(report.orders_only_with_fok) / Decimal(orders) if orders else None)
    excluded = analysis.exclude_price_suspect
    assert report.analysed == analysis.auctions - (
        len(analysis.price_suspect_auctions) if excluded else 0)
    assert report.auctions_solver_won == analysis.auctions_solver_won_baseline
    assert report.protocol_fees_base == analysis.protocol_fees_base
    assert report.auctions_capped_skipped == analysis.auctions_capped_skipped
    assert (report.multi_pair_solutions, report.multi_pair_filter_proxied) == (
        analysis.multi_pair_solutions, analysis.multi_pair_filter_proxied)
    deltas = {m.auction_id: m.delta_surplus for m in report.moves}
    spread = aggregate.distribution(deltas)
    nonzero = [Decimal(v) for v in deltas.values() if v]
    assert spread.n_nonzero == len(nonzero)
    assert (spread.n_positive, spread.n_negative) == (
        sum(1 for v in nonzero if v > 0), sum(1 for v in nonzero if v < 0))
    assert spread.sum_positive == sum((v for v in nonzero if v > 0), Decimal(0))
    assert spread.sum_negative == sum((v for v in nonzero if v < 0), Decimal(0))
    assert spread.total == sum(nonzero, Decimal(0))
    if nonzero:
        assert spread.median_abs == median(sorted(abs(v) for v in nonzero))
        biggest = max(abs(v) for v in nonzero)
        assert spread.largest is not None and abs(spread.largest) == biggest
        assert deltas[spread.largest_auction_id or 0] == spread.largest
        assert spread.largest_share == (
            float(biggest / abs(spread.total)) if spread.total else None)
    else:
        assert spread.median_abs is None and spread.largest is None


def check_tampering(payload: dict[str, Any], tmp_path: Path) -> None:
    """A report that is not internally consistent is refused rather than tabulated."""

    def refused(change: dict[str, Any], name: str) -> None:
        altered = copy.deepcopy(payload)
        altered.update(change)
        target = tmp_path / f"altered-{name}.report"
        target.write_text(json.dumps(altered))
        try:
            aggregate.load_report(str(target))
        except ValueError:
            return
        raise AssertionError(f"load_report accepted a report with {name} altered")

    refused({"format": payload["format"] - 1}, "format")
    refused({"sign_convention": "actual-minus-counterfactual"}, "sign")
    if payload["changed"]:
        refused({"delta_surplus": payload["delta_surplus"] + 1}, "surplus")
        refused({"delta_rewards": payload["delta_rewards"] - 1}, "rewards")
        refused({"delta_rewards_capped": str(Decimal(payload["delta_rewards_capped"]) + 1)},
                "capped")
        refused({"delta_consistency_budget":
                 str(Decimal(payload["delta_consistency_budget"]) + 1)}, "budget")
        refused({"orders_only_with_solver": payload["orders_only_with_solver"] + 1}, "lost")
        margin = payload["delta_reference_margin"]
        refused(dict(delta_reference_margin=margin + 1), "margin")
        refused({"delta_score": payload["delta_score"] + 1}, "score")
        refused({"delta_protocol_and_partner_fees":
                 payload["delta_protocol_and_partner_fees"] + 1}, "fees")
        if payload["delta_pressed_rewards_capped"] is not None:
            refused({"delta_pressed_rewards_capped":
                     str(Decimal(payload["delta_pressed_rewards_capped"]) + 1)}, "pressed")
        if payload["delta_margin_above_cap"] is not None:
            refused({"delta_margin_above_cap":
                     str(Decimal(payload["delta_margin_above_cap"]) + 1)}, "above cap")
        if payload["delta_settled_margin"] is not None:
            refused({"delta_settled_margin": payload["delta_settled_margin"] + 1},
                    "settled margin")
    for name in ("performance", "consistency", "quote", "total"):
        paid = copy.deepcopy(payload["paid"])
        paid[name] = str(Decimal(paid[name]) + 1)
        refused({"paid": paid}, f"paid {name}")


def check_more_rows(table: aggregate.Table, windows: list[aggregate.SolverWindow]) -> None:
    rows = dict(table.rows)
    assert table.columns == tuple(w.solver for w in windows)
    for column, window in enumerate(windows):
        head = window.headline
        cell = {label: cells[column] for label, cells in rows.items()}
        analysed = head.analysed
        assert cell["solver bid"] == (
            f"{head.auctions_with_solver:,} ({pct_text(head.auctions_with_solver, analysed)})")
        assert cell["solver won"] == (
            f"{head.auctions_solver_won:,} ({pct_text(head.auctions_solver_won, analysed)})")
        assert cell["Δpartner fee"] == oracle.signed_eth_text(head.delta_partner_fees) + " ETH"
        unpriced = head.auctions_fees_incomplete
        assert cell["Δscore (Δsurplus + Δprotocol and partner fees)"] == (
            oracle.signed_eth_text(head.delta_surplus + head.delta_protocol_and_partner_fees)
            + " ETH" + (f"; fees unpriced in {unpriced:,} auctions" if unpriced else ""))
        assert head.delta_rewards_capped is not None
        capped = cell["Δrewards capped (estimate)"]
        assert capped.startswith(oracle.signed_eth_text(head.delta_rewards_capped) + " ETH")
        if head.delta_rewards_capped_cow is not None and not head.cow_auctions_without_rate:
            cow = (head.delta_rewards_capped_cow / Decimal(10**18)).quantize(Decimal("0.01"))
            assert f"({cow} COW)" in capped
        assert cell["orders executed only without"] == f"{head.orders_only_without_solver:,}"
        assert cell["auctions where anything moved"] == f"{len(head.moves):,}"
        assert cell["fairness filter relaxed"] == (
            f"{head.auctions_filter_relaxed} (newly kept won {head.auctions_newly_kept_won})")
        assert cell["  filter verdicts resting on surplus, in those auctions"] == (
            f"{head.multi_pair_filter_proxied:,} of {head.multi_pair_solutions:,} "
            f"({pct_text(head.multi_pair_filter_proxied, head.multi_pair_solutions)}), "
            f"could have moved winners in {head.auctions_filter_guess_could_matter:,}")
        assert cell["  Δsurplus from orders no longer executed"] == (
            oracle.signed_eth_text(head.delta_surplus_only_with) + " ETH")
        assert cell["  share of orders in auctions the solver bid in"] == (
            f"{pct_text(head.orders_only_with_solver, head.orders_compared)} "
            f"of {head.orders_compared:,} compared")
        deltas = [Decimal(m.delta_surplus) for m in head.moves if m.delta_surplus]
        if deltas:
            middle = median(sorted(abs(d) for d in deltas))
            assert cell["  median non-zero auction"] == (
                f"{oracle.eth_text(middle, 6)} ETH over {len(deltas):,} auctions")
            up = [d for d in deltas if d > 0]
            down = [d for d in deltas if d < 0]
            assert cell["  auctions moved + / −"] == (
                f"{len(up):,} ({oracle.signed_eth_text(sum(up, Decimal(0)))} ETH) / "
                f"{len(down):,} ({oracle.signed_eth_text(sum(down, Decimal(0)))} ETH)")
            assert cell["  largest single auction"].startswith(
                oracle.signed_eth_text(max(deltas, key=abs)) + " ETH")
        else:
            assert cell["  median non-zero auction"] == "no auction moved"
            assert cell["  largest single auction"] == "n/a"
        impact = head.price_impact
        if impact.weighted_bps is not None and impact.median_bps is not None:
            assert cell["relative price change, still-traded orders"].startswith(
                bps_text(impact.weighted_bps) + " volume-weighted over "
                + oracle.eth_text(impact.volume_base, 2) + " ETH")
            assert cell["  median moved order"] == (
                f"{bps_text(impact.median_bps)} over {impact.orders_moved:,} orders")
        else:
            assert cell["relative price change, still-traded orders"].startswith(
                "no still-traded order moved")
            assert cell["  median moved order"] == "n/a"
        overall = head.overall_bps
        if overall is not None:
            assert cell["  price change over all traded volume"] == (
                f"{bps_text(overall, 3)} of {oracle.eth_text(head.window_volume, 2)} ETH "
                f"traded ({head.window_orders:,} orders)")
        if head.quote is None:
            assert cell["quote price change, all executed volume"] == "no quote data"
    expected_warnings = sum(
        bool(r.auctions_capped_skipped) + bool(r.cow_auctions_without_rate)
        for w in windows for r in w.by_rule.values())
    expected_warnings += sum(bool(w.headline.paid and w.headline.paid.mismatches)
                             for w in windows)
    assert len(table.warnings) == expected_warnings
    markdown = aggregate.render_markdown(table)
    for label, cells in table.rows:
        shown = "&nbsp;&nbsp;" + label.strip() if label.startswith("  ") else label
        assert "| " + shown + " | " + " | ".join(cells) + " |" in markdown
