"""ClickHouse connection for the quote competition's data (`gold_quotes`).

The quote competition is recorded in ClickHouse, not in the analytics DB: one row per
request in `fct_quote_requests` and one per estimator's answer in
`fct_quote_estimates`. Credentials are `CLICKHOUSE_URL` / `CLICKHOUSE_USER` /
`CLICKHOUSE_PASSWORD` in `.env`, and the URL is a plain HTTP endpoint — no driver is
needed and none is added, so the dependency footprint of the quote side is `urllib`.

Two shapes of that schema decide how every query here is written, and both cost real
time to rediscover (docs/quotes.md):

- `fct_quote_estimates` is sorted `(chain_id, environment, estimator, event_timestamp,
  request_id)`. `estimator = …` is a key prefix; `request_id in (…)` is **not**, so a
  request lookup scans whatever partitions the time filter leaves.
- Both tables partition by `(chain_id, environment, toYYYYMMDD(event_timestamp))`, so a
  day-bounded filter prunes to that day. Every query below carries one.

The `_live` views are deliberately never used: they splice the last ≤6 hours in from the
raw tables, so two runs over the same window can disagree.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from dotenv import load_dotenv

DATABASE = "gold_quotes"
RAW_DATABASE = "raw_quotes"
"""The event tables the gold ones are built from. Needed because `gold_quotes` is built
off the `quote_requested` event and a competition whose request event never arrived is
in neither gold table, while its estimates and winner are complete in the raw layer
(`quotes.load_raw_candidates`). The raw tables carry no `environment` column; measured
on 2026-07-25 mainnet the raw competition count equals the gold prod count plus exactly
the request-less ones, so the stream is prod."""

# Chain ids as `gold_quotes` records them, for the networks the quote side is enabled on.
# Absence from this map is the support test: a network with no entry has no quote data
# and its quote columns report as unavailable rather than as zero (Q-D10). A chain is
# entered only once its link and pick reproduction have been measured on both links
# (docs/quotes.md, "Coverage"); lens and sepolia have no quote data at all.
NETWORK_CHAIN_IDS = {
    "mainnet": 1,  # prod requests from 2026-07-22 15:28
    "bnb": 56,  # from 2026-07-22 15:28
    # The rest from 2026-09-01 16:00.
    "base": 8453,
    "arbitrum-one": 42161,
    "xdai": 100,
    "polygon": 137,
    "avalanche": 43114,
    "ink": 57073,
    "plasma": 9745,
    "linea": 59144,
}

STATEMENT_TIMEOUT_SECONDS = 300
"""Every query carries it, in the spirit of CLAUDE.md's bound-every-query rule: a bad
filter fails instead of holding a shared cluster."""

Row = dict[str, Any]


class ClickHouseError(RuntimeError):
    """A query the server refused or a response it could not complete."""


class NoQuoteDataError(KeyError):
    """The quote competition has no data for this network.

    A `KeyError` so that a lookup-shaped caller still reads it as one, but its own type
    so the CLI can report it without catching every stray `KeyError` a bug might raise.
    """


def chain_id(network: str) -> int:
    """The chain id `gold_quotes` uses, or a refusal naming the supported networks."""
    try:
        return NETWORK_CHAIN_IDS[network]
    except KeyError:
        known = ", ".join(sorted(NETWORK_CHAIN_IDS))
        raise NoQuoteDataError(
            f"no quote data for network {network!r}; quote coverage is {known} "
            f"(Q-D10 — report quote columns as unavailable, not empty)"
        ) from None


def has_quote_data(network: str) -> bool:
    return network in NETWORK_CHAIN_IDS


class Client:
    """A ClickHouse HTTP endpoint, read-only by usage.

    Constructed from the environment once and passed around, mirroring how `db.connect`
    hands out a connection. Holds no state beyond the credentials, so it is safe to
    reuse across a whole window.
    """

    def __init__(self, url: str, user: str, password: str) -> None:
        self.url = url
        self._user = user
        self._password = password

    @classmethod
    def from_env(cls) -> Client:
        load_dotenv()
        missing = [
            name
            for name in ("CLICKHOUSE_URL", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD")
            if not os.environ.get(name)
        ]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} not set (expected in .env) — the quote side "
                f"needs the ClickHouse credentials alongside ANALYTICS_DB_URL"
            )
        return cls(
            os.environ["CLICKHOUSE_URL"].strip().strip('"'),
            os.environ["CLICKHOUSE_USER"],
            os.environ["CLICKHOUSE_PASSWORD"],
        )

    def fetch(self, sql: str, *, timeout: int = STATEMENT_TIMEOUT_SECONDS) -> list[Row]:
        """Run a query and return rows as dicts.

        `JSONCompact` rather than `JSON` because the column names then arrive once
        instead of per row; large `Float64` values come back as integer-looking JSON
        numbers either way, which is why every amount is coerced through `as_float`.
        """
        body = f"{sql}\nsettings max_execution_time = {timeout}\nformat JSONCompact"
        request = urllib.request.Request(
            self.url,
            data=body.encode(),
            headers={
                "X-ClickHouse-User": self._user,
                "X-ClickHouse-Key": self._password,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout + 30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:2000]
            raise ClickHouseError(f"ClickHouse refused the query: {detail}") from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            # An error raised after the server started streaming arrives as a 200 with
            # the JSON cut off and the message appended; the tail is where it is.
            tail = raw[-2000:].decode(errors="replace")
            raise ClickHouseError(
                f"ClickHouse returned an incomplete response: …{tail}"
            ) from error
        columns = [column["name"] for column in payload["meta"]]
        return [dict(zip(columns, row, strict=True)) for row in payload["data"]]

    def coverage(self, network: str) -> tuple[str, str] | None:
        """`(first, last)` prod request timestamps for a network, or `None` if it has
        none. Queried rather than assumed: the backfill start has moved once already."""
        rows = self.fetch(
            f"""
            select min(event_timestamp) as first, max(event_timestamp) as last
            from {DATABASE}.fct_quote_requests
            where chain_id = {chain_id(network)} and environment = 'prod'
            """
        )
        if not rows or rows[0]["first"] is None:
            return None
        return str(rows[0]["first"]), str(rows[0]["last"])


def as_float(value: Any) -> float:
    """Coerce a ClickHouse amount to `float`.

    Amounts in `gold_quotes` are `Float64`, so the quote side cannot be integer-exact
    the way the rest of `loo` is: an 18-decimal amount above ~0.01 tokens is already
    past `2^53`. JSON also hands large `Float64` values back as integer-looking values,
    which coerce silently to `int` and then compare unequal to their own float — hence
    one conversion point rather than many.
    """
    if value is None:
        return 0.0
    return float(value)


def quote_literals(values: Sequence[str]) -> str:
    """A comma-separated SQL list of string literals.

    Every value that reaches this is a request id or a 0x token address read back out
    of one of these two databases, so the escaping is a guard against a malformed row
    rather than against a user: a value carrying a quote is refused, not escaped.
    """
    for value in values:
        if "'" in value or "\\" in value:
            raise ValueError(f"refusing to interpolate {value!r} into SQL")
    return ", ".join(f"'{value}'" for value in values)
