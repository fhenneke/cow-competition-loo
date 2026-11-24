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
from loo.counterfactual import analyse_auction
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
