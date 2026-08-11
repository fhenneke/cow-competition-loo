"""Pull one auction's worth of bids, orders and prices out of the analytics DB.

Reads the raw staging tables rather than `int_backend_data__proposed_solution_data`:
the intermediate model lags the staging tables by 2-3 days and carries no
`filtered_out` column, which M1's comparison needs. The intermediate model is still
used, optionally, as an independent check on the surplus computation
(`load_db_order_surplus`).

Joins follow docs/analytics-db.md — in particular `proposed_trade_executions.solution_uid`
joins `proposed_solutions.uid`, never `.id`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

from .db import as_int, chunked, fetch
from .primitives import order_owner
from .valuation import Order

CHUNK_SIZE = 200


class MissingOrderError(Exception):
    """A traded order uid is in neither `orders` nor `jit_orders`."""


@dataclass(frozen=True)
class Bid:
    """One proposed solution, with the orders it trades."""

    auction_id: int
    uid: int
    solver: str
    score: int
    is_winner: bool
    filtered_out: bool
    orders: tuple[Order, ...]
    contributes: dict[str, bool]
    """Per order uid: does it count toward score (`auction.rs:45`)?"""


@dataclass(frozen=True)
class AuctionBundle:
    auction_id: int
    block_deadline: int
    jit_owners: frozenset[str]
    native_prices: dict[str, int]
    bids: tuple[Bid, ...]
    """Ordered by `uid`, i.e. best-to-worst as the autopilot recorded them."""
    reference_scores: dict[str, int]
    """Ground truth from `stg_backend_data__reference_scores`, winners only."""


WINDOW_SQL = """
select distinct auction_id, block_deadline
from dbt.pre_stg__orders_per_auction_with_at_least_one_bid
where block_deadline between
      (select min(block_number) from dbt.stg_rpc_data__block_timestamp where time >= %(start)s)
  and (select max(block_number) from dbt.stg_rpc_data__block_timestamp where time <  %(end)s)
order by auction_id
"""

SOLUTIONS_SQL = """
select auction_id, uid, encode(solver, 'hex') as solver, score, is_winner, filtered_out
from dbt.stg_backend_data__proposed_solutions
where auction_id = any(%(ids)s)
order by auction_id, uid
"""

# `partially_fillable` and the order side come from whichever of the two order tables
# has the uid; `coalesce` on the enums is done via ::text since the two columns are
# separate enum types.
EXECUTIONS_SQL = """
select
    pte.auction_id,
    pte.solution_uid,
    encode(pte.order_uid, 'hex')                            as order_uid,
    pte.executed_sell,
    pte.executed_buy,
    encode(coalesce(o.sell_token, j.sell_token), 'hex')     as sell_token,
    encode(coalesce(o.buy_token, j.buy_token), 'hex')       as buy_token,
    coalesce(o.sell_amount, j.sell_amount)                  as sell_amount,
    coalesce(o.buy_amount, j.buy_amount)                    as buy_amount,
    coalesce(o.kind::text, j.kind::text)                    as kind,
    coalesce(o.partially_fillable, j.partially_fillable)    as partially_fillable,
    (pte.order_uid = any(ca.order_uids))                    as in_auction
from dbt.stg_backend_data__proposed_trade_executions pte
join dbt.stg_backend_data__competition_auctions ca
    on ca.auction_id = pte.auction_id
left join dbt.stg_backend_data__orders o
    on o.uid = pte.order_uid
left join dbt.stg_backend_data__jit_orders j
    on j.uid = pte.order_uid
where pte.auction_id = any(%(ids)s)
"""

AUCTIONS_SQL = """
select
    auction_id,
    block_deadline,
    array(
        select encode(owner, 'hex')
        from unnest(surplus_capturing_jit_order_owners) as owner
    ) as jit_owners
from dbt.stg_backend_data__competition_auctions
where auction_id = any(%(ids)s)
"""

# Only buy-token prices are needed: `compute_order_score` converts through the buy
# token for both order sides. Restricting to traded tokens keeps this from returning
# every price in every auction (~900 rows each).
PRICES_SQL = """
with traded as (
    select distinct
        pte.auction_id,
        coalesce(o.buy_token, j.buy_token) as token
    from dbt.stg_backend_data__proposed_trade_executions pte
    left join dbt.stg_backend_data__orders o on o.uid = pte.order_uid
    left join dbt.stg_backend_data__jit_orders j on j.uid = pte.order_uid
    where pte.auction_id = any(%(ids)s)
)
select ap.auction_id, encode(ap.token, 'hex') as token, ap.price
from dbt.stg_backend_data__auction_prices ap
join traded t on t.auction_id = ap.auction_id and t.token = ap.token
"""

REFERENCE_SCORES_SQL = """
select auction_id, encode(solver, 'hex') as solver, reference_score
from dbt.stg_backend_data__reference_scores
where auction_id = any(%(ids)s)
"""

DB_SURPLUS_SQL = """
select auction_id, solution_uid, encode(order_uid, 'hex') as order_uid,
       order_surplus_atoms_in_surplus_token as surplus
