"""Checks on what the CLI writes: the report files, the comparison table, and the two
validation gates — each against numbers derived without the code that wrote them."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from statistics import median

from loo import aggregate, validate
from loo.counterfactual import Analysis
from loo.extract import AuctionBundle
from loo.payouts import COMPONENTS

from . import expect, oracle, payout_checks, report_checks, runner_up, summary_checks
from .harness import Sim
from .world import STABLES, World


def usd_rates(world: World, auction_ids: set[int]) -> dict[int, Decimal]:
    """USD per native token per auction: the median of the network's stablecoins."""
    rates: dict[int, Decimal] = {}
    for a in world.auctions:
        if a.auction_id not in auction_ids:
            continue
        found = [Decimal(10) ** (2 * world.decimals - d) / Decimal(a.prices[t])
                 for t, d in STABLES[world.network].items() if t in a.prices]
        if found:
            rates[a.auction_id] = median(found)
    return rates


def usd_text(usd: Decimal) -> str:
    magnitude = abs(usd)
    places = 2 if magnitude >= Decimal("0.01") or magnitude == 0 else 4
    sign = "-" if usd < 0 else ""
    return sign + "$" + f"{magnitude:,.{places}f}"


def cow_rate(world: World, block: int) -> Decimal | None:
    for low, high, rate in world.periods:
        if low <= block <= high:
            return Decimal(str(rate)) if rate is not None else None
    return None


def paid_text(amount: Decimal, unaccounted: int, none_accounted: bool) -> str:
    """Nothing accounted yet shows no amount; a partial amount says how much is missing."""
    if unaccounted and none_accounted:
        return f"not yet accounted ({unaccounted:,} auctions)"
    return oracle.eth_text(amount, 4) + " ETH" + (
        f"; incomplete: {unaccounted:,} auctions not yet accounted" if unaccounted else "")


def check_paid_rows(rows: dict[str, tuple[str, ...]], column: int,
                    head: aggregate.Report) -> None:
    """Recorded amounts unsigned, the budget rows signed like every delta."""
    paid = head.paid
    assert paid is not None
    assert rows["rewards paid to the solver (recorded)"][column] == paid_text(
        paid.total, paid.unaccounted_any,
        all(paid.unaccounted[c] >= paid.auctions for c in COMPONENTS))
    for component in COMPONENTS:
        missing = paid.unaccounted[component]
        assert rows[f"  {component} rewards (recorded)"][column] == paid_text(
            paid.amount(component), missing, missing >= paid.auctions)
    ratio = rows["  rewards paid ÷ −Δsurplus"][column]
    if head.delta_surplus < 0:
        assert ratio == f"{paid.total / Decimal(-head.delta_surplus):.2f}" + (
            "; rewards paid incomplete" if paid.unaccounted_any else "")
    else:
        assert ratio == "n/a — Δsurplus is not negative"
    budget, capped = head.delta_consistency_budget, head.delta_rewards_capped
    assert budget is not None and capped is not None
    assert rows["  Δconsistency budget (estimate)"][column] == (
        oracle.signed_eth_text(budget) + " ETH")
    assert rows["  sum = Δ Σ upper reward caps (estimate)"][column] == (
        oracle.signed_eth_text(budget + capped) + " ETH")


