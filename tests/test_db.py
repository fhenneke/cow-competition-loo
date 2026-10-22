"""How `db.fetch` names a failed query, against a faked connection.

A lost connection and a statement timeout both arrive from psycopg2 as an
`OperationalError`; a run that dies on either must say which, and that neither is a
data problem. Everything else passes through untouched — `extract.fetch_prices` catches
`UndefinedTable` by its own name.
"""

from __future__ import annotations

from typing import Any, cast

import psycopg2
import psycopg2.errors
import pytest

from loo import db


class FakeConnection:
    dsn = "user=loo password=xxx dbname=prod_bnb host=db.example port=5432"

    def __init__(self, error: Exception, *, breaks: bool) -> None:
        self.error = error
        self.breaks = breaks
        self.closed = 0

    def cursor(self, cursor_factory: Any = None) -> FakeCursor:
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if self.conn.breaks:
            # What psycopg2 does once libpq reports the connection bad.
            self.conn.closed = 2
        raise self.conn.error


def fetch_failing_with(error: Exception, *, breaks: bool = False) -> None:
    conn = cast(db.Connection, FakeConnection(error, breaks=breaks))
    db.fetch(conn, "select order_uid\n  from dbt.stg_backend_data__orders")


class TestNamedFailures:
    def test_a_broken_connection_is_named_lost_whatever_libpq_said(self):
        error = psycopg2.OperationalError("SSL SYSCALL error: Operation timed out\n")
        with pytest.raises(db.ConnectionLostError) as info:
            fetch_failing_with(error, breaks=True)
        message = str(info.value)
        assert "prod_bnb" in message
        assert "SSL SYSCALL error: Operation timed out." in message
        assert "not the data" in message
        assert info.value.__cause__ is error

    def test_a_statement_timeout_is_named_with_its_query(self):
        error = psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")
        with pytest.raises(db.StatementTimeoutError) as info:
            fetch_failing_with(error)
        assert db.STATEMENT_TIMEOUT in str(info.value)
        assert "select order_uid from dbt.stg_backend_data__orders" in str(info.value)

    @pytest.mark.parametrize(
        "error",
        [
            psycopg2.errors.QueryCanceled("canceling statement due to user request"),
            psycopg2.errors.UndefinedTable('relation "dbt.x" does not exist'),
            psycopg2.OperationalError("could not serialize access"),
        ],
    )
    def test_anything_on_an_open_connection_passes_through(self, error: Exception):
        with pytest.raises(type(error)) as info:
            fetch_failing_with(error)
        assert info.value is error
