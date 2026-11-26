"""Wiring: point `loo` at a simulated world and run it the way `loo` runs.

Only the two process boundaries are replaced — `loo.db.connect` hands out a simulated
connection and `urllib.request.urlopen` reaches a simulated ClickHouse — so everything
between them, `loo.db.fetch` and `loo.ch.Client.fetch` included, is the real code.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import urllib.request
from collections.abc import Generator
from dataclasses import dataclass

import pytest

from loo import cli, db
from loo.db import Connection

from .simch import SimClickHouse
from .simdb import Faults, SimServer, connection
from .world import World


@dataclass
class Run:
    code: int
    out: str
    err: str


@dataclass
class Sim:
    world: World
    server: SimServer
    clickhouse: SimClickHouse
    connections: list[Connection]

    def conn(self) -> Connection:
        made = connection(self.server)
        self.connections.append(made)
        return made

    def cli(self, *argv: str) -> Run:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return Run(code, out.getvalue(), err.getvalue())

    def window_args(self) -> list[str]:
        return ["--network", self.world.network, "--start", self.world.start,
                "--end", self.world.end]


@contextlib.contextmanager
def simulate(
    world: World,
    faults: Faults | None = None,
    *,
    shuffle_seed: int | None = None,
    clickhouse: SimClickHouse | None = None,
) -> Generator[Sim]:
    server = SimServer(world, faults or Faults(),
                       shuffle_seed=world.seed if shuffle_seed is None else shuffle_seed)
    house = clickhouse or SimClickHouse(world)
    sim = Sim(world, server, house, [])

    def connect(network: str = "mainnet", *, connect_timeout: int = 20) -> Connection:
        assert network == world.network, f"connected to {network}, the world is {world.network}"
        return sim.conn()

    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory() as cache:
        # A cache of its own: the simulated rates must neither read nor fill the real one.
        patch.setenv("LOO_CACHE_DIR", cache)
        patch.setattr(db, "connect", connect)
        patch.setattr(urllib.request, "urlopen", house.urlopen)
        patch.setenv("CLICKHOUSE_URL", ' "http://sim.clickhouse:8123" ')
        patch.setenv("CLICKHOUSE_USER", "sim-reader")
        patch.setenv("CLICKHOUSE_PASSWORD", "sim-secret")
        patch.setenv("ANALYTICS_DB_URL", "sim:sim@sim:5432")
        yield sim
