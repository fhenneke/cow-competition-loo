"""`load_auctions`, the D17 missing-order policy, and the conversion-rate loader.

The DB is faked by monkeypatching `extract.fetch` with canned rows keyed on the module's
own SQL constants, so the tests exercise exactly the code path the CLI uses.
"""

from decimal import Decimal
from typing import Any, cast

import pytest

from loo import extract
from loo.db import Connection, Row

GOOD, BAD = 101, 102

CONN = cast(Connection, None)
"""`load_auctions` never touches the connection once `fetch` is faked."""


def execution(auction_id: int, missing: bool = False) -> Row:
    """One execution row; `missing=True` mimics an order in neither order table, where
    every column served by the orders join comes back NULL."""
    return {
        "auction_id": auction_id,
        "solution_uid": 0,
        "order_uid": "ab" * 56,
        "executed_sell": 10,
        "executed_buy": 20,
        "sell_token": None if missing else "aa" * 20,
        "buy_token": None if missing else "bb" * 20,
        "sell_amount": None if missing else 10,
        "buy_amount": None if missing else 15,
        "kind": None if missing else "sell",
        "partially_fillable": None if missing else False,
        "in_auction": not missing,
    }


@pytest.fixture
def canned_db(monkeypatch: pytest.MonkeyPatch) -> None:
    data: dict[str, list[Row]] = {
        extract.AUCTIONS_SQL: [
            {"auction_id": a, "block_deadline": 1, "jit_owners": []} for a in (GOOD, BAD)
        ],
        extract.SOLUTIONS_SQL: [
            {
                "auction_id": a,
                "uid": 0,
                "solver": "cc" * 20,
                "score": 5,
                "is_winner": True,
                "filtered_out": False,
            }
            for a in (GOOD, BAD)
        ],
        extract.EXECUTIONS_SQL: [execution(GOOD), execution(BAD, missing=True)],
        extract.PRICES_SQL: [],
        extract.REFERENCE_SCORES_SQL: [],
    }

    def fake_fetch(conn: Connection, sql: str, params: Any = None) -> list[Row]:
        ids = set(params["ids"])
        return [row for row in data[sql] if row["auction_id"] in ids]

    monkeypatch.setattr(extract, "fetch", fake_fetch)


class TestMissingOrderPolicy:
    def test_raises_by_default(self, canned_db: None):
        with pytest.raises(extract.MissingOrderError):
            list(extract.load_auctions(CONN, [GOOD, BAD]))

    def test_collector_skips_the_auction_and_records_it(self, canned_db: None):
        missing: list[int] = []

        bundles = list(extract.load_auctions(CONN, [GOOD, BAD], missing_data=missing))

        assert [b.auction_id for b in bundles] == [GOOD]
        assert missing == [BAD]
        # the surviving auction is complete, not partially built
        assert len(bundles[0].bids) == 1
        assert len(bundles[0].bids[0].orders) == 1


class TestConversionRates:
    """`load_conversion_rates` must query per accounting period, never per block: the
    table behind it is one row per block with no index, and a large-array block probe
    was what drove the shared DB's CPU alert (see `CONVERSION_RATES_SQL`)."""

    def test_maps_blocks_through_period_ranges(self, monkeypatch: pytest.MonkeyPatch):
        queries: list[Any] = []

        def fake_fetch(conn: Connection, sql: str, params: Any = None) -> list[Row]:
            assert sql is extract.CONVERSION_RATES_SQL
            queries.append(params)
            return [
                {
                    "first_block": 100,
                    "last_block": 199,
                    "conversion_rate_cow_to_native": 0.5,
                },
                {
                    "first_block": 200,
                    "last_block": 299,
                    "conversion_rate_cow_to_native": None,
                },
            ]

        monkeypatch.setattr(extract, "fetch", fake_fetch)

        rates = extract.load_conversion_rates(CONN, [150, 250, 350, 100])

        assert rates == {
            100: Decimal("0.5"),
            150: Decimal("0.5"),
            250: None,  # period synced, rate not snapshotted yet
            350: None,  # outside every synced period
        }
        # One query, bounded by the requested range — never a per-block array.
        assert queries == [{"first": 100, "last": 350}]

    def test_no_blocks_means_no_query(self, monkeypatch: pytest.MonkeyPatch):
        def fake_fetch(conn: Connection, sql: str, params: Any = None) -> list[Row]:
            raise AssertionError("no query expected for an empty block list")

        monkeypatch.setattr(extract, "fetch", fake_fetch)
        assert extract.load_conversion_rates(CONN, []) == {}
