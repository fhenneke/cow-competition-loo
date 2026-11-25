"""The rewards paid to a solver, derived a second time from the world's accounting tables:
performance summed per auction, each weekly reward split over the window by the solver's
metric in exact fractions, the auctions not yet accounted, and the int-table/mart
disagreements — then held against what `loo.payouts` reports."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction

from loo.payouts import RecordedRewards

from .world import World, auction_times, period_start, period_text, reward_rows

MISMATCH = Fraction(1, 10**6)
"""Relative gap that counts as a rebuilt table here; float noise is ~1e-15, a rebuild far
more, and `loo`'s own threshold (1e-9) sits between."""


@dataclass
class Expected:
    performance: Decimal = Decimal(0)
    consistency: Fraction = Fraction(0)
    quote: Fraction = Fraction(0)
    unaccounted: dict[str, set[int]] = field(default_factory=dict[str, set[int]])
    mismatches: set[tuple[str, str, str]] = field(default_factory=set[tuple[str, str, str]])
    periods: dict[str, dict[str, Fraction | Decimal | bool]] = field(
        default_factory=dict[str, dict[str, Fraction | Decimal | bool]])
    rows: int = 0
    """Per-(auction, solver) weekly amounts, each rounded to the wei by `loo`."""

    @property
    def unaccounted_any(self) -> int:
        return len(set[int]().union(*self.unaccounted.values()))


def metric_sum(rows: dict[int, list[tuple[str, str, float]]], auctions: Collection[int],
               solver: str, position: int) -> Fraction:
    return sum((Fraction(row[2]) for a in auctions for row in rows.get(a, [])
                if row[position] == solver), Fraction(0))


def expected_paid(world: World, window_ids: Collection[int], with_bids: Collection[int],
                  addresses: Collection[str]) -> Expected:
    window = set(window_ids)
    expected_set = window & set(with_bids)
    times = auction_times(world)
    rewards = reward_rows(world)
    cut = world.fct_cut
    out = Expected()
    out.unaccounted = {"performance": set(), "consistency": set(), "quote": set()}
    for a in window:
        rows = rewards.get(a, {}) if cut is None or a <= cut else {}
        if not rows and a in expected_set:
            out.unaccounted["performance"].add(a)
        out.performance += sum((capped for s, (_, capped) in rows.items() if s in addresses),
                               Decimal(0))

    by_period: dict[str, list[int]] = {}
    for a, when in times.items():
        by_period.setdefault(period_text(period_start(when)), []).append(a)
    for label, members in sorted(by_period.items()):
        piece = [a for a in members if a in window]
        if not piece:
            continue
        info: dict[str, Fraction | Decimal | bool] = {}
        for component, position, rows in (("consistency", 0, world.relative),
                                          ("quote", 1, world.shares)):
            mart = world.consistency_mart if component == "consistency" else world.quote_mart
            info[f"{component}_accounted"] = label in mart
            whole, part = Decimal(0), Fraction(0)
            if label not in mart:
                out.unaccounted[component] |= set(piece) & expected_set
            else:
                any_row = next(iter(world.quote_mart[label].values())) if (
                    component == "quote") else None
                for solver in addresses:
                    row = mart[label].get(solver)
                    reward = Fraction(row[0]) if row else Fraction(0)
                    whole += Decimal(str(row[0])) if row else Decimal(0)
                    total = metric_sum(rows, members, solver, position)
                    numerator = metric_sum(rows, piece, solver, position)
                    if reward and total:
                        part += reward * numerator / total
                        out.rows += sum(1 for a in piece if metric_sum(rows, [a], solver,
                                                                      position))
                    # What the mart saw against what the int table holds now.
                    if component == "consistency":
                        ours, theirs = total, Fraction(row[1]) if row else Fraction(0)
                    else:
                        assert any_row is not None
                        _, budget, orders = any_row
                        ours = Fraction(budget) * total / orders if orders else Fraction(0)
                        theirs = reward
                    if abs(ours - theirs) > MISMATCH * max(abs(ours), abs(theirs)):
                        out.mismatches.add((component, label, solver))
            info[f"{component}_period"] = whole
            info[f"{component}_window"] = part
            if component == "consistency":
                out.consistency += part
            else:
                out.quote += part
        out.periods[label] = info
    return out


def close(got: Decimal, want: Fraction, rows: int) -> bool:
    """Within the per-row wei rounding, or 1e-9 of the amount for float summation order."""
    gap = abs(Fraction(got) - want)
    return gap <= max(Fraction(1, 10**9) * abs(want), Fraction(rows + 1))


def check_paid(paid: RecordedRewards, want: Expected, auctions: int) -> None:
    assert paid.auctions == auctions
    assert paid.amount("performance") == want.performance
    assert close(paid.amount("consistency"), want.consistency, want.rows), (
        paid.amount("consistency"), float(want.consistency))
    assert close(paid.amount("quote"), want.quote, want.rows), (
        paid.amount("quote"), float(want.quote))
    assert dict(paid.unaccounted) == {c: len(v) for c, v in want.unaccounted.items()}
    assert paid.unaccounted_any == want.unaccounted_any
    assert {(m.component, m.period, m.solver) for m in paid.mismatches} == want.mismatches
    assert [p.period for p in paid.periods] == sorted(want.periods)
    for part in paid.periods:
        info = want.periods[part.period]
        for component in ("consistency", "quote"):
            assert getattr(part, f"{component}_accounted") == info[f"{component}_accounted"]
            assert getattr(part, f"{component}_period") == info[f"{component}_period"]
            window = getattr(part, f"{component}_window")
            target = info[f"{component}_window"]
            assert isinstance(target, Fraction)
            assert close(window, target, want.rows)
    # The parts are the per-auction amounts, and nothing is stored as zero.
    for values in paid.per_auction.values():
        assert any(values)
    assert sum((p.consistency_window for p in paid.periods), Decimal(0)) == paid.amount(
        "consistency")
    assert sum((p.quote_window for p in paid.periods), Decimal(0)) == paid.amount("quote")
    assert paid.total == sum((paid.amount(c) for c in ("performance", "consistency", "quote")),
                             Decimal(0))
