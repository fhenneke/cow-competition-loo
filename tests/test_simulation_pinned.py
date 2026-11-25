"""Pinned worlds: real-data fixtures and the shapes behind past defects, each run through
every layer a generated seed goes through, plus the one fact it exists to pin."""

from __future__ import annotations

import random
from pathlib import Path

from sim import pinned
from sim.harness import simulate
from sim.world import World
from test_simulation import exercise, loaded

from loo import extract
from loo.counterfactual import (
    Analysis,
    AuctionCounterfactual,
    analyse_auction,
    price_imbalanced,
    price_suspects,
)
from loo.valuation import build_solutions
from loo.winner_selection import arbitrate, compute_reference_scores


def bundles_of(world: World) -> list[extract.AuctionBundle]:
    with simulate(world) as sim:
        bundles, _, _ = loaded(sim)
    return bundles


def test_live_fee_fixture(tmp_path: Path) -> None:
    world = pinned.live_fee_fixture()
    (auction,) = world.auctions
    live = next(s for s in auction.solutions if len(s.trades) == 2)
    scores = live.order_scores
    uid = next(u for u in scores if u.startswith("e1e1"))
    # The oracle reproduces the record; `loo` must reproduce both.
    assert scores[uid].score_native == 1330654764658941815
    assert scores[uid].fee_atoms == 367424346879097137
    (bundle,) = bundles_of(world)
    valued, failures = build_solutions(bundle.bids, bundle.native_prices, world.weth,
                                       fee_context=bundle.fee_context)
    assert not failures
    entry = next(v for v in valued if len(v.bid.orders) == 2)
    v = entry.valuation
    assert v.order_surplus_native[uid] + v.order_fee_native[uid] == 1330654764658941815
    assert 0 < v.order_partner_fee_native[uid] < v.order_fee_native[uid]
    assert not entry.solution.score_anchor_failed
    assert entry.solution.pair_scores[(pinned.USDC, "ee" * 20)] == (
        1330654764658941815)
    exercise(world, tmp_path, random.Random(0), ["Quasi"])


def test_dark_pair(tmp_path: Path) -> None:
    world = pinned.dark_pair()
    (bundle,) = bundles_of(world)
    valued, _ = build_solutions(bundle.bids, bundle.native_prices, world.weth,
                                fee_context=bundle.fee_context)
    ranking = arbitrate([v.solution for v in valued])
    batch = next(v.solution for v in valued if len(v.solution.pair_values) == 3)
    assert set(batch.pair_scores) == {(world.weth, pinned.USDC)}
    assert batch.solution_uid in {s.solution_uid for s in ranking.filtered_out}
    assert not ranking.proxied_uids
    exercise(world, tmp_path, random.Random(0), ["Arc"])


def test_ranked_order(tmp_path: Path) -> None:
    world = pinned.ranked_order()
    (bundle,) = bundles_of(world)
    valued, _ = build_solutions(bundle.bids, bundle.native_prices, world.weth,
                                fee_context=bundle.fee_context)
    ranking = arbitrate([v.solution for v in valued])
    assert [s.total for s in ranking.ranked] == [100, 80, 90]
    assert compute_reference_scores(ranking)["c1" * 20] == 80
    exercise(world, tmp_path, random.Random(0), ["Sector", "Horadrim,Quasi"])


def test_blocked_batch(tmp_path: Path) -> None:
    world = pinned.blocked_batch()
    (bundle,) = bundles_of(world)
    result = analyse_auction(bundle, world.weth, frozenset({"d1" * 20}),
                             settled={0: extract.Settlement(True, True)})
    assert sum(d.only_without_solver for d in result.order_diffs) == 2
    assert sum(d.only_with_solver for d in result.order_diffs) == 1
    exercise(world, tmp_path, random.Random(0), ["Gnosis_0x"])


def test_penalty_caps(tmp_path: Path) -> None:
    world = pinned.penalty_caps()
    (auction,) = world.auctions
    zero = next(s for s in auction.solutions if s.score == 0)
    assert zero.is_winner and not zero.filtered_out
    floors = {solver: row.lower for solver, row in auction.mart.items()}
    assert floors["e1" * 20] == 0
    assert floors["e2" * 20] == -(10**16)
    exercise(world, tmp_path, random.Random(0), ["Baseline", "Sector,Quasi"])


def test_split_slot(tmp_path: Path) -> None:
    world = pinned.split_slot()
    (bundle,) = bundles_of(world)
    (auction,) = world.auctions
    caps = {u: extract.SolutionCap(c.upper, auction.flat_lower, False, c.penalty)
            for u, c in auction.caps.items()}
    result = analyse_auction(bundle, world.weth, frozenset({"f1" * 20}),
                             settled={0: extract.Settlement(True, True)}, solution_caps=caps)
    assert len(result.loo_winner_uids) == 2 and result.cap_double_inherited == 1
    exercise(world, tmp_path, random.Random(0), ["Sector"])


