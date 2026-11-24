"""The simulation with a record that contradicts the mechanism: a score the fees cannot
add up to, a filter verdict no valid split explains, a stream that forwarded something
else. The validation gates exist to catch exactly these, so each must be named."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from sim.harness import simulate
from sim.simdb import Faults
from sim.world import World, generate

from loo import extract


def validate_args(world: World, out: Path, start: str | None = None) -> list[str]:
    return ["validate", "--network", world.network, "--start", start or world.start,
            "--end", world.end, "--max-winners", str(world.max_winners), "--out", str(out)]


def test_scores_the_fees_cannot_reach(seed: int, tmp_path: Path) -> None:
    """A recorded score one wei off a fully priced multi-pair bid, or below a single-pair
    bid's own surplus, fails its anchor: counted, never trusted."""
    world = generate(seed)
    offsets: dict[tuple[int, int], int] = {}
    for auction in world.auctions:
        if auction.fast_path:
            continue
        for s in auction.solutions:
            priced = all(x.partner_native is not None for x in s.order_scores.values())
            if len(s.pair_scores) > 1 and not s.filtered_out and priced:
                offsets[(auction.auction_id, s.uid)] = 1
            elif s.filtered_out and len(s.pair_scores) > 2:
                covered = {t.uid for x in auction.solutions if not x.filtered_out
                           for t in x.trades}
                dark = {(t.sell_token, t.buy_token) for t in s.trades if t.uid not in covered
                        or s.order_scores[t.uid].partner_native is None}
                if len(dark) >= 2:
                    floor = sum(v for p, v in s.pair_scores.items() if p not in dark) + sum(
                        s.order_scores[t.uid].surplus_native for t in s.trades
                        if (t.sell_token, t.buy_token) in dark)
                    if floor > 0:
                        offsets[(auction.auction_id, s.uid)] = floor - s.score - 1
            elif len(s.pair_scores) == 1:
                surplus = sum(x.surplus_native for x in s.order_scores.values())
                if surplus > 0:
                    offsets[(auction.auction_id, s.uid)] = surplus - s.score - 1
    with simulate(world, Faults(score_offsets=offsets)) as sim:
        sim.cli(*validate_args(world, tmp_path / "v.out"))
    payload = json.loads((tmp_path / "v.out").read_text())
    assert payload["score_anchor_failures"] == len(offsets)


def test_a_verdict_no_split_explains(seed: int, tmp_path: Path) -> None:
    """A single-pair bid is exempt from the filter, so a record that filtered one out is
    a contradiction on the score scale: the gate is not met and the auction is named."""
    world = generate(seed)
    singles = [(a.auction_id, s.uid) for a in world.auctions if not a.fast_path
               for s in a.solutions if len(s.pair_scores) == 1 and not s.filtered_out
               and not s.is_winner]
    if not singles:
        return
    target = random.Random(seed).choice(singles)
    with simulate(world, Faults(flip_filtered=frozenset({target}))) as sim:
        result = sim.cli(*validate_args(world, tmp_path / "v.out"))
    assert result.code == 2
    payload = json.loads((tmp_path / "v.out").read_text())
    assert [target[0], "filter-decision-no-valid-split-explains"] in payload["unexplained"]
    (mismatch,) = [m for m in payload["mismatched"] if m["auction_id"] == target[0]]
    causes: dict[str, int] = {}
    for check in mismatch["checks"]:
        if check["filter_cause"]:
            causes[check["filter_cause"]] = causes.get(check["filter_cause"], 0) + 1
    assert mismatch["filter_causes"] == causes and causes.get("bug", 0) >= 1
    assert payload["filter_causes"].get("bug", 0) >= 1


def test_a_stream_that_forwarded_something_else(seed: int) -> None:
    """When the recorded forwards are not what a replay of the ladder sends, the stream
    replay reports it rather than passing."""
    world = generate(seed)
    if world.chain_id is None:
        return
    candidates = [r for r in world.requests if r.winner is not None and len(r.forwarded) >= 3]
    if not candidates:
        return
    request = random.Random(seed).choice(candidates)
    taken = {o.quote.quote_id for o in world.orders.values() if o.quote is not None}
    # The last forward missing: what was recorded is a prefix of the replay, and a
    # finished stream must have recorded all of it.
    drop = request.forwarded[-1]
    if drop.quote_id in taken:
        return
    request.forwarded = [e for e in request.forwarded if e is not drop]
    from loo import ch, quotes

    with simulate(world) as sim:
        conn = sim.conn()
        ids = extract.auctions_in_window(conn, world.start, world.end)
        window = quotes.load_quote_window(ch.Client.from_env(), conn, world.network,
                                          world.start, world.end, auction_ids=ids)
    replayed = [m for m in window.matches
                if m.request.request_id == request.request_id
                and m.order.order_uid not in window.price_suspects]
    v = window.validation
    if replayed and replayed[0].stream is not None and len(replayed[0].stream.forwarded) >= 2:
        assert v.streams - v.streams_reproduced == 1
        assert v.stream_disagreements[0][0] == request.request_id
        return


def test_windows_and_unpriced_auctions(seed: int, tmp_path: Path) -> None:
    """The current mechanism starts on its first day, inclusive; NULL price arrays are
    named, one auction or a span of them."""
    world = generate(seed)
    with simulate(world) as sim:
        assert sim.cli(*validate_args(world, tmp_path / "a.out", extract.MECHANISM_START)
                       ).code == 0
        assert sim.cli(*validate_args(world, tmp_path / "b.out", "2026-10-27")).code == 4
    live = sorted(a.auction_id for a in world.auctions if not a.fast_path)
    if len(live) < 2:
        return
    pair = frozenset(live[:2])
    with simulate(world, Faults(unpriced=pair)) as sim, pytest.raises(
        extract.MissingPricesError, match=f"2 auctions in {live[0]}..{live[1]} have bids"
    ):
        sim.cli(*validate_args(world, tmp_path / "c.out"))
    with simulate(world, Faults(unpriced=frozenset(live[:1]))) as sim, pytest.raises(
        extract.MissingPricesError, match=f"auction {live[0]} has bids"
    ):
        sim.cli(*validate_args(world, tmp_path / "d.out"))
