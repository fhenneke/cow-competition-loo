"""Read-only analytics DB access.

`ANALYTICS_DB_URL` is `user:password@host:port`: no scheme, no dbname.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import zlib
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import psycopg2
import psycopg2.errors
import psycopg2.extras

PgConnection = psycopg2.extensions.connection
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
    "solana": "prod_solana",
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


def connect(network: str = "mainnet", *, connect_timeout: int = 20) -> PgConnection:
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
def _named_failures(conn: PgConnection, sql: str) -> Generator[None, None, None]:
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


def _query(conn: PgConnection, sql: str, params: Any) -> list[Row]:
    with (
        _named_failures(conn, sql),
        conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur,
    ):
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def always(rows: list[Row]) -> bool:
    return True


def fetch(
    conn: Connection,
    sql: str,
    params: Any = None,
    *,
    final: Callable[[list[Row]], bool] = always,
) -> list[Row]:
    """`final` says whether the rows can no longer change; a `Snapshot` keeps only those."""
    if isinstance(conn, Snapshot):
        snapshot = conn
        return snapshot.rows(
            (sql, params), lambda: _query(snapshot.connection(), sql, params), final
        )
    return _query(conn, sql, params)


# --- snapshots: a past window's query results, replayed in place of the DB

SETTLED_AFTER = timedelta(days=1)
"""How long the DB takes to hold everything recorded before a moment."""


def utcnow() -> datetime:
    """The clock, naive UTC like the DB session. The simulation pins it."""
    return datetime.now(UTC).replace(tzinfo=None)


def settled(moment: str) -> bool:
    """Whether the DB holds everything recorded before `moment` (UTC unless it says)."""
    when = datetime.fromisoformat(moment)
    if when.tzinfo is not None:
        when = when.astimezone(UTC).replace(tzinfo=None)
    return when + SETTLED_AFTER <= utcnow()


def cache_dir() -> Path:
    """`$LOO_CACHE_DIR`, default `~/.cache/loo`."""
    return Path(os.environ.get("LOO_CACHE_DIR") or Path.home() / ".cache" / "loo")


class Snapshot:
    """One window's query results on disk, read in place of the DB.

    A result is one file, named by a hash of its query text and parameters: a changed
    query or parameter misses, is read from the DB, and is kept beside the old files. A
    result its caller marks as not final is read again on every run and never kept. The
    connection opens on the first miss, so a full replay never connects."""

    def __init__(self, network: str, start: str, end: str) -> None:
        self.network = network
        self.directory = snapshot_dir(network, start, end)
        self.replayed = 0
        self.kept = 0
        self.unsettled = 0
        self._conn: PgConnection | None = None

    def connection(self) -> PgConnection:
        if self._conn is None:
            self._conn = connect(self.network)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def rows(
        self,
        query: Any,
        read: Callable[[], list[Row]],
        final: Callable[[list[Row]], bool],
    ) -> list[Row]:
        """`query` (text and parameters) from disk, else `read()` and keep it if final."""
        digest = hashlib.sha256(repr(query).encode()).hexdigest()[:32]
        path = self.directory / f"{digest}.pickle.z"
        if path.exists():
            self.replayed += 1
            columns, values = pickle.loads(zlib.decompress(path.read_bytes()))
            return [dict(zip(columns, value, strict=True)) for value in values]
        rows = read()
        if not final(rows):
            self.unsettled += 1
            return rows
        # Column names once, not per row: a third of the size and of the load time.
        columns = tuple(rows[0]) if rows else ()
        values = [tuple(row[column] for column in columns) for row in rows]
        blob = zlib.compress(pickle.dumps((columns, values), pickle.HIGHEST_PROTOCOL))
        self.directory.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(f".{os.getpid()}.tmp")
        partial.write_bytes(blob)
        partial.replace(path)
        self.kept += 1
        return rows

    def summary(self) -> str:
        return (
            f"snapshot {self.directory}: {self.replayed} queries replayed, "
            f"{self.kept} read and kept, {self.unsettled} read but not yet final"
        )


def snapshot_dir(network: str, start: str, end: str) -> Path:
    return cache_dir() / "snapshots" / network / f"{start}..{end}"


Connection = PgConnection | Snapshot
"""What the loaders read from: the DB, or a window's snapshot of it."""


def network_of(conn: Connection) -> str:
    """The network whose database `conn` reads: which tables and SQL it needs."""
    if isinstance(conn, Snapshot):
        return conn.network
    dbname = psycopg2.extensions.parse_dsn(conn.dsn).get("dbname")
    for network, name in NETWORK_DATABASES.items():
        if name == dbname:
            return network
    raise KeyError(f"connection to {dbname!r} is to no known network's database")


def as_int(value: Any) -> int:
    """`numeric` → `int`; a fractional value means a wrong column, so it raises."""
    if isinstance(value, Decimal):  # the common case, so first
        try:
            integral = int(value)  # truncates; NaN raises here, Infinity OverflowError
        except ValueError:
            raise ValueError(f"expected an integral numeric, got {value}") from None
        if integral != value:
            raise ValueError(f"expected an integral numeric, got {value}")
        return integral
    if isinstance(value, int):
        return value
    raise TypeError(f"cannot convert {type(value).__name__} to int: {value!r}")


T = TypeVar("T")


def chunked(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