def test_cancelled_price(tmp_path: Path) -> None:
    world = pinned.cancelled_price()
    cow, subsidy = sorted(bundles_of(world), key=lambda b: b.auction_id, reverse=True)
    # Summed per solution, the wrong price cancels and the cross-subsidy balances.
    assert not any(price_imbalanced(b, cow.native_prices) for b in cow.bids)
    assert not any(price_imbalanced(b, subsidy.native_prices) for b in subsidy.bids)
    assert price_suspects(cow.bids, cow.native_prices) == {b.uid for b in cow.bids}
    assert not price_suspects(subsidy.bids, subsidy.native_prices)
    exercise(world, tmp_path, random.Random(0), ["Sector", "Quasi"])


def removed_once(world: World, address: str) -> dict[int, AuctionCounterfactual]:
    """Every auction with `address` removed; margins do not depend on settlement."""
    return {
        bundle.auction_id: analyse_auction(bundle, world.weth, frozenset({address}),
                                           outcome_rule="assume-settled")
        for bundle in bundles_of(world)
    }


def uid_of(world: World, auction_id: int, address: str, score: int) -> int:
    auction = next(a for a in world.auctions if a.auction_id == auction_id)
    return next(s.uid for s in auction.solutions if s.solver == address and s.score == score)


def test_runner_up_only(tmp_path: Path) -> None:
    """A solver that is only ever runner-up: the winners stay, their margins rise."""
    world = pinned.runner_up_only()
    sector, quasi, baseline = "a7" * 20, "b7" * 20, "c7" * 20
    results = removed_once(world, sector)
    first, second = results[14000011], results[14000012]
    for result in (first, second):
        assert not result.winner_set_changed and not result.solver_won_baseline
    assert (first.reference_margins_base, first.reference_margins_loo) == (
        {quasi: 10}, {quasi: 50})
    assert first.runner_up_uids == {uid_of(world, 14000011, sector, 90)}
    assert first.pressed_volume_base == 10**9 + 100
    assert (second.reference_margins_base, second.reference_margins_loo) == (
        {quasi: 100, baseline: 10}, {quasi: 100, baseline: 60})
    assert second.runner_up_uids == {uid_of(world, 14000012, sector, 70)}
    assert second.pressed_volume_base == 10**9 + 80
    analysis = Analysis(solver="Sector", addresses=frozenset({sector}))
    for result in results.values():
        analysis.add(result)
    assert (analysis.auctions_runner_up, analysis.delta_reference_margin) == (2, 90)
    assert (analysis.reference_margin_rises, analysis.reference_margin_falls) == (2, 0)
    assert analysis.auctions_reference_margin_moved == 2
    exercise(world, tmp_path, random.Random(0), ["Sector"])


def test_runner_up_elsewhere(tmp_path: Path) -> None:
    """Sector wins X and Quasi's reference holds that win: the reference moves, the
    margin does not, and Sector is no runner-up. Bidding Y too, it is one by uid."""
    world = pinned.runner_up_elsewhere()
    sector, quasi = "a8" * 20, "b8" * 20
    results = removed_once(world, sector)
    plain, both = results[14000021], results[14000022]
    assert plain.baseline_reference_scores[quasi] == 130
    assert plain.loo_reference_scores[quasi] == 90
    assert (plain.reference_margins_base, plain.reference_margins_loo) == (
        {quasi: 50}, {quasi: 50})
    assert plain.runner_up_uids == frozenset() and plain.delta_reference_margin == 0
    assert (both.reference_margins_base, both.reference_margins_loo) == (
        {quasi: 10}, {quasi: 50})
    assert both.runner_up_uids == {uid_of(world, 14000022, sector, 70)}
    assert both.solver_won_baseline
    exercise(world, tmp_path, random.Random(0), ["Sector"])


def test_margin_falls(tmp_path: Path) -> None:
    """Removal lowers a margin: once by un-filtering a rival batch, once by vacating a
    pair a blocked batch needed."""
    world = pinned.margin_falls()
    sector, quasi, arc = "a9" * 20, "b9" * 20, "e9" * 20
    results = removed_once(world, sector)
    unfiltered, claimed = results[14000031], results[14000032]
    assert (unfiltered.reference_margins_base, unfiltered.reference_margins_loo) == (
        {quasi: 40}, {quasi: 25})
    assert unfiltered.un_filtered_uids == {uid_of(world, 14000031, arc, 75)}
    assert unfiltered.runner_up_uids == {uid_of(world, 14000031, sector, 40)}
    assert (claimed.reference_margins_base, claimed.reference_margins_loo) == (
        {quasi: 35}, {quasi: 10})
    assert not claimed.un_filtered_uids and claimed.solver_won_baseline
    assert claimed.runner_up_uids == frozenset()
    for result in (unfiltered, claimed):
        assert (result.reference_margin_rises, result.reference_margin_falls) == (0, 1)
    exercise(world, tmp_path, random.Random(0), ["Sector"])