def check_reports_and_compare(
    sim: Sim, world: World, tmp_path: Path, entries: list[str],
    direct: dict[tuple[str, str], Analysis], code: int,
) -> None:
    """The report files round-trip, their totals are the sums of their auctions, the
    COW conversion uses each auction's own period, the recorded rewards are the world's,
    and the comparison table says what the reports say."""
    paths = sorted(str(p) for p in tmp_path.glob("*.*.json"))
    blocks = {a.auction_id: a.block for a in world.auctions}
    window_ids = {a.auction_id for a in world.auctions} | set(world.extra_window_ids)
    with_bids = {a.auction_id for a in world.auctions}
    absent = False
    for path in paths:
        payload = json.loads(Path(path).read_text())
        report = aggregate.load_report(path)
        analysis = direct[(report.solver, report.outcome_rule)]
        report_checks.check_report_properties(report, payload, analysis)
        report_checks.check_tampering(payload, tmp_path)
        runner_up.check_report(report, payload, analysis)
        assert report.delta_consistency_budget == analysis.delta_consistency_budget
        assert report.paid is not None
        payout_checks.check_paid(
            report.paid,
            payout_checks.expected_paid(world, window_ids, with_bids, analysis.addresses),
            auctions=len(with_bids),
        )
        moves = {m.auction_id: m for m in report.moves}
        assert set(moves) == {r.auction_id for r in analysis.changed}
        assert sum(m.delta_surplus for m in report.moves) == report.delta_surplus
        assert sum(
            row["delta_protocol_fees"] for row in payload["changed"] if row["fees_complete"]
        ) == report.delta_protocol_fees
        assert analysis.delta_surplus == sum(r.delta_surplus for r in analysis.changed)
        assert analysis.delta_rewards == sum(r.delta_rewards for r in analysis.changed)
        absent |= any(
            not (expect.label_addresses(world, name.strip()) & analysis.addresses_seen)
            for name in report.solver.split(",")
        )
        cow = payload["cow"]
        expected_cow, expected_capped, expected_missing = Decimal(0), Decimal(0), 0
        converted, missing_native, missing_capped = 0, 0, Decimal(0)
        for r in analysis.changed:
            capped = r.delta_rewards_capped
            if not (r.delta_rewards or (capped is not None and capped != 0)):
                continue
            rate = cow_rate(world, blocks[r.auction_id])
            if rate:
                expected_cow += Decimal(r.delta_rewards) / rate
                if capped is not None:
                    expected_capped += capped / rate
                converted += r.delta_rewards
            else:
                expected_missing += 1
                missing_native += r.delta_rewards
                if capped is not None:
                    missing_capped += capped
        assert Decimal(cow["cow_wei"]) == expected_cow
        assert Decimal(cow["cow_wei_capped"]) == expected_capped
        assert cow["auctions_without_rate"] == expected_missing
        assert cow["converted_native"] == converted
        assert cow["native_without_rate"] == missing_native
        assert Decimal(cow["capped_without_rate"]) == missing_capped
    if absent:
        assert code == 4
    windows = aggregate.group_reports([aggregate.load_report(p) for p in paths])
    assert len(windows) == len(entries)
    table = aggregate.comparison(windows)
    rows = dict(table.rows)
    for column, window in enumerate(windows):
        head = window.headline
        alt = window.by_rule["assume-settled"]
        assert rows["Δsurplus (inherited)"][column] == (
            oracle.signed_eth_text(head.delta_surplus) + " ETH")
        assert rows["  assume-settled"][column] == (
            oracle.signed_eth_text(alt.delta_surplus) + " ETH")
        suspects = f" ({head.price_suspects} price-suspect" if head.price_suspects else ""
        assert rows["auctions analysed"][column].startswith(
            f"{head.auctions - head.price_suspects:,} of {head.auctions + head.fast_path:,}"
            + suspects)
        assert head.delta_rewards is not None and head.delta_rewards_capped is not None
        assert rows["Δrewards uncapped"][column].startswith(
            oracle.signed_eth_text(head.delta_rewards) + " ETH")
        assert rows["net change (Δsurplus − capped Δrewards)"][column] == (
            oracle.signed_eth_text(Decimal(head.delta_surplus) - head.delta_rewards_capped)
            + " ETH")
        fees = rows["Δprotocol and partner fees"][column]
        assert fees.startswith(
            oracle.signed_eth_text(head.delta_protocol_and_partner_fees) + " ETH")
        assert ("auctions unpriced" in fees) == bool(head.auctions_fees_incomplete)
        assert rows["Δprotocol fee"][column] == (
            oracle.signed_eth_text(head.delta_protocol_fees) + " ETH")
        assert rows["  DAO fee revenue with the solver"][column] == (
            oracle.eth_text(head.protocol_fees_base, 4) + " ETH")
        if head.window_executions:
            share = Decimal(head.orders_only_with_solver) / Decimal(head.window_executions)
            assert rows["orders no longer executed"][column] == (
                f"{head.orders_only_with_solver:,} ({share:.3%} of the window's "
                f"{head.window_executions:,} executions)")
        # Recorded, so the same whatever the outcome rule.
        assert all(r.paid == head.paid for r in window.by_rule.values())
        check_paid_rows(rows, column, head)
    report_checks.check_more_rows(table, windows)
    runner_up.check_rows(table, windows)
    rendered = sim.cli("compare", *paths, "--skip-usd")
    assert rendered.code == 0, rendered.err
    lines = rendered.out.splitlines()
    for label, cells in table.rows:
        line = next(line for line in lines if line.startswith(label))
        for cell in cells:
            assert cell in line
    # With USD: each auction converted at its own stablecoin-implied rate.
    usd_run = sim.cli("compare", *paths, "--markdown")
    assert usd_run.code == 0, usd_run.err
    # The rates are cached: a second run prints the same and asks the DB for no rate.
    asked = len(sim.server.queries)
    again = sim.cli("compare", *paths, "--markdown")
    assert again.code == 0, again.err
    assert again.out == usd_run.out
    assert not any("ap.token = any(" in sql and "ap.auction_id, encode" in sql
                   for sql in sim.server.queries[asked:])
    moved = {m.auction_id for w in windows for m in w.headline.moves}
    rates = usd_rates(world, moved)
    for window in windows:
        head = window.headline
        if not rates or not head.moves:
            continue
        fallback = median(rates.values())
        usd = sum((Decimal(m.delta_surplus) * rates.get(m.auction_id, fallback)
                   for m in head.moves), Decimal(0)) / Decimal(oracle.ONE)
        cell = oracle.signed_eth_text(head.delta_surplus) + " ETH (" + usd_text(usd) + ")"
        assert cell in usd_run.out
    # Recorded rewards convert per auction too, every auction `compare` loaded a rate for.
    loaded = {m.auction_id for w in windows for r in w.by_rule.values() for m in r.moves}
    loaded |= {a for w in windows if w.headline.paid for a in w.headline.paid.per_auction}
    rates = usd_rates(world, loaded)
    for window in windows:
        paid = window.headline.paid
        if not rates or paid is None or not paid.per_auction or (
                min(paid.unaccounted.values()) >= paid.auctions):
            continue
        fallback = median(rates.values())
        usd = sum((total * rates.get(a, fallback) for a, total in
                   paid.totals_by_auction().items()), Decimal(0)) / Decimal(oracle.ONE)
        cell = oracle.eth_text(paid.total, 4) + " ETH (" + usd_text(usd) + ")"
        assert cell in usd_run.out
    summary_checks.check_cli(sim, paths, windows, rates)


