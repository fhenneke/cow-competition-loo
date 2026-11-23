"""ClickHouse connection for the quote competition's data (`raw_quotes`).

The quote competition is recorded in ClickHouse, not in the analytics DB: one row per
estimator's answer in `price_estimates`, the winner in `winning_price_estimates`, and
what a request returned in `quote_computed`. Credentials are `CLICKHOUSE_URL` /
`CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` in `.env`, and the URL is a plain HTTP
endpoint — no driver is needed and none is added, so the dependency footprint of the
quote side is `urllib`.

The raw tables partition by month and carry amounts as exact decimal strings. Every
query carries a `chain_id` and a day-bounded `event_timestamp` filter (docs/quotes.md).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from dotenv import load_dotenv

RAW_DATABASE = "raw_quotes"
"""The event tables, which hold every competition. The `gold_quotes` tables built from
them miss the competitions whose `quote_requested` event never arrived — a quarter of a
day's orders — and are not read. The raw tables carry no `environment` column; the
stream is prod."""

# Chain ids of the networks the quote side is enabled on. Absence from this map is the
# support test: a network with no entry has no quote data and its quote columns report
# as unavailable rather than as zero (Q-D10). A chain is entered only once its link and
# pick reproduction have been measured (docs/quotes.md, "Coverage").
NETWORK_CHAIN_IDS = {
    "mainnet": 1,
    "bnb": 56,
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
    """The network's chain id, or a refusal naming the supported networks."""
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
        instead of per row.
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
