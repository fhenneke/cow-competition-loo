"""`load_auctions` and the D17 missing-order policy.

The DB is faked by monkeypatching `extract.fetch` with canned rows keyed on the module's
own SQL constants, so the tests exercise exactly the code path the CLI uses.
"""

import pytest

from loo import extract

GOOD, BAD = 101, 102


def execution(auction_id: int, missing: bool = False) -> dict:
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
def canned_db(monkeypatch):
    data = {
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

    def fake_fetch(conn, sql, params=None):
        ids = set(params["ids"])
        return [row for row in data[sql] if row["auction_id"] in ids]

    monkeypatch.setattr(extract, "fetch", fake_fetch)


class TestMissingOrderPolicy:
    def test_raises_by_default(self, canned_db):
        with pytest.raises(extract.MissingOrderError):
            list(extract.load_auctions(None, [GOOD, BAD]))

    def test_collector_skips_the_auction_and_records_it(self, canned_db):
        missing: list[int] = []

        bundles = list(extract.load_auctions(None, [GOOD, BAD], missing_data=missing))

        assert [b.auction_id for b in bundles] == [GOOD]
        assert missing == [BAD]
        # the surviving auction is complete, not partially built
        assert len(bundles[0].bids) == 1
        assert len(bundles[0].bids[0].orders) == 1