def check_validate(sim: Sim, world: World, tmp_path: Path, bundles: list[AuctionBundle]) -> None:
    out = tmp_path / "validate.out"
    run_ = sim.cli("validate", *sim.window_args(), "--max-winners", str(world.max_winners),
                   "--cross-check-surplus", "--out", str(out))
    payload = json.loads(out.read_text())
    reports = [validate.check_auction(b, world.weth, max_winners=world.max_winners)
               for b in bundles]
    assert payload["unexplained"] == []
    assert run_.code == 0, run_.err
    assert payload["auctions"] == len(bundles)
    assert payload["fast_path_auctions"] == [a.auction_id for a in world.auctions if a.fast_path]
    assert payload["solutions"] == sum(len(b.bids) for b in bundles)
    assert payload["auctions_winners_match"] == sum(r.winners_match for r in reports)
    assert payload["auctions_filter_match"] == sum(r.filter_matches for r in reports)
    assert payload["auctions_reference_match_recomputed"] == sum(
        r.reference_matches_recomputed for r in reports)
    assert payload["auctions_reference_match_observed"] == len(bundles)
    assert payload["auctions_pick_match_observed"] == len(bundles)
    assert payload["valuation_failures"] == 0
    assert payload["score_anchor_failures"] == 0
    assert payload["auctions_guess_could_matter"] == sum(r.guess_could_matter for r in reports)
    multi = [c for r in reports for c in r.checks if c.n_pairs > 1 and c.total]
    assert payload["multi_pair_solutions"] == len(multi)
    assert payload["multi_pair_filter_mismatch"] == sum(c.filter_differs for c in multi)
    assert payload["multi_pair_filter_scales"].get("surplus", 0) == sum(
        c.filter_scale == "surplus" for c in multi)
    assert "bug" not in payload["filter_causes"]
    assert [m["auction_id"] for m in payload["mismatched"]] == [
        r.auction_id for r in reports if r.disagreement != "none"]
    # The bracket against the truth: what any valid split forces, the record obeys.
    by_id = {a.auction_id: a for a in world.auctions}
    for report in reports:
        auction = by_id[report.auction_id]
        for check in report.checks:
            solution = next(s for s in auction.solutions if s.uid == check.uid)
            surplus = expect.surplus_pairs(world, auction, solution)
            base = report.baselines
            if len(surplus) <= 1:
                assert check.bracket == "must_keep"
                continue
            need = sum(max(base.get(p, 0), u) for p, u in surplus.items())
            if need > solution.score:
                assert check.bracket == "must_filter" and solution.filtered_out
            elif all(u >= base.get(p, 0) for p, u in surplus.items()):
                assert check.bracket == "must_keep" and not solution.filtered_out
            else:
                assert check.bracket == "undetermined"
            if check.filter_differs:
                assert check.filter_scale == "surplus"
                assert check.filter_cause in ("proxy", "model")
    # The surplus cross-check finds exactly the rows the model got wrong.
    cross = payload["surplus_cross_check"]
    live = {a.auction_id for a in world.auctions if not a.fast_path}
    off = {k for k in world.surplus_model_off if k[0] in live}
    null = {k for k in world.surplus_model_null if k[0] in live}
    keys = {(a.auction_id, s.uid, t.uid) for a in world.auctions if a.auction_id in live
            for s in a.solutions for t in s.trades}
    assert {tuple(m[:3]) for m in cross["mismatches"]} == off - null
    assert cross["compared"] == len(keys - null)
    assert cross["agreed"] == len(keys - null) - len(off - null)
    for _, _, _, ours, theirs in cross["mismatches"]:
        assert theirs == ours + 1


def check_validate_rewards(sim: Sim, world: World, tmp_path: Path) -> None:
    out = tmp_path / "rewards.out"
    run_ = sim.cli("validate-rewards", *sim.window_args(), "--out", str(out))
    payload = json.loads(out.read_text())
    off = set(world.mart_off)
    assert {(m["auction_id"], m["solver"]) for m in payload["mismatches"]} == off
    for m in payload["mismatches"]:
        assert m["differing_fields"] == ["uncapped_reward"]
    rows = sum(len(a.mart) for a in world.auctions)
    assert payload["rows"] == rows
    assert payload["rows_matched"] == rows - len(off)
    assert payload["auctions"] == len(
        {a.auction_id for a in world.auctions} | set(world.extra_window_ids))
    assert payload["auctions_with_winners"] == sum(1 for a in world.auctions if a.winners)
    assert payload["gate_met"] == (not off)
    assert run_.code == (2 if off else 0), run_.err