from dbt.int_backend_data__proposed_solution_data
where auction_id = any(%(ids)s)
"""


def auctions_in_window(conn, start: str, end: str) -> list[tuple[int, int]]:
    """Auction ids with at least one bid in `[start, end)`, with their block deadlines.

    This is the same auction universe the dbt reward models use. Auction ids are sparse
    — do not assume the returned range is contiguous.
    """
    rows = fetch(conn, WINDOW_SQL, {"start": start, "end": end})
    return [(row["auction_id"], row["block_deadline"]) for row in rows]


def load_auctions(
    conn, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> Iterator[AuctionBundle]:
    """Yield one `AuctionBundle` per auction, in the order given.

    Queries are batched over chunks of auction ids; auctions with no recorded solutions
    are skipped silently, since they carry no bids to arbitrate.
    """
    for chunk in chunked(list(auction_ids), chunk_size):
        ids = list(chunk)
        params = {"ids": ids}

        contexts = {
            row["auction_id"]: row for row in fetch(conn, AUCTIONS_SQL, params)
        }
        solution_rows = fetch(conn, SOLUTIONS_SQL, params)
        execution_rows = fetch(conn, EXECUTIONS_SQL, params)
        price_rows = fetch(conn, PRICES_SQL, params)
        reference_rows = fetch(conn, REFERENCE_SCORES_SQL, params)

        prices: dict[int, dict[str, int]] = {}
        for row in price_rows:
            prices.setdefault(row["auction_id"], {})[row["token"]] = as_int(row["price"])

        references: dict[int, dict[str, int]] = {}
        for row in reference_rows:
            references.setdefault(row["auction_id"], {})[row["solver"]] = as_int(
                row["reference_score"]
            )

        executions: dict[tuple[int, int], list[dict]] = {}
        for row in execution_rows:
            executions.setdefault(
                (row["auction_id"], row["solution_uid"]), []
            ).append(row)

        solutions: dict[int, list[dict]] = {}
        for row in solution_rows:
            solutions.setdefault(row["auction_id"], []).append(row)

        for auction_id in ids:
            if auction_id not in solutions:
                continue
            context = contexts.get(auction_id)
            jit_owners = frozenset(context["jit_owners"] if context else [])

            bids = tuple(
                _build_bid(row, executions.get((auction_id, row["uid"]), []), jit_owners)
                for row in solutions[auction_id]
            )

            yield AuctionBundle(
                auction_id=auction_id,
                block_deadline=context["block_deadline"] if context else 0,
                jit_owners=jit_owners,
                native_prices=prices.get(auction_id, {}),
                bids=bids,
                reference_scores=references.get(auction_id, {}),
            )


def _build_bid(row: dict, execution_rows: list[dict], jit_owners: frozenset[str]) -> Bid:
    orders = []
    contributes = {}

    for execution in execution_rows:
        uid = execution["order_uid"]
        if execution["sell_token"] is None:
            # Nothing can be valued without the order's tokens and limits, and guessing
            # would corrupt the pair decomposition silently. Loud failure instead.
            raise MissingOrderError(
                f"auction {row['auction_id']} solution {row['uid']}: "
                f"order {uid} is in neither orders nor jit_orders"
            )

        orders.append(
            Order(
                uid=uid,
                sell_token=execution["sell_token"],
                buy_token=execution["buy_token"],
                sell_amount=as_int(execution["sell_amount"]),
                buy_amount=as_int(execution["buy_amount"]),
                executed_sell=as_int(execution["executed_sell"]),
                executed_buy=as_int(execution["executed_buy"]),
                side=execution["kind"],
                partially_fillable=bool(execution["partially_fillable"]),
            )
        )
        contributes[uid] = bool(execution["in_auction"]) or order_owner(uid) in jit_owners

    return Bid(
        auction_id=row["auction_id"],
        uid=row["uid"],
        solver=row["solver"],
        score=as_int(row["score"]),
        is_winner=bool(row["is_winner"]),
        filtered_out=bool(row["filtered_out"]),
        orders=tuple(orders),
        contributes=contributes,
    )


def load_db_order_surplus(
    conn, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[tuple[int, int, str], int]:
    """`order_surplus_atoms_in_surplus_token` keyed by (auction, solution uid, order).

    An independent check on `valuation.order_surplus`: the dbt model computes the same
    quantity by a different route. Only covers auctions the intermediate model has
    caught up with.
    """
    surplus: dict[tuple[int, int, str], int] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        for row in fetch(conn, DB_SURPLUS_SQL, {"ids": list(chunk)}):
            if row["surplus"] is None:
                continue
            key = (row["auction_id"], row["solution_uid"], row["order_uid"])
            surplus[key] = as_int(row["surplus"])
    return surplus
