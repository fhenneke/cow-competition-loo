"""Read-only analytics DB access.

`ANALYTICS_DB_URL` is `user:password@host:port`: no scheme, no dbname.
"""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import psycopg2
import psycopg2.errors
import psycopg2.extras

Connection = psycopg2.extensions.connection
Row = dict[str, Any]


def load_env() -> None:
    """Load the nearest `.env` above this package; never overrides the environment."""
    start = Path(__file__).resolve().parent
    for directory in (start, *start.parents):
        path = directory / ".env"
        if path.is_file():
            break
    else:
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


# One database per network, models in schema `dbt`.
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
    "sepolia": "prod_sepolia",
}


def database_name(network: str) -> str:
    try:
        return NETWORK_DATABASES[network]
    except KeyError:
        known = ", ".join(sorted(NETWORK_DATABASES))
        raise KeyError(f"unknown network {network!r}; known networks: {known}") from None


def raw_schema(network: str) -> str:
    """`raw_prod_<network>` with `-` as `_`: the backend tables, unmodelled."""
    database_name(network)
    return "raw_prod_" + network.replace("-", "_")


STATEMENT_TIMEOUT = "30min"


class ConnectionLostError(RuntimeError):
    """The connection died mid-query: the network, never the data. Rerun unchanged."""


class StatementTimeoutError(RuntimeError):
    """A statement ran past `STATEMENT_TIMEOUT` and the server cancelled it."""


def connect(network: str = "mainnet", *, connect_timeout: int = 20) -> Connection:
    load_env()
    url = os.environ.get("ANALYTICS_DB_URL")
    if not url:
        raise RuntimeError("ANALYTICS_DB_URL is not set (expected in .env)")

    credentials, hostport = url.split("@")
    user, password = credentials.split(":", 1)
    host, port = hostport.split(":")

    # Timezone pinned: the quote side compares naive timestamps against ClickHouse UTC.
    conn = psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=database_name(network),
        connect_timeout=connect_timeout,
        options=f"-c timezone=UTC -c statement_timeout={STATEMENT_TIMEOUT}",
        # psycopg2 has no read timeout; keepalives make a dead peer raise in ~2 min
        # instead of hanging forever. tcp_user_timeout is Linux-only.
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
    """Rename the `OperationalError` psycopg2 raises for both a lost connection and a
    statement timeout."""
    try:
        yield
    except psycopg2.Error as error:
        # `conn.closed` is the reliable test; libpq's message varies by OS and SSL.
        if conn.closed:
            dbname = psycopg2.extensions.parse_dsn(conn.dsn).get("dbname")
            raise ConnectionLostError(
                f"lost the connection to the analytics DB ({dbname}) during a query: "
                f"{str(error).strip()}. This is the network, not the data — the "
                f"machine slept, the network changed, or the server dropped the "
                f"session — and the run can be repeated as it was. Keep the machine "
                f"awake for long runs: caffeinate -i uv run loo ..."
            ) from error
        timed_out = "statement timeout" in str(error)
        if isinstance(error, psycopg2.errors.QueryCanceled) and timed_out:
            raise StatementTimeoutError(
                f"a statement ran past the {STATEMENT_TIMEOUT} statement_timeout and the "
                f"server cancelled it. No query here needs that long: the DB is "
                f"overloaded, the statement waited on a lock, or its plan went "
                f"pathological (a sequential scan of proposed_solutions is the known "
                f"case) — EXPLAIN it before rerunning: "
                f"{' '.join(sql.split())[:300]} ..."
            ) from error
        raise


def fetch(conn: Connection, sql: str, params: Any = None) -> list[Row]:
    with (
        _named_failures(conn, sql),
        conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur,
    ):
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def as_int(value: Any) -> int:
    """`numeric` → `int`; a fractional value means a wrong column, so it raises."""
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError(f"expected an integral numeric, got {value}")
        return int(value)
    raise TypeError(f"cannot convert {type(value).__name__} to int: {value!r}")


T = TypeVar("T")


def chunked(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
