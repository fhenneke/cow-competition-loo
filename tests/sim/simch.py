"""ClickHouse, simulated: an HTTP endpoint serving the world's quote competition.

`loo.ch.Client.fetch` runs unchanged — it builds the request, `urllib` "sends" it, and
the reply is a `JSONCompact` body — because `urlopen` is the thing replaced. The five
query shapes the quote link sends are recognised by their leading select and answered
with their filters applied: chain id, and per unit (a day of the link) its event-time
window and its quote-id subquery or request-key tuples, each row tagged with the units it
meets. Amounts come back as exact decimal strings, as the raw layer stores them, and a
`ReplacingMergeTree` duplicate is served now and then.
"""

from __future__ import annotations

import io
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .world import Estimate, Request, World

PROXY_BODY_BYTES = 1_000_000
"""nginx's `client_max_body_size` in front of the real endpoint, about 1 MB."""

TAIL = re.compile(r"\nsettings max_execution_time = (\d+)\nformat JSONCompact$")
WINDOW = r"event_timestamp >= '([^']+)' and event_timestamp < '([^']+)'"
LINK_UNIT = re.compile(
    rf"if\({WINDOW} and request_id in \(select request_id from raw_quotes\.price_estimates "
    rf"where chain_id = \d+ and {WINDOW} and quote_id in \(([\d, ]+)\)\), (\d+), 0\)"
)
REQUOTE_UNIT = re.compile(
    rf"if\({WINDOW} and \(lower\(sell_token\), lower\(buy_token\), kind, in_amount\) "
    rf"in \((.*?)\), (\d+), 0\)"
)
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
    _count: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def urlopen(self, request: urllib.request.Request, timeout: float) -> Response:
        data = request.data
        assert isinstance(data, bytes)
        body = data.decode()
        tail = TAIL.search(body)
        assert tail, body
        assert timeout == int(tail.group(1)) + 30, "the socket must outlive the query"
        assert request.get_header("X-clickhouse-user") == "sim-reader"
        sql = body[: tail.start()]
        # The proxy in front refuses a body over 1 MB, before ClickHouse sees it.
        if len(data) > PROXY_BODY_BYTES:
            raise urllib.error.HTTPError(
                "http://sim.clickhouse", 413, "Request Entity Too Large", None,  # type: ignore[arg-type]
                io.BytesIO(b"<html>413 Request Entity Too Large</html>"),
            )
        # The parser's limit: 256 kB unless the URL raises it.
        settings = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        limit = int(settings.get("max_query_size", ["262144"])[0])
        if len(data) > limit:
            raise urllib.error.HTTPError(
                "http://sim.clickhouse", 400, "Bad Request", None,  # type: ignore[arg-type]
                io.BytesIO(b"Code: 62. DB::Exception: Max query size exceeded (simulated)"),
            )
        with self._count:  # `loo` asks from several threads
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
        assert "notEmpty(units)" in sql

        def window(lo: str, hi: str) -> Callable[[Estimate], bool]:
            start, end = datetime.fromisoformat(lo), datetime.fromisoformat(hi)
            return lambda e: start <= e.timestamp < end

        links = [
            (window(lo, hi), self._linked(window(lo2, hi2), ids), int(k))
            for lo, hi, lo2, hi2, ids, k in LINK_UNIT.findall(sql)
        ]

        def linked(r: Request, e: Estimate) -> list[int]:
            """The units whose window holds `e` and whose ids link `r`."""
            return [k for inside, requests, k in links if inside(e) and r.request_id in requests]

        if sql.startswith("select request_id, estimator, arrayFilter"):
            return ("request_id", "estimator", "units"), [
                [r.request_id, r.winner.answer.estimator, units]
                for r in self.world.requests
                if r.request_id is not None and r.winner is not None
                and (units := linked(r, r.winner))
            ]
        if sql.startswith("select request_id, quote_id, nats_seq, arrayFilter"):
            return ("request_id", "quote_id", "nats_seq", "units"), [
                [r.request_id, e.quote_id, e.nats_seq, units]
                for r in self.world.requests
                if r.request_id is not None
                for e in r.forwarded
                if (units := linked(r, e))
            ]
        if sql.startswith("select request_id, estimator, solver, quote_id"):
            assert " success " in sql
            return (*LADDER, "units"), [
                [*self._row(LADDER, e), units]
                for r in self.world.requests
                if r.request_id is not None
                for e in self._served(r.estimates)
                if e.success and (units := linked(r, e))
            ]
        assert "request_id is null" in sql
        keyed_units = [
            (window(lo, hi), {(s, b, k, int(a)) for s, b, k, a in KEYS.findall(tuples)}, int(n))
            for lo, hi, tuples, n in REQUOTE_UNIT.findall(sql)
        ]
        requotes = [r for r in self.world.requests if r.request_id is None]

        def keyed(e: Estimate) -> list[int]:
            key = (e.sell_token, e.buy_token, e.kind, e.in_amount)
            return [n for inside, keys, n in keyed_units if key in keys and inside(e)]

        if sql.startswith("select quote_id, arrayFilter"):
            return ("quote_id", "units"), [
                [r.winner.quote_id, units]
                for r in requotes
                if r.winner is not None and (units := keyed(r.winner))
            ]
        if sql.startswith("select quote_id, estimator, solver"):
            return (*REQUOTE, "units"), [
                [*self._row(REQUOTE, e), units]
                for r in requotes
                for e in self._served(r.estimates)
                if e.success and (units := keyed(e))
            ]
        raise AssertionError(f"the simulated ClickHouse does not know this query:\n{sql}")

    def _linked(self, inside: Callable[[Estimate], bool], ids_text: str) -> set[str | None]:
        """`select request_id … where {window} and quote_id in (…)`, as a set."""
        ids = {int(i) for i in ids_text.split(", ")}
        return {
            request.request_id
            for request in self.world.requests
            if request.request_id is not None
            and any(e.quote_id in ids and inside(e) for e in request.estimates)
        }

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
