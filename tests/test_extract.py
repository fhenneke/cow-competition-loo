"""`load_auctions`, the missing-order error and fast-path exclusion, the price source, the
conversion-rate loader, and the reward-cap loaders.

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
    """One execution row; `missing=True` mimics an auction order absent from the orders
    table, where every column served by the join comes back NULL."""
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
    }


@pytest.fixture
def canned_db(monkeypatch: pytest.MonkeyPatch) -> None:
    data: dict[str, list[Row]] = {
        extract.AUCTIONS_SQL: [
            {"auction_id": a, "block_deadline": 1, "unpriced": False, "fast_path": False}
            for a in (GOOD, BAD)
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
        extract.price_query(extract.PRICES_SQL): [],
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


class TestMissingOrder:
    """An auction order absent from the orders table is a broken sync, so it is loud."""

    def test_it_raises(self, canned_db: None):
        with pytest.raises(extract.MissingOrderError):
            list(extract.load_auctions(CONN, [GOOD, BAD]))


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


class TestFastPath:
    """A fast-path auction settles one order straight from its quote competition; the
    losing quotes are stored as solutions below the order's limit. It is excluded by
    name rather than arbitrated."""

    def fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = extract.fetch

        def fetch(conn: Connection, sql: str, params: Any = None) -> list[Row]:
            rows = original(conn, sql, params)
            if sql is extract.AUCTIONS_SQL:
                return [{**row, "fast_path": row["auction_id"] == BAD} for row in rows]
            return rows

        monkeypatch.setattr(extract, "fetch", fetch)

    def test_it_is_skipped_and_recorded(
        self, canned_db: None, monkeypatch: pytest.MonkeyPatch
    ):
        self.fast(monkeypatch)
        fast_path: list[int] = []

        bundles = list(extract.load_auctions(CONN, [GOOD, BAD], fast_path=fast_path))

        assert [b.auction_id for b in bundles] == [GOOD]
        assert fast_path == [BAD]

    def test_it_is_skipped_without_a_collector_too(
        self, canned_db: None, monkeypatch: pytest.MonkeyPatch
    ):
        """It comes before the missing-order check: `BAD`'s order is missing as well,
        and a fast-path auction must not die on data nobody is going to use."""
        self.fast(monkeypatch)
        assert [b.auction_id for b in extract.load_auctions(CONN, [GOOD, BAD])] == [GOOD]


class TestPriceSource:
    """Prices come from the `competition_auctions` arrays and from nowhere else."""

    def test_prices_are_read_from_the_arrays(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[tuple[str, Any]] = []

        def fake_fetch(conn: Connection, sql: str, params: Any = None) -> list[Row]:
            calls.append((sql, params))
            return []

        monkeypatch.setattr(extract, "fetch", fake_fetch)
        params = {"ids": [GOOD], "tokens": []}
        extract.fetch_prices(CONN, extract.USD_PRICES_SQL, params)

        [(sql, sent)] = calls
        assert "price_tokens" in sql
        assert sent == params

    def test_array_prices_reach_the_bundle(
        self, canned_db: None, monkeypatch: pytest.MonkeyPatch
    ):
        original = extract.fetch
        prices_sql = extract.price_query(extract.PRICES_SQL)

        def fetch(conn: Connection, sql: str, params: Any = None) -> list[Row]:
            if sql == prices_sql:
                return [{"auction_id": GOOD, "token": "bb" * 20, "price": 7}]
            return original(conn, sql, params)

        monkeypatch.setattr(extract, "fetch", fetch)
        bundle = next(iter(extract.load_auctions(CONN, [GOOD])))
        assert bundle.native_prices == {"bb" * 20: 7}

    def test_bids_with_null_price_arrays_raise(
        self, canned_db: None, monkeypatch: pytest.MonkeyPatch
    ):
        """An auction before 2026-09-15: without the guard it becomes a valuation
        failure and the run exits 0."""
        original = extract.fetch

        def fetch_unpriced(conn: Connection, sql: str, params: Any = None) -> list[Row]:
            if sql is extract.AUCTIONS_SQL:
                return [{**row, "unpriced": True} for row in original(conn, sql, params)]
            return original(conn, sql, params)

        monkeypatch.setattr(extract, "fetch", fetch_unpriced)
        with pytest.raises(extract.MissingPricesError, match=f"auction {GOOD} has bids"):
            list(extract.load_auctions(CONN, [GOOD]))


class TestSupportedWindow:
    """Only the current mechanism is replayed, so an older window fails by name."""

    def test_a_window_before_the_mechanism_start_is_refused(self):
        with pytest.raises(extract.UnsupportedWindowError, match="2026-10-27"):
            extract.require_current_mechanism("2026-10-27")

    def test_the_first_supported_day_and_later_pass(self):
        extract.require_current_mechanism(extract.MECHANISM_START)
        extract.require_current_mechanism("2026-11-18 12:00:00")


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


class TestRewardCaps:
    @pytest.fixture
    def cap_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def row(uid: int, penalty: Decimal | None) -> Row:
            return {
                "auction_id": GOOD,
                "solution_uid": uid,
                "solver": "cc" * 20,
                "score": 5,
                "is_settled_in_time": False,
                "upper_reward_cap": Decimal(0),
                "lower_reward_cap": Decimal(-(10**16)),
                "penalty_cap_native": penalty,
                "is_excluded": False,
            }

        def fake_fetch(conn: Connection, sql: str, params: Any = None) -> list[Row]:
            assert sql == extract.REWARD_INPUTS_SQL
            return [row(0, Decimal("28000000000000.5")), row(1, Decimal(0)), row(2, None)]

        monkeypatch.setattr(extract, "fetch", fake_fetch)

    def test_a_missing_penalty_cap_stays_none_and_a_zero_one_stays_zero(
        self, cap_rows: None
    ):
        """NULL means "no cap, use the flat floor"; 0 is a cap that waives the penalty.
        Collapsing the two is the bug cow-dagster#1058 fixed in the mart."""
        caps = extract.load_solution_caps(CONN, [GOOD])[GOOD]
        assert [caps[uid].penalty for uid in (0, 1, 2)] == [
            Decimal("28000000000000.5"),
            Decimal(0),
            None,
        ]
        inputs = extract.load_reward_inputs(CONN, [GOOD])[GOOD]
        assert [win.penalty_cap for win in inputs.wins] == [
            Decimal("28000000000000.5"),
            Decimal(0),
            None,
        ]
        assert inputs.lower_cap == -(10**16)
