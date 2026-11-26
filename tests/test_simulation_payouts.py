"""The rewards recorded as paid to a solver over free windows: whole accounting periods, a
span of at least two, that span cut at period boundaries and at an auction, a last period
not paid out yet, and an int table rebuilt after its mart — each held against the world's
marts and the second derivation in `sim.payout_checks`."""

from __future__ import annotations

import json
import random
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from sim import payout_checks, summary_checks
from sim.harness import Sim, simulate
from sim.world import (
    World,
    generate,
    period_start,
    period_text,
    record_weekly_marts,
    reward_rows,
)

from loo import aggregate, extract, payouts

WEEK = timedelta(days=7)


def period_starts(world: World) -> list[datetime]:
    """From the window's first accounting period through the one after its last."""
    begin = period_start(datetime.fromisoformat(world.start))
    after = period_start(datetime.fromisoformat(world.end) - timedelta(seconds=1)) + WEEK
    starts: list[datetime] = []
    while begin <= after:
        starts.append(begin)
        begin += WEEK
    return starts


def with_bids(world: World) -> set[int]:
    """The window's auctions and every shadow: each stands for an auction with bids."""
    return {a.auction_id for a in world.auctions} | {s.auction_id for s in world.shadows}


def text(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S}"


def load(sim: Sim, world: World, begin: datetime, end: datetime, addresses: Collection[str],
         ids: list[int] | None = None) -> tuple[payouts.RecordedWindow, list[int]]:
    conn = sim.conn()
    window = extract.auctions_in_window(conn, text(begin), text(end)) if ids is None else ids
    recorded = payouts.load_recorded(conn, window, with_bids(world), text(begin), text(end),
                                     addresses, network=world.network, chunk_size=3)
    return recorded, window


def test_whole_periods_are_their_mart_rows(seed: int) -> None:
    """Over a whole period the window's share is one: each weekly reward is the mart's
    row, performance is the period's reward rows, and an unpaid period counts its
    auctions instead of reporting zero."""
    world = generate(seed)
    addresses = sorted(world.bidding_addresses())
    bids = with_bids(world)
    with simulate(world) as sim:
        for begin in period_starts(world):
            recorded, window = load(sim, world, begin, begin + WEEK, addresses)
            label = period_text(begin)
            expected = set(window) & bids
            for solver in addresses:
                paid = recorded.for_addresses([solver])
                want = payout_checks.expected_paid(world, window, bids, [solver])
                payout_checks.check_paid(paid, want, auctions=len(expected))
                rows = reward_rows(world)
                assert paid.amount("performance") == sum(
                    (rows[a][solver][1] for a in window if solver in rows.get(a, {})),
                    Decimal(0))
                for component in payouts.WEEKLY:
                    mart = (world.consistency_mart if component == "consistency"
                            else world.quote_mart).get(label)
                    if mart is not None:
                        row = mart.get(solver)
                        target = Fraction(row[0]) if row else Fraction(0)
                        assert payout_checks.close(paid.amount(component), target, len(window))
                        assert paid.unaccounted[component] == 0
                    else:
                        assert paid.amount(component) == 0
                        assert paid.unaccounted[component] == len(expected)


def test_split_windows_sum_to_the_whole(seed: int) -> None:
    """A span of two or more periods, cut at every period boundary or at one auction:
    the pieces' per-auction amounts are the whole's, exactly, and so are their sums and
    their unaccounted counts."""
    world = generate(seed)
    rng = random.Random(seed)
    starts = period_starts(world)
    assert len(starts) >= 2
    bidders = sorted(world.bidding_addresses())
    addresses = rng.sample(bidders, min(len(bidders), rng.randint(1, 2)))
    begin, end = starts[0], starts[-1] + WEEK
    with simulate(world) as sim:
        whole_window, window = load(sim, world, begin, end, addresses)
        whole = whole_window.for_addresses(addresses)
        by_period = [load(sim, world, b, b + WEEK, addresses)[0].for_addresses(addresses)
                     for b in starts]
        cut = rng.choice(window)
        halves = [
            load(sim, world, begin, end, addresses, ids=part)[0].for_addresses(addresses)
            for part in ([a for a in window if a <= cut], [a for a in window if a > cut])
        ]
    payout_checks.check_paid(
        whole, payout_checks.expected_paid(world, window, with_bids(world), addresses),
        auctions=len(set(window) & with_bids(world)))
    for pieces in (by_period, halves):
        merged: dict[int, tuple[Decimal, Decimal, Decimal]] = {}
        for piece in pieces:
            assert not set(merged) & set(piece.per_auction)
            merged.update(piece.per_auction)
        assert merged == dict(whole.per_auction)
        for component in payouts.COMPONENTS:
            assert sum((p.amount(component) for p in pieces), Decimal(0)) == whole.amount(
                component)
            assert sum(p.unaccounted[component] for p in pieces) == whole.unaccounted[component]
        assert sum(p.auctions for p in pieces) == whole.auctions
    # Cut at period boundaries, each piece is exactly one of the whole's periods.
    for piece in by_period:
        for part in piece.periods:
            (same,) = [p for p in whole.periods if p.period == part.period]
            assert part == same


