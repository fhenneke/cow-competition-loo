"""Read-only analytics DB access.

`ANALYTICS_DB_URL` is `user:password@host:port`: no scheme, no dbname.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pickle
import threading
import zlib
from collections import deque
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypeVar

import psycopg2
import psycopg2.errors

PgConnection = psycopg2.extensions.connection
Row = dict[str, Any]
Result = tuple[tuple[str, ...], list[tuple[Any, ...]]]
"""A query's column names once and one tuple per row: what a snapshot file holds."""


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
        if not conn.closed:
            # The failed statement aborted the transaction; end it so the connection
            # serves its next query (a reader's next one, say).
            with contextlib.suppress(psycopg2.Error):
                conn.rollback()
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


def _query(conn: PgConnection, sql: str, params: Any) -> Result:
    # Plain tuples: psycopg2 builds them in C, where a dict cursor runs Python per row.
    with _named_failures(conn, sql), conn.cursor() as cur:
        cur.execute(sql, params)
        values: list[tuple[Any, ...]] = cur.fetchall()
        columns = tuple(column.name for column in cur.description or ())
    return columns, values


def as_rows(result: Result) -> list[Row]:
    columns, values = result
    return [dict(zip(columns, value, strict=True)) for value in values]


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
        return conn.rows((sql, params), conn.reader(sql, params), final)
    return as_rows(_query(conn, sql, params))


def prefetch(
    conn: Connection, queries: Iterable[tuple[str, Any]], *, first: bool = False
) -> None:
    """Start reading `queries` (text and parameters) on parallel connections; `fetch`
    later takes each result instead of asking the DB. Only for queries the caller goes
    on to fetch, in the order it will; `first` puts them ahead of those already waiting.
    Nothing happens on a bare connection."""
    if isinstance(conn, Snapshot):
        conn.prefetch(
            [((sql, params), conn.reader(sql, params)) for sql, params in queries],
            first=first,
        )


# --- reading ahead: the DB answers slowly, but several queries at once

PARALLEL_READS: dict[Literal["postgres", "clickhouse"], int] = {
    "postgres": 6,
    "clickhouse": 8,
}
"""Threads per database reading ahead of the analysis, each on its own connection. The
analytics DB stops gaining at about two queries' worth of work in flight (2026-11-26);
the threads beyond that hide the round trip and the transfer of each result."""

READ_AHEAD = 256
"""Results read but not yet taken before the readers wait: a bound on memory."""


class _Job:
    __slots__ = ("digest", "error", "first", "read", "result", "state")

    def __init__(self, digest: str, read: Callable[[], Any], first: bool) -> None:
        self.digest = digest
        self.read = read
        self.first = first
        self.state: Literal["queued", "running", "done", "taken"] = "queued"
        self.result: Any = None
        self.error: BaseException | None = None


class _Readers:
    """Threads that run queued jobs in submission order, those submitted `first` ahead of
    the rest. The caller takes each job's result once, in any order, and runs a job no
    thread has started itself. Past `READ_AHEAD` results not yet taken, the threads wait,
    except for `first` jobs: those are wanted next."""

    def __init__(self, threads: int) -> None:
        self._size = threads
        self._jobs: dict[str, _Job] = {}
        self._queue: deque[_Job] = deque()
        self._lock = threading.Condition()
        self._threads: list[threading.Thread] = []
        self._ready = 0
        self._running = 0
        self._closed = False

    def submit(self, jobs: Sequence[tuple[str, Callable[[], Any]]], first: bool) -> None:
        with self._lock:
            if self._closed:
                return
            new: list[_Job] = []
            for digest, read in jobs:
                if digest not in self._jobs:
                    job = self._jobs[digest] = _Job(digest, read, first)
                    new.append(job)
            if first:
                self._queue.extendleft(reversed(new))
            else:
                self._queue.extend(new)
            while len(self._threads) < min(self._size, len(self._queue)):
                thread = threading.Thread(target=self._work, daemon=True)
                self._threads.append(thread)
                thread.start()
            self._lock.notify_all()

    def take(self, digest: str) -> tuple[bool, Any]:
        """`(True, result)` for a submitted job, raising what it raised; `(False, None)`
        if it never was."""
        with self._lock:
            job = self._jobs.pop(digest, None)
            if job is None:
                return False, None
            if job.state == "queued":
                job.state = "taken"  # the threads skip it
                run_here = True
            else:
                run_here = False
                while job.state == "running":
                    self._lock.wait()
                self._ready -= 1
                job.state = "taken"
                self._lock.notify_all()
        if run_here:
            return True, job.read()
        if job.error is not None:
            raise job.error
        return True, job.result

    def busy(self) -> bool:
        with self._lock:
            return self._running > 0

    def close(self, timeout: float) -> None:
        with self._lock:
            self._closed = True
            self._queue.clear()
            self._lock.notify_all()
        for thread in self._threads:
            thread.join(timeout)

    def _work(self) -> None:
        while True:
            with self._lock:
                while not self._closed and not (
                    self._queue and (self._queue[0].first or self._ready < READ_AHEAD)
                ):
                    self._lock.wait()
                if self._closed:
                    return
                job = self._queue.popleft()
                if job.state != "queued":
                    continue
                job.state = "running"
                self._running += 1
            try:
                job.result = job.read()
            except BaseException as error:  # handed to whoever takes the job
                job.error = error
            with self._lock:
                job.state = "done"
                self._running -= 1
                self._ready += 1
                self._lock.notify_all()


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


