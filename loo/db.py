"""Analytics DB connection.

`ANALYTICS_DB_URL` in `.env` is `user:password@host:port` — no scheme and no database
name, so `psycopg2.connect(url)` cannot be used directly. See docs/analytics-db.md.

The DB user is read-only and should stay that way; nothing here writes.
"""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, TypeVar

import pandas as pd
import psycopg2
import psycopg2.errors
import psycopg2.extras
from dotenv import load_dotenv

SCHEMA = "dbt"

Connection = psycopg2.extensions.connection
"""The concrete psycopg2 connection type, aliased so callers can annotate against it."""

Row = dict[str, Any]
"""One result row as `fetch` returns it. Values are `Any`: column types are the query's
business, and every amount is normalised through `as_int` at the point of use."""

# One database per network, all with models in schema `dbt`.
NETWORK_DATABASES = {
    "mainnet": "prod_mainnet",
    "xdai": "prod_xdai",
    "base": "prod_base",
    "arbitrum-one": "prod_arbitrum-one",
    "avalanche": "prod_avalanche",
    "polygon": "prod_polygon",
    "bnb": "prod_bnb",
    "ink": "prod_ink",
    "linea": "prod_linea",
    "plasma": "prod_plasma",
    "lens": "prod_lens",
    "sepolia": "prod_sepolia",
}


def database_name(network: str) -> str:
    try:
        return NETWORK_DATABASES[network]
    except KeyError:
        known = ", ".join(sorted(NETWORK_DATABASES))
        raise KeyError(f"unknown network {network!r}; known networks: {known}") from None


def raw_schema(network: str) -> str:
    """The schema holding the backend's own tables, mirrored unmodelled, for a network:
    `raw_prod_<network>` with `-` as `_` (`raw_prod_arbitrum_one`). Read only where a
    staging model lags its source — `order_quotes.quote_id` is the case (docs/quotes.md)."""
    database_name(network)  # refuse an unknown network by the same message
    return "raw_prod_" + network.replace("-", "_")


STATEMENT_TIMEOUT = "30min"
"""Server-side limit on any one statement, so one that is alive but stuck — behind a
lock, or on a plan gone pathological — fails instead of running on unattended. The
slowest statement measured on the analyse path is the quote side's `ORDERS_SQL`, which
grows with the window: 81 s over 80 mainnet days on 2026-10-22, when every chunked
extraction query took under a second. Twenty times that leaves room for a longer window on a slower day
(CLAUDE.md gotcha 17); the server's own default is no limit at all."""


class ConnectionLostError(RuntimeError):
    """The connection to the analytics DB died during a query.

    A network failure — the machine slept, the network changed, the server dropped the
    session — and never a property of the data, so the run can be repeated as it was.
    psycopg2 raises it as an `OperationalError`, the class a cancelled statement shares,
    so `fetch` and `run` re-raise it under this name."""


class StatementTimeoutError(RuntimeError):
    """A statement ran past `STATEMENT_TIMEOUT` and the server cancelled it."""


def connect(network: str = "mainnet", *, connect_timeout: int = 20) -> Connection:
    """Open a read-only connection to one network's analytics database."""
    load_dotenv()
    url = os.environ.get("ANALYTICS_DB_URL")
    if not url:
        raise RuntimeError("ANALYTICS_DB_URL is not set (expected in .env)")

    credentials, hostport = url.split("@")
    user, password = credentials.split(":", 1)
    host, port = hostport.split(":")

    # Timestamps come back rendered in the session's timezone, and the quote side
    # compares them as naive strings against ClickHouse's UTC clock inside a window of
    # minutes — so the timezone is pinned rather than inherited from the server or a
    # `PGTZ` in the environment, where a change would silently empty every match.
    conn = psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=database_name(network),
        connect_timeout=connect_timeout,
        options=f"-c timezone=UTC -c statement_timeout={STATEMENT_TIMEOUT}",
        # A connection that dies under a query must raise, not hang. psycopg2 sets no
        # read timeout, so a query whose connection is gone — the machine slept, the
        # network changed — waits forever on a socket this kernel still holds as
        # ESTABLISHED: a `loo analyse` sat 7.5 hours that way on 2026-10-22, with an
        # empty log. Keepalives make the kernel probe a connection that has been silent
        # for 60 s, six probes 10 s apart, so a dead peer raises about two minutes after
        # the line goes quiet. The server's kernel answers the probes however long its
        # query runs, so a slow query is never taken for a dead connection.
        # `tcp_user_timeout` bounds the other case, data sent and never acknowledged, on
        # Linux; macOS has no such socket option and libpq skips it there.
        keepalives=1,
        keepalives_idle=60,
        keepalives_interval=10,
        keepalives_count=6,
        tcp_user_timeout=120_000,  # milliseconds, unlike the three above
    )
    conn.set_session(readonly=True)
    return conn


@contextmanager
def _named_failures(conn: Connection, sql: str) -> Generator[None, None, None]:
    """Re-raise a lost connection or a statement timeout as what it is, rather than as
    the `OperationalError` psycopg2 raises for both. Anything else passes unchanged."""
    try:
        yield
    except psycopg2.Error as error:
        # psycopg2 marks the connection closed once libpq reports it broken. That is the
        # test to use: the message varies with the OS and with SSL ("SSL SYSCALL error:
        # Operation timed out", "server closed the connection unexpectedly", ...).
        if conn.closed:
            dbname = psycopg2.extensions.parse_dsn(conn.dsn).get("dbname")
            raise ConnectionLostError(
                f"lost the connection to the analytics DB ({dbname}) during a query: "
                f"{str(error).strip()}. This is the network, not the data — the "
                f"machine slept, the network changed, or the server dropped the "
                f"session — and the run can be repeated as it was. Keep the machine "
                f"awake for long runs: caffeinate -i uv run loo ..."
            ) from error
        # `QueryCanceled` also covers a cancel on request; only the timeout is renamed.
        timed_out = "statement timeout" in str(error)
        if isinstance(error, psycopg2.errors.QueryCanceled) and timed_out:
            raise StatementTimeoutError(
                f"a statement ran past the {STATEMENT_TIMEOUT} statement_timeout and the "
                f"server cancelled it. No query here needs that long: the DB is "
                f"overloaded, the statement waited on a lock, or its plan went "
                f"pathological (a sequential scan of proposed_solutions is the known "
                f"case, CLAUDE.md gotcha 16) — EXPLAIN it before rerunning: "
                f"{' '.join(sql.split())[:300]} ..."
            ) from error
        raise


def fetch(conn: Connection, sql: str, params: Any = None) -> list[Row]:
    """Run a query and return rows as dicts. Preferred over `run` on the extraction
    path, where amounts must stay exact Python ints rather than become numpy dtypes."""
    with (
        _named_failures(conn, sql),
        conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur,
    ):
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def run(conn: Connection, sql: str, params: Any = None) -> pd.DataFrame:
    """Run a query and return a DataFrame. For ad-hoc work and notebooks."""
    with _named_failures(conn, sql), conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            raise ValueError("query produced no result set")
        columns = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)


def as_int(value: Any) -> int:
    """Convert a `numeric` column to `int`, refusing to silently truncate.

    Every amount in these tables is an integer stored as `numeric`; a fractional value
    would mean a wrong column, not a rounding decision to make quietly.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError(f"expected an integral numeric, got {value}")
        return int(value)
    raise TypeError(f"cannot convert {type(value).__name__} to int: {value!r}")


T = TypeVar("T")


def chunked(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Split a sequence into chunks, to keep `= any(%s)` parameter lists sane."""
    for start in range(0, len(items), size):
        yield items[start : start + size]