def analyse(sim: Sim, world: World, tmp_path: Path, solver: str) -> tuple[aggregate.Report, str]:
    out = tmp_path / "{solver}.{rule}.json"
    result = sim.cli("analyse", "--network", world.network, "--start", world.start,
                     "--end", world.end, "--max-winners", str(world.max_winners),
                     "--solver", solver, "--out", str(out))
    assert result.code in (0, 4), result.err
    assert "\nrewards paid to the solver (recorded" in result.out
    (path,) = tmp_path.glob("*.inherited.json")
    compared = sim.cli("compare", str(path), "--skip-usd")
    assert compared.code == 0, compared.err
    return aggregate.load_report(str(path)), compared.out


def test_unpaid_rewards_are_counted_not_zero(seed: int, tmp_path: Path) -> None:
    """The window's last period missing from both weekly marts and the reward rows of its
    later auctions not written yet: each component counts the auctions it lacks, the
    figures cover the rest, and `compare` says so in the row."""
    world = generate(seed)
    last = period_start(datetime.fromisoformat(world.end) - timedelta(seconds=1))
    world.consistency_mart.pop(period_text(last), None)
    world.quote_mart.pop(period_text(last), None)
    live = sorted(a.auction_id for a in world.auctions)
    world.fct_cut = live[len(live) // 2]
    rng = random.Random(seed)
    solver = world.solver_of(rng.choice(sorted(world.bidding_addresses()))).label
    with simulate(world) as sim:
        report, compared = analyse(sim, world, tmp_path, solver)
    paid = report.paid
    assert paid is not None
    addresses = json.loads(Path(report.path).read_text())["addresses"]
    window = set(live) | set(world.extra_window_ids)
    want = payout_checks.expected_paid(world, window, live, addresses)
    payout_checks.check_paid(paid, want, auctions=len(live))
    assert paid.unaccounted["performance"] == sum(1 for a in live if a > world.fct_cut)
    in_last = sum(1 for a in world.auctions if a.time >= last)
    assert paid.unaccounted["consistency"] == paid.unaccounted["quote"] == in_last
    assert paid.unaccounted_any == len({a for a in live if a > world.fct_cut}
                                       | {a.auction_id for a in world.auctions
                                          if a.time >= last})
    lines = compared.splitlines()
    line = next(row for row in lines if row.startswith("rewards paid to the solver (recorded)"))
    assert f"incomplete: {paid.unaccounted_any:,} auctions not yet accounted" in line
    for component in payouts.WEEKLY:
        line = next(row for row in lines if row.startswith(f"  {component} rewards (recorded)"))
        missing = paid.unaccounted[component]
        if missing == len(live):
            assert f"not yet accounted ({missing:,} auctions)" in line
        elif missing:
            assert f"incomplete: {missing:,} auctions not yet accounted" in line
    # The summary marks the same gaps: pending for none accounted, * for some.
    windows = aggregate.group_reports([report])
    table = aggregate.summary(windows)
    summary_checks.check_table(table, windows, {})
    ((_, cells),) = table.rows
    for component in payouts.COMPONENTS:
        index = table.columns.index(component)
        missing = paid.unaccounted[component]
        assert (cells[index] == "pending") == (missing == len(live))
        assert cells[index].endswith("*") == (0 < missing < len(live))


def test_an_int_table_rebuilt_after_its_mart(seed: int, tmp_path: Path) -> None:
    """One relative-surplus row and one quote-share row changed after the weekly marts
    were built: the full-period sums no longer match the marts' totals, the report flags
    exactly those (week, solver) pairs and `compare` warns, and the window's part is still
    the paid reward split by the int table as it now stands."""
    world = generate(seed)
    record_weekly_marts(world)  # every period paid out, so there is a mart to disagree with
    rng = random.Random(seed)
    live = {a.auction_id: a for a in world.auctions}

    def candidates(rows: dict[int, list[tuple[str, str, float]]], position: int,
                   mart: Mapping[str, Mapping[str, tuple[float, ...]]]
                   ) -> list[tuple[int, int, str]]:
        found: list[tuple[int, int, str]] = []
        for a, entries in rows.items():
            if a not in live:
                continue
            label = period_text(period_start(live[a].time))
            for i, row in enumerate(entries):
                solver = row[0] if position == 0 else row[1]
                if row[2] > 0 and label in mart and mart[label].get(solver, (0.0,))[0]:
                    found.append((a, i, solver))
        return found

    relative = candidates(world.relative, 0, world.consistency_mart)
    if not relative:
        return
    auction_id, index, address = rng.choice(relative)
    solver_row = world.relative[auction_id][index]
    world.relative[auction_id][index] = (solver_row[0], solver_row[1], solver_row[2] + 0.5)
    shares = [c for c in candidates(world.shares, 1, world.quote_mart) if c[2] == address]
    if shares:
        share_auction, share_index, _ = rng.choice(shares)
        row = world.shares[share_auction][share_index]
        world.shares[share_auction][share_index] = (row[0], row[1], row[2] + 0.5)
    label = world.solver_of(address).label
    with simulate(world) as sim:
        report, compared = analyse(sim, world, tmp_path, label)
    paid = report.paid
    assert paid is not None
    addresses = json.loads(Path(report.path).read_text())["addresses"]
    live_ids = sorted(live)
    window = set(live_ids) | set(world.extra_window_ids)
    want = payout_checks.expected_paid(world, window, live_ids, addresses)
    assert ("consistency", period_text(period_start(live[auction_id].time)), address) in (
        want.mismatches)
    assert len(want.mismatches) == 1 + bool(shares)
    payout_checks.check_paid(paid, want, auctions=len(live_ids))
    assert "disagrees with the weekly mart's own total" in compared
