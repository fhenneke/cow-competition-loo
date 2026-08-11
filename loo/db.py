"""Analytics DB connection.

`ANALYTICS_DB_URL` in `.env` is `user:password@host:port` — no scheme and no database
name, so `psycopg2.connect(url)` cannot be used directly. See docs/analytics-db.md.

The DB user is read-only and should stay that way; nothing here writes.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Sequence

import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

SCHEMA = "dbt"

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


def connect(network: str = "mainnet", *, connect_timeout: int = 20):
    """Open a read-only connection to one network's analytics database."""
    load_dotenv()
    url = os.environ.get("ANALYTICS_DB_URL")
    if not url:
        raise RuntimeError("ANALYTICS_DB_URL is not set (expected in .env)")

    credentials, hostport = url.split("@")
    user, password = credentials.split(":", 1)
    host, port = hostport.split(":")

    conn = psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=database_name(network),
        connect_timeout=connect_timeout,
    )
    conn.set_session(readonly=True)
    return conn


def fetch(conn, sql: str, params: Any = None) -> list[dict]:
    """Run a query and return rows as dicts. Preferred over `run` on the extraction
    path, where amounts must stay exact Python ints rather than become numpy dtypes."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def run(conn, sql: str, params: Any = None) -> pd.DataFrame:
    """Run a query and return a DataFrame. For ad-hoc work and notebooks."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
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


def chunked(items: Sequence, size: int):
    """Split a sequence into chunks, to keep `= any(%s)` parameter lists sane."""
    for start in range(0, len(items), size):
        yield items[start : start + size]
