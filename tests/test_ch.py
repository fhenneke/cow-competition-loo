"""The ClickHouse client's pure parts: coercion, the SQL-literal guard that every
interpolated request id passes through, the network refusal, and response parsing
against a faked `urlopen`."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from loo import ch


class TestAsFloat:
    def test_none_is_zero(self):
        assert ch.as_float(None) == 0.0

    def test_integer_looking_json_numbers_become_floats(self):
        """ClickHouse renders a large `Float64` as an integer-looking number, which
        `json` hands back as `int`; left alone it compares unequal to its own float."""
        value = ch.as_float(10**21)
        assert isinstance(value, float)
        assert value == 1e21

    def test_quoted_64bit_integers_are_parsed(self):
        assert ch.as_float("123456789012345678") == 123456789012345678.0


class TestQuoteLiterals:
    def test_joins_as_sql_string_literals(self):
        assert ch.quote_literals(["a", "b"]) == "'a', 'b'"

    @pytest.mark.parametrize("bad", ["it's", "back\\slash"])
    def test_refuses_rather_than_escapes(self, bad: str):
        with pytest.raises(ValueError, match="refusing"):
            ch.quote_literals(["fine", bad])


class TestChainId:
    def test_supported_networks(self):
        assert (ch.chain_id("mainnet"), ch.chain_id("bnb"), ch.chain_id("base")) == (
            1,
            56,
            8453,
        )
        assert ch.has_quote_data("mainnet") and not ch.has_quote_data("xdai")

    def test_an_unsupported_network_is_its_own_error_and_still_a_key_error(self):
        with pytest.raises(ch.NoQuoteDataError, match="mainnet"):
            ch.chain_id("xdai")
        assert issubclass(ch.NoQuoteDataError, KeyError)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class TestFetch:
    def client(self) -> ch.Client:
        return ch.Client("http://ch.local:8123", "reader", "secret")

    def test_rows_come_back_as_dicts_and_the_query_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        seen: dict[str, Any] = {}

        def urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            seen["body"] = request.data
            seen["timeout"] = timeout
            seen["user"] = request.get_header("X-clickhouse-user")
            payload = {
                "meta": [{"name": "request_id"}, {"name": "in_amount"}],
                "data": [["r1", 1e21], ["r2", "5"]],
            }
            return FakeResponse(json.dumps(payload).encode())

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        rows = self.client().fetch("select 1", timeout=7)
        assert rows == [
            {"request_id": "r1", "in_amount": 1e21},
            {"request_id": "r2", "in_amount": "5"},
        ]
        body = seen["body"].decode()
        assert body.startswith("select 1\n")
        assert "settings max_execution_time = 7" in body
        assert body.endswith("format JSONCompact")
        assert seen["timeout"] == 37
        assert seen["user"] == "reader"

    def test_a_refused_query_carries_the_servers_message(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            raise urllib.error.HTTPError(
                "http://ch.local", 400, "Bad Request", None,  # type: ignore[arg-type]
                io.BytesIO(b"Code: 62. DB::Exception: Syntax error"),
            )

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        with pytest.raises(ch.ClickHouseError, match="Syntax error"):
            self.client().fetch("select oops")

    def test_a_response_cut_off_mid_stream_is_an_error_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            return FakeResponse(b'{"meta": [{"name": "x"}], "data": [[1], [2 Code: 241')

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        with pytest.raises(ch.ClickHouseError, match="Code: 241"):
            self.client().fetch("select big")

    def test_coverage_is_none_for_a_network_with_no_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            payload = {"meta": [{"name": "first"}, {"name": "last"}], "data": [[None, None]]}
            return FakeResponse(json.dumps(payload).encode())

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        assert self.client().coverage("bnb") is None
