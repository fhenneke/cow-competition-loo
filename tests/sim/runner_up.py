"""The runner-up measures, derived a second time from the oracle's arbitration: each
pressed winner's margin over its reference score on both sides, the removed solvers'
losing bids that set another winner's reference, the volume under the moved margins,
and what the report and the comparison table make of them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from loo import aggregate
from loo.counterfactual import Analysis, AuctionCounterfactual

from . import oracle
from .oracle import Bid
from .world import Auction


def margin(winning: int, reference: int) -> int:
    """What a settled winner earns before caps: its share of the winning score above the
    reference (docs/rewards.md)."""
    return winning - min(winning, reference)


@dataclass(frozen=True)
class RunnerUp:
    margins_base: dict[str, int]
    margins_loo: dict[str, int]
    runner_up_uids: frozenset[int]
    volume: int


def expected(auction: Auction, bids: list[Bid], removed: frozenset[str],
             max_winners: int) -> RunnerUp:
    """`bids` in uid order, exactly what `loo` arbitrates."""
    by_uid = {b.uid: b for b in bids}
    base = oracle.arbitrate(bids, max_winners)
    loo = oracle.arbitrate([b for b in bids if b.solver not in removed], max_winners)

    def wins(outcome: oracle.Outcome) -> dict[str, frozenset[int]]:
        return {solver: frozenset(u for u in outcome.winners if by_uid[u].solver == solver)
                for solver in {by_uid[u].solver for u in outcome.winners}}

    won_base, won_loo = wins(base), wins(loo)
    # Pressed: outside the removal, the very same winning solutions on both sides.
    pressed = {s: uids for s, uids in won_base.items()
               if s not in removed and won_loo.get(s) == uids}
    refs_base = oracle.references(base.ranked, base.winners, max_winners)
    refs_loo = oracle.references(loo.ranked, loo.winners, max_winners)
    total_base = sum(by_uid[u].total for u in base.winners)
    total_loo = sum(by_uid[u].total for u in loo.winners)
    margins_base = {s: margin(total_base, refs_base[s][0]) for s in pressed}
    margins_loo = {s: margin(total_loo, refs_loo[s][0]) for s in pressed}
    picks = oracle.reference_picks(base.ranked, base.winners, max_winners)
    runner_up = frozenset(
        u for s, taken in picks.items() if s not in removed for u in taken
        if by_uid[u].solver in removed and u not in base.winners)
    solutions = {s.uid: s for s in auction.solutions}
    volume = sum(
        t.executed_buy * auction.prices[t.buy_token] // oracle.ONE
        for s, uids in pressed.items() if margins_base[s] != margins_loo[s]
        for u in uids for t in solutions[u].trades)
    return RunnerUp(margins_base, margins_loo, runner_up, volume)


def above_cap(margins: Mapping[str, int],
              rewards: Mapping[str, oracle.RewardRow]) -> Decimal | None:
    """What the pressed winners that settled could withhold at no cost in reward: each
    one's margin beyond its upper cap. Unknown with an unknown cap, or with no reward rows
    at all (surplus mode)."""
    total = Decimal(0)
    for solver, m in margins.items():
        row = rewards.get(solver)
        if row is None or row.upper is None:
            return None
        if row.observed == row.competition:
            total += max(Decimal(0), Decimal(m) - row.upper)
    return total


def check(result: AuctionCounterfactual, auction: Auction, bids: list[Bid],
          removed: frozenset[str], max_winners: int) -> None:
    truth = expected(auction, bids, removed, max_winners)
    assert result.reference_margins_base == truth.margins_base
    assert result.reference_margins_loo == truth.margins_loo
    assert result.runner_up_uids == truth.runner_up_uids
    assert result.pressed_volume_base == truth.volume
    deltas = [truth.margins_loo[s] - m for s, m in truth.margins_base.items()]
    assert result.delta_reference_margin == sum(deltas)
    assert result.reference_margin_rises == sum(1 for d in deltas if d > 0)
    assert result.reference_margin_falls == sum(1 for d in deltas if d < 0)
    # A margin moves only with the winning score or a reference score.
    if any(deltas):
        assert result.anything_moved
    # A removed solver that won nothing and un-filtered nothing leaves the winners alone.
    if not result.solver_won_baseline and not result.un_filtered_uids:
        assert result.baseline_winner_uids == result.loo_winner_uids


def check_above_cap(result: AuctionCounterfactual,
                    rewards_base: Mapping[str, oracle.RewardRow],
                    rewards_loo: Mapping[str, oracle.RewardRow], excluded: bool) -> None:
    """Against the oracle's reward rows, which are this auction's only where the oracle saw
    the same filter inputs; the margins themselves are checked by `check`."""
    above_base = above_cap(result.reference_margins_base, rewards_base)
    above_loo = above_cap(result.reference_margins_loo, rewards_loo)
    assert result.margin_above_cap_base == above_base
    assert result.margin_above_cap_loo == above_loo
    assert result.delta_margin_above_cap == (
        None if above_base is None or above_loo is None else above_loo - above_base)
    # What the pressed winners are paid, capped: the oracle's own rows for the same solvers.
    paid: list[Decimal | None] = []
    for margins, rewards in ((result.reference_margins_base, rewards_base),
                             (result.reference_margins_loo, rewards_loo)):
        rows = [rewards.get(s) for s in margins]
        paid.append(None if any(r is None or r.capped is None for r in rows)
                    else sum((r.capped for r in rows if r is not None and r.capped is not None),
                             Decimal(0)))
    assert (result.pressed_rewards_capped_base, result.pressed_rewards_capped_loo) == tuple(paid)
    base, loo = paid
    assert result.delta_pressed_rewards_capped == (
        None if base is None or loo is None else loo - base)
    check_value(result, rewards_base, rewards_loo, excluded)


def check_value(result: AuctionCounterfactual,
                rewards_base: Mapping[str, oracle.RewardRow],
                rewards_loo: Mapping[str, oracle.RewardRow], excluded: bool) -> None:
    """The settled winners' Δmargin, from the oracle's rows, defined where both capped
    figures are. It fits them: a settled winner's capped reward is min(margin, cap) and V2
    takes max(0, margin − cap), so V1 + V2 is the settled Δmargin plus the other pressed
    winners' Δcapped reward (their penalty). In an excluded auction every capped reward is
    0, and only V2 is left."""
    v1, v2 = result.delta_pressed_rewards_capped, result.delta_margin_above_cap
    if v1 is None or v2 is None:
        assert result.delta_settled_margin is None
        return
    settled = 0
    split = Decimal(0)
    for sign, margins, rewards in ((-1, result.reference_margins_base, rewards_base),
                                   (1, result.reference_margins_loo, rewards_loo)):
        for solver, m in margins.items():
            row = rewards[solver]
            assert row.capped is not None and row.upper is not None
            if row.observed != row.competition:
                split += sign * row.capped
                continue
            settled += sign * m
            above = max(Decimal(0), Decimal(m) - row.upper)
            # A settled winner's uncapped reward is its margin exactly.
            assert row.uncapped == m
            assert row.capped + above == (above if excluded else m)
            split += sign * (above if excluded else m)
    assert result.delta_settled_margin == settled
    assert v1 + v2 == split


def check_report(report: aggregate.Report, payload: dict[str, Any], analysis: Analysis) -> None:
    """The totals round-trip, and every moved margin lies in a `changed` row."""
    assert report.delta_reference_margin == analysis.delta_reference_margin
    assert report.delta_margin_above_cap == analysis.delta_margin_above_cap
    assert report.delta_pressed_rewards_capped == analysis.delta_pressed_rewards_capped
    assert report.delta_settled_margin == analysis.delta_settled_margin
    assert report.auctions_margin_cap_unknown == analysis.auctions_margin_cap_unknown
    assert report.delta_score == analysis.delta_surplus + analysis.delta_protocol_and_partner_fees
    assert report.auctions_runner_up == analysis.auctions_runner_up
    assert report.reference_margin_volume == analysis.reference_margin_volume
    rises = falls = auctions = 0
    for row in payload["changed"]:
        base, loo = row["reference_margins_base"], row["reference_margins_loo"]
        assert set(base) == set(loo)
        up = sum(1 for s in base if loo[s] > base[s])
        down = sum(1 for s in base if loo[s] < base[s])
        assert row["delta_reference_margin"] == sum(loo.values()) - sum(base.values())
        rises, falls, auctions = rises + up, falls + down, auctions + bool(up or down)
    assert (report.reference_margin_rises, report.reference_margin_falls) == (rises, falls)
    assert report.auctions_reference_margin_moved == auctions
    assert sum(m.delta_reference_margin for m in report.moves) == report.delta_reference_margin
    if report.delta_pressed_rewards_capped is not None:
        assert sum((m.delta_pressed_rewards_capped for m in report.moves
                    if m.delta_pressed_rewards_capped is not None),
                   Decimal(0)) == report.delta_pressed_rewards_capped
    if report.delta_margin_above_cap is not None:
        assert sum((m.delta_margin_above_cap for m in report.moves
                    if m.delta_margin_above_cap is not None),
                   Decimal(0)) == report.delta_margin_above_cap
    if report.delta_settled_margin is not None:
        assert sum(v for m in report.moves
                   if (v := m.delta_settled_margin) is not None) == report.delta_settled_margin
    # Δscore per auction carries the fee leg only where every fee is priced.
    for row in payload["changed"]:
        fees = row["delta_protocol_and_partner_fees"] if row["fees_complete"] else 0
        assert row["delta_score"] == row["delta_surplus"] + fees


def check_rows(table: aggregate.Table, windows: list[aggregate.SolverWindow]) -> None:
    rows = dict(table.rows)
    labels = [label for label, _ in table.rows]
    # The block closes the table, right after the filter rows.
    assert labels[-9:] == [
        "  filter verdicts resting on surplus, in those auctions",
        "runner-up to another winner",
        "Δother winners' rewards (capped)",
        "Δother winners' margin over their reference",
        "  winners that settled",
        "  above the reward cap, winners that settled",
        "  consistency rewards paid ÷ margin of winners that settled",
        "  over their winning solutions' volume",
        "  largest single auction of those",
    ]
    for column, window in enumerate(windows):
        head = window.headline
        cell = {label: cells[column] for label, cells in rows.items()}
        bid = head.auctions_with_solver
        share = f"{head.auctions_runner_up / bid:.1%}" if bid else "n/a"
        assert cell["runner-up to another winner"] == (
            f"{head.auctions_runner_up:,} auctions ({share} of the {bid:,} the solver bid in)")
        moved = head.reference_margin_rises + head.reference_margin_falls
        if moved:
            assert cell["Δother winners' margin over their reference"] == (
                f"{oracle.signed_eth_text(head.delta_reference_margin)} ETH over {moved:,} "
                f"winners in {head.auctions_reference_margin_moved:,} auctions "
                f"({head.reference_margin_rises:,} rose, {head.reference_margin_falls:,} fell)")
        else:
            assert cell["Δother winners' margin over their reference"] == (
                "no other winner's margin moved")
        unknown = head.auctions_margin_cap_unknown
        left_out = f"; {unknown:,} auctions with an unknown cap left out" if unknown else ""
        pressed = head.delta_pressed_rewards_capped
        if pressed is None:
            assert cell["Δother winners' rewards (capped)"] == "not computed (surplus mode)"
        else:
            assert cell["Δother winners' rewards (capped)"] == (
                oracle.signed_eth_text(pressed) + " ETH" + left_out)
        above = head.delta_margin_above_cap
        if above is None:
            assert cell["  above the reward cap, winners that settled"] == (
                "not computed (surplus mode)")
        else:
            unknown = head.auctions_margin_cap_unknown
            assert cell["  above the reward cap, winners that settled"] == (
                oracle.signed_eth_text(above) + " ETH"
                + (f"; {unknown:,} auctions with an unknown cap left out" if unknown else ""))
        # The settled winners' margin, uncapped, and consistency paid over it, native.
        settled = head.delta_settled_margin
        settled_cell = cell["  winners that settled"]
        ratio_cell = cell["  consistency rewards paid ÷ margin of winners that settled"]
        paid = head.paid
        if settled is None:
            assert settled_cell == "not computed (surplus mode)"
            assert ratio_cell == "n/a"
        else:
            assert settled_cell == oracle.signed_eth_text(settled) + " ETH" + left_out
            missing = paid.unaccounted["consistency"] if paid is not None else 0
            if paid is None:
                assert ratio_cell == "n/a"
            elif missing and missing >= paid.auctions:
                assert ratio_cell == "pending"
            else:
                ratio = (f"{paid.amount('consistency') / settled:.2f}"
                         if settled > 0 else "n/a")
                assert ratio_cell == ratio + (
                    "; consistency paid incomplete" if missing else "")
        volume = head.reference_margin_volume
        if volume:
            value = Decimal(head.delta_reference_margin) * 10_000 / Decimal(volume)
            assert cell["  over their winning solutions' volume"] == (
                f"{'+' if value > 0 else ''}{value:.2f} bps of "
                f"{oracle.eth_text(volume, 2)} ETH ({head.mode}, not a price change)")
        else:
            assert cell["  over their winning solutions' volume"] == "n/a"
        deltas = {m.auction_id: m.delta_reference_margin for m in head.moves
                  if m.delta_reference_margin}
        whale = cell["  largest single auction of those"]
        if deltas:
            largest = max(deltas, key=lambda a: abs(deltas[a]))
            assert whale.startswith(oracle.signed_eth_text(deltas[largest]) + " ETH")
            assert whale.endswith(f"(auction {largest})")
        else:
            assert whale == "n/a"
