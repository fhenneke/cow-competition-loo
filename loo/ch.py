"""ClickHouse over plain HTTP (`urllib`, no driver) for the quote competition.

Every query carries a `chain_id` and a day-bounded `event_timestamp` filter.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .db import Snapshot, always, load_env

RAW_DATABASE = "raw_quotes"  # not gold_quotes: those miss competitions

# Networks with quote data. Absent means quote columns are unavailable, not zero.
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

Row = dict[str, Any]


class ClickHouseError(RuntimeError):
    """A refused query or a truncated response."""


class NoQuoteDataError(KeyError):
    """No quote data for this network. Its own type so the CLI need not catch `KeyError`."""


def chain_id(network: str) -> int:
    try:
        return NETWORK_CHAIN_IDS[network]
    except KeyError:
        known = ", ".join(sorted(NETWORK_CHAIN_IDS))
        raise NoQuoteDataError(
            f"no quote data for network {network!r}; quote coverage is {known} "
            f"(report quote columns as unavailable, not empty)"
        ) from None


def has_quote_data(network: str) -> bool:
    return network in NETWORK_CHAIN_IDS


class Client:
    """A read-only ClickHouse HTTP endpoint; stateless beyond credentials. With a
    `snapshot`, results are read from it and kept in it as `db.fetch` does."""

    def __init__(
        self, url: str, user: str, password: str, snapshot: Snapshot | None = None
    ) -> None:
        self.url = url
        self._user = user
        self._password = password
        self.snapshot = snapshot

    @classmethod
    def from_env(cls, snapshot: Snapshot | None = None) -> Client:
        load_env()
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
            snapshot,
        )

    def fetch(self, sql: str, *, timeout: int = STATEMENT_TIMEOUT_SECONDS) -> list[Row]:
        if self.snapshot is not None:
            return self.snapshot.rows(
                ("clickhouse", sql), lambda: self._query(sql, timeout), always
            )
        return self._query(sql, timeout)

    def _query(self, sql: str, timeout: int) -> list[Row]:
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
            # A mid-stream error arrives as a 200 with the message appended to cut JSON.
            tail = raw[-2000:].decode(errors="replace")
            raise ClickHouseError(
                f"ClickHouse returned an incomplete response: …{tail}"
            ) from error
        columns = [column["name"] for column in payload["meta"]]
        return [dict(zip(columns, row, strict=True)) for row in payload["data"]]
