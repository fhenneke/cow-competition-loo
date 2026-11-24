"""ClickHouse, simulated: an HTTP endpoint serving the world's quote competition.

`loo.ch.Client.fetch` runs unchanged — it builds the request, `urllib` "sends" it, and
the reply is a `JSONCompact` body — because `urlopen` is the thing replaced. The five
query shapes the quote link sends are recognised by their leading select and answered
with their filters applied: chain id, the event-time window, the quote-id subquery and
the request-key tuples. Amounts come back as exact decimal strings, as the raw layer
stores them, and a `ReplacingMergeTree` duplicate is served now and then.
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .world import Estimate, Request, World

TAIL = re.compile(r"\nsettings max_execution_time = (\d+)\nformat JSONCompact$")
WINDOW = re.compile(r"event_timestamp >= '([^']+)' and event_timestamp < '([^']+)'")
KEYS = re.compile(r"\('0x([0-9a-f]+)', '0x([0-9a-f]+)', '(\w+)', '(\d+)'\)")

LADDER = ("request_id", "estimator", "solver", "quote_id", "kind", "sell_", "buy_",
          "in_amount", "out_amount", "gas", "verified", "nats_seq", "ts")
REQUOTE = ("quote_id", "estimator", "solver", "sell_", "buy_", "kind", "in_amount",
           "out_amount", "gas", "verified", "nats_seq", "ts")


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@dataclass
class SimClickHouse:
    world: World
    refuse_at: int | None = None
    truncate_at: int | None = None
    queries: list[str] = field(default_factory=list[str])

    def urlopen(self, request: urllib.request.Request, timeout: float) -> Response:
        data = request.data
        assert isinstance(data, bytes)
        body = data.decode()
        tail = TAIL.search(body)
        assert tail, body
        assert timeout == int(tail.group(1)) + 30, "the socket must outlive the query"
        assert request.get_header("X-clickhouse-user") == "sim-reader"
        sql = body[: tail.start()]
        self.queries.append(sql)
        n = len(self.queries)
        if self.refuse_at == n:
            raise urllib.error.HTTPError(
                "http://sim.clickhouse", 400, "Bad Request", None,  # type: ignore[arg-type]
                io.BytesIO(b"Code: 62. DB::Exception: Syntax error (simulated)"),
            )
        columns, rows = self._answer(sql)
        payload = json.dumps({"meta": [{"name": c} for c in columns], "data": rows}).encode()
        if self.truncate_at == n:
            payload = payload[: len(payload) // 2] + b" Code: 241. DB::Exception: memory"
        return Response(payload)

    # --- query semantics --------------------------------------------------------------

    def _answer(self, sql: str) -> tuple[tuple[str, ...], list[list[Any]]]:
        chain = int(re.findall(r"chain_id = (\d+)", sql)[0])
        assert chain == self.world.chain_id, "every query carries the network's chain id"
        lo, hi = (datetime.fromisoformat(t) for t in WINDOW.findall(sql)[0])

        def inside(e: Estimate) -> bool:
            return lo <= e.timestamp < hi

        if sql.startswith("select request_id, estimator from raw_quotes.winning_price_estimates"):
            return ("request_id", "estimator"), [
                [r.request_id, r.winner.answer.estimator]
                for r in self._linked(sql, inside)
                if r.winner is not None and inside(r.winner)
            ]
        if sql.startswith("select request_id, quote_id, nats_seq from raw_quotes.quote_computed"):
            return ("request_id", "quote_id", "nats_seq"), [
                [r.request_id, e.quote_id, e.nats_seq]
                for r in self._linked(sql, inside)
                for e in r.forwarded
                if inside(e)
            ]
        if sql.startswith("select request_id, estimator, solver, quote_id"):
            assert " success " in sql
            return LADDER, [
                self._row(LADDER, e)
                for r in self._linked(sql, inside)
                for e in self._served(r.estimates)
                if e.success and inside(e)
            ]
        keys = {(s, b, k, int(a)) for s, b, k, a in KEYS.findall(sql)}
        assert "request_id is null" in sql
        requotes = [r for r in self.world.requests if r.request_id is None]

        def keyed(e: Estimate) -> bool:
            return (e.sell_token, e.buy_token, e.kind, e.in_amount) in keys and inside(e)

        if sql.startswith("select quote_id from raw_quotes.winning_price_estimates"):
            return ("quote_id",), [
                [r.winner.quote_id] for r in requotes if r.winner is not None and keyed(r.winner)
            ]
        if sql.startswith("select quote_id, estimator, solver"):
            return REQUOTE, [
                self._row(REQUOTE, e)
                for r in requotes
                for e in self._served(r.estimates)
                if e.success and keyed(e)
            ]
        raise AssertionError(f"the simulated ClickHouse does not know this query:\n{sql}")

    def _linked(self, sql: str, inside: Any) -> Iterator[Request]:
        """`request_id in (select request_id … where quote_id in (…))`."""
        ids_text = re.search(r"quote_id in \(([\d, ]+)\)", sql)
        assert ids_text
        ids = {int(i) for i in ids_text.group(1).split(", ")}
        for request in self.world.requests:
            if request.request_id is None:
                continue
            if any(e.quote_id in ids and inside(e) for e in request.estimates):
                yield request

    def _served(self, estimates: list[Estimate]) -> Iterator[Estimate]:
        """Every row, plus an unmerged duplicate of some, arriving later."""
        for e in estimates:
            yield e
        for e in estimates:
            if e.quote_id % 5 == 0:
                yield Estimate(e.request_id, e.answer, e.quote_id, e.nats_seq + 10**9,
                               e.timestamp, e.solver, e.success, e.kind, e.sell_token,
                               e.buy_token, e.in_amount)

    def _row(self, columns: tuple[str, ...], e: Estimate) -> list[Any]:
        values: dict[str, Any] = {
            "request_id": e.request_id,
            "estimator": e.answer.estimator,
            "solver": e.solver,
            "quote_id": e.quote_id,
            "kind": e.kind,
            "sell_": "0x" + e.sell_token,
            "buy_": "0x" + e.buy_token,
            "in_amount": str(e.in_amount),
            "out_amount": str(e.answer.out_amount),
            "gas": str(e.answer.gas),
            "verified": e.answer.verified,
            "nats_seq": e.nats_seq,
            "ts": stamp(e.timestamp),
        }
        return [values[c] for c in columns]