def _encode(result: Result) -> bytes:
    return zlib.compress(pickle.dumps(result, pickle.HIGHEST_PROTOCOL))


def _decode(blob: bytes) -> Result:
    result: Result = pickle.loads(zlib.decompress(blob))
    return result


Pool = Literal["postgres", "clickhouse"]


class Snapshot:
    """One window's query results on disk, read in place of the DB.

    A result is one file, named by a hash of its query text and parameters: a changed
    query or parameter misses, is read from the DB, and is kept beside the old files. A
    result its caller marks as not final is read again on every run and never kept. A
    window too recent to settle is read with `keep=False`: from the DB on every run, and
    nothing written. Connections open on the first miss, so a full replay never connects.

    Queries announced with `prefetch` are read ahead on parallel connections while the
    caller works through earlier results; `rows` takes them as they come."""

    def __init__(self, network: str, start: str, end: str, *, keep: bool = True) -> None:
        self.network = network
        self.directory = snapshot_dir(network, start, end)
        self.keep = keep
        self.replayed = 0
        self.kept = 0
        self.unsettled = 0
        self._local = threading.local()
        self._opened: list[PgConnection] = []
        self._lock = threading.Lock()
        self._readers: dict[Pool, _Readers] = {}

    def connection(self) -> PgConnection:
        """The calling thread's own connection, opened on its first query."""
        conn: PgConnection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = connect(self.network)
            with self._lock:
                self._opened.append(conn)
        return conn

    def reader(self, sql: str, params: Any) -> Callable[[], Result]:
        return lambda: _query(self.connection(), sql, params)

    def close(self) -> None:
        """Stop reading ahead, cancelling what still runs, and close every connection."""
        postgres = self._readers.get("postgres")
        if postgres is not None and postgres.busy():
            for conn in self._opened:
                cancel = getattr(conn, "cancel", None)
                if cancel is not None and not conn.closed:
                    with contextlib.suppress(psycopg2.Error):
                        cancel()
        for readers in self._readers.values():
            readers.close(timeout=30)
        for conn in self._opened:
            conn.close()

    def prefetch(
        self,
        queries: Sequence[tuple[Any, Callable[[], Result]]],
        *,
        pool: Pool = "postgres",
        first: bool = False,
    ) -> None:
        """Read each `(query, read)` ahead, unless its result is on disk."""
        keep = self.keep
        jobs: list[tuple[str, Callable[[], Any]]] = []
        for query, read in queries:
            digest = _digest(query)
            if keep and self._path(digest).exists():
                continue
            # Encoded on the reading thread, so the caller only writes the bytes.
            jobs.append((digest, lambda read=read: _with_blob(read(), keep)))
        if not jobs:
            return
        readers = self._readers.get(pool)
        if readers is None:
            readers = self._readers[pool] = _Readers(PARALLEL_READS[pool])
        readers.submit(jobs, first)

    def rows(
        self,
        query: Any,
        read: Callable[[], Result],
        final: Callable[[list[Row]], bool],
    ) -> list[Row]:
        """`query` (text and parameters) from disk, else from the readers or `read()`,
        and kept if final."""
        digest = _digest(query)
        path = self._path(digest)
        if self.keep and path.exists():
            self.replayed += 1
            return as_rows(_decode(path.read_bytes()))
        result, blob = self._take(digest) or (read(), None)
        rows = as_rows(result)
        if not final(rows):
            self.unsettled += 1
            return rows
        if self.keep:
            self.directory.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            partial.write_bytes(blob if blob is not None else _encode(result))
            partial.replace(path)
            self.kept += 1
        return rows

    def summary(self) -> str:
        return (
            f"snapshot {self.directory}: {self.replayed} queries replayed, "
            f"{self.kept} read and kept, {self.unsettled} read but not yet final"
        )

    def _path(self, digest: str) -> Path:
        return self.directory / f"{digest}.pickle.z"

    def _take(self, digest: str) -> tuple[Result, bytes | None] | None:
        for readers in self._readers.values():
            found, value = readers.take(digest)
            if found:
                taken: tuple[Result, bytes | None] = value
                return taken
        return None


def _digest(query: Any) -> str:
    return hashlib.sha256(repr(query).encode()).hexdigest()[:32]


def _with_blob(result: Result, keep: bool) -> tuple[Result, bytes | None]:
    return result, _encode(result) if keep else None


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
