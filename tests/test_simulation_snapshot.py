"""Snapshots: a past window's analysis read from disk instead of the DB. The first run
fills the snapshot, the second reads it and asks neither database anything beyond the
results the first could not keep because they were not final yet, and both write the
reports a run without a snapshot writes. A window too recent to have settled is read live
and leaves no snapshot."""

from __future__ import annotations

import os
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

from sim.harness import SETTLED, Run, Sim, simulate
from sim.world import World, generate

from loo import db, extract, payouts

SETTLING = {
    extract.WINDOW_SQL,
    extract.CONVERSION_RATES_SQL,
    payouts.PERFORMANCE_SQL,
    payouts.CONSISTENCY_MART_SQL,
    payouts.QUOTE_MART_SQL,
}
"""The queries whose results can still change after the window has settled."""


def analyse(sim: Sim, world: World, out: Path, solver: str) -> tuple[Run, dict[str, str]]:
    out.mkdir()
    args = ["analyse", *sim.window_args(), "--max-winners", str(world.max_winners),
            "--solver", solver, "--out", str(out / "{solver}.{rule}.json")]
    if world.chain_id is not None:
        args.append("--quotes")
    result = sim.cli(*args)
    assert result.code in (0, 4), result.err
    return result, {path.name: path.read_text() for path in sorted(out.iterdir())}


def not_final(err: str) -> int:
    found = re.search(r"(\d+) read but not yet final", err)
    assert found, err
    return int(found.group(1))


def some_solver(world: World, rng: random.Random) -> str:
    return rng.choice(sorted({world.solver_of(a).label for a in world.bidding_addresses()}))


def test_snapshot_replays_the_window(seed: int, tmp_path: Path) -> None:
    """The second run of a settled window reads only what the first could not keep, opens
    no connection when that is nothing, and writes the same reports and output; both
    match a run read live."""
    world = generate(seed)
    rng = random.Random(seed)
    solver = some_solver(world, rng)
    end = datetime.fromisoformat(world.end)
    # Long after the window, or just past settling, when its last accounting period may
    # still be open: then its auctions and payouts are read again on every run.
    now = rng.choice((SETTLED, end + db.SETTLED_AFTER + timedelta(hours=rng.randint(0, 72))))
    with simulate(world, now=end) as sim:
        _, live = analyse(sim, world, tmp_path / "live", solver)
    with simulate(world, now=now) as sim:
        first, filled = analyse(sim, world, tmp_path / "first", solver)
        asked, sent = len(sim.server.queries), len(sim.clickhouse.queries)
        connections = len(sim.connections)
        second, replayed = analyse(sim, world, tmp_path / "second", solver)
        again = sim.server.queries[asked:]
        assert len(sim.clickhouse.queries) == sent
    assert first.out == second.out
    assert filled == replayed == live
    assert len(again) == not_final(first.err) == not_final(second.err)
    assert set(again) <= SETTLING
    if not again:
        assert len(sim.connections) == connections


def test_recent_window_is_read_live(seed: int, tmp_path: Path) -> None:
    """A window that ended less than `SETTLED_AFTER` ago is read from the DB on every run
    and nothing is written to the snapshot directory."""
    world = generate(seed)
    solver = some_solver(world, random.Random(seed))
    end = datetime.fromisoformat(world.end)
    with simulate(world, now=end + db.SETTLED_AFTER - timedelta(minutes=1)) as sim:
        first, filled = analyse(sim, world, tmp_path / "first", solver)
        asked = len(sim.server.queries)
        second, again = analyse(sim, world, tmp_path / "second", solver)
        assert len(sim.server.queries) == 2 * asked
        assert not (Path(os.environ["LOO_CACHE_DIR"]) / "snapshots").exists()
    assert "no snapshot is kept" in first.err and "no snapshot is kept" in second.err
    assert filled == again
