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
        extract.FEE_POLICIES_SQL: [
            {
                "auction_id": GOOD,
                "order_uid": "ab" * 56,
                "kind": "volume",
                "surplus_factor": None,
                "surplus_max_volume_factor": None,
                "volume_factor": 0.001,
                "price_improvement_factor": None,
                "price_improvement_max_volume_factor": None,
            }
        ],
        extract.PARTNER_COUNTS_SQL: [],
        extract.QUOTES_SQL: [],
    }

    def fake_fetch(conn: Connection, sql: str, params: Any = None) -> list[Row]:
        # The uid-keyed queries carry no auction_id to filter on.
        if "uids" in params:
            return data[sql]
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


class TestFeeContext:
    """`load_auctions` derives fee coverage from the solutions it already has: an
    auction order's policies are recorded iff it appears in a solution that was not
    filtered out."""

    def test_order_of_a_kept_solution_is_covered(self, canned_db: None):
        bundle = next(iter(extract.load_auctions(CONN, [GOOD])))
        context = bundle.fee_context
        assert context.known == frozenset({"ab" * 56})
        assert len(context.policies["ab" * 56]) == 1
        assert context.policies["ab" * 56][0].volume_factor == 0.001

    def test_with_fees_off_leaves_every_fee_unrecorded(self, canned_db: None):
        bundle = next(iter(extract.load_auctions(CONN, [GOOD], with_fees=False)))
        assert bundle.fee_context is extract.EMPTY_FEE_CONTEXT
        assert bundle.fee_context.known == frozenset()

    def test_filtered_out_only_order_is_not_covered(
        self, canned_db: None, monkeypatch: pytest.MonkeyPatch
    ):
        """The whole gap: an order seen only by a filtered-out solution has no recorded
        policies, and "no rows" must not be read as "no fees"."""
        original = extract.fetch

        def fetch_with_filtered_solution(
            conn: Connection, sql: str, params: Any = None
        ) -> list[Row]:
            rows = original(conn, sql, params)
            if sql is extract.SOLUTIONS_SQL:
                return [{**row, "filtered_out": True} for row in rows]
            return rows

        monkeypatch.setattr(extract, "fetch", fetch_with_filtered_solution)
        bundle = next(iter(extract.load_auctions(CONN, [GOOD])))
        assert bundle.fee_context.known == frozenset()


class TestAllSolvers:
    """`--all-solvers` derives the scenario list from the window (D26)."""

    def test_names_group_addresses_and_uncatalogued_stay_addresses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The query does the grouping, so the test pins the contract at the seam: one
        label per competitor, and every label is something `resolve_solver` accepts."""
        rows = [{"label": "Baseline"}, {"label": "Fractal"}, {"label": "26a6fdc0" * 5}]

        def fake_fetch(_conn: Connection, sql: str, _params: Any = None) -> list[Row]:
            assert sql is extract.ALL_SOLVERS_SQL
            return rows

        monkeypatch.setattr(extract, "fetch", fake_fetch)
        conn = cast("Connection", None)

        assert extract.all_solvers(conn, "2026-07-01", "2026-08-01") == [
            "Baseline",
            "Fractal",
            "26a6fdc0" * 5,
        ]

    def test_an_empty_window_yields_no_scenarios(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_fetch(_conn: Connection, _sql: str, _params: Any = None) -> list[Row]:
            return []

        monkeypatch.setattr(extract, "fetch", fake_fetch)
        assert extract.all_solvers(cast("Connection", None), "a", "b") == []


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
