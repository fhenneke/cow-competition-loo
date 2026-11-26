"""Load bids, orders and prices from the analytics DB.

Reads staging, not `int_backend_data__proposed_solution_data` (lags; no `filtered_out`).
`proposed_trade_executions.solution_uid` joins `proposed_solutions.uid`, never `.id`.

Each query is written for the EVM tables; `SOLANA_SQL` holds what Solana answers it with,
and `sql_for` picks by the connection's network.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, NamedTuple

from .db import (
    Connection,
    Row,
    as_int,
    cache_dir,
    chunked,
    fetch,
    network_of,
    prefetch,
    settled,
)
from .fees import (
    EMPTY_FEE_CONTEXT,
    FeeContext,
    FeeError,
    Policy,
    PolicyKind,
    Quote,
    quote_fee,
)
from .primitives import (
    SOLANA,
    base58_to_hex,
    hex_to_base58,
    native_decimals,
    native_price_scale,
    usd_per_native,
    usd_reference_tokens,
)
from .rewards import AuctionCaps, SolverReward, Win
from .valuation import Order, Side

CHUNK_SIZE = 200


MECHANISM_START = "2026-10-28"
"""First day replayed faithfully: JIT orders claim no token pairs since v2.385.0, live
2026-10-27."""


class UnsupportedWindowError(Exception):
    """The window starts before `MECHANISM_START`."""


def require_current_mechanism(start: str) -> None:
    if start[:10] < MECHANISM_START:
        raise UnsupportedWindowError(
            f"the window starts {start}, before {MECHANISM_START}: this code replays "
            f"only the current mechanism (JIT orders ignored) and reads "
            f"prices only from the competition_auctions arrays"
        )


class MissingOrderError(Exception):
    """A traded auction order is not in `stg_backend_data__orders`."""


class MissingAuctionContextError(Exception):
    """An auction has proposed solutions but no `competition_auctions` row.

    Fatal: `EXECUTIONS_SQL` inner-joins that table, so its bids would silently vanish."""


class MissingPricesError(Exception):
    """An auction with bids has NULL price arrays in `competition_auctions`
    ([docs](../docs/analytics-db.md#native-prices-the-competition_auctions-arrays))."""


class SolverResolutionError(Exception):
    """`--solver` did not name exactly one solver that bid in the window."""


class WindowNotCoveredError(Exception):
    """The block times or the model of auctions with a bid stop before the end of the
    window, or of a paid accounting period it touches, so `WINDOW_SQL` would silently
    return it shorter
    ([docs](../docs/analytics-db.md#the-window-needs-block-times-and-the-auction-model-up-to-its-end))."""


@dataclass(frozen=True)
class Settlement:
    """On-chain status of one winning solution; a landed batch executes exactly its
    proposed amounts
    ([docs](../docs/analytics-db.md#a-settled-proposal-is-exact--the-only-divergence-is-settling-at-all)).
    """

    landed: bool
    in_time: bool

    @property
    def counts_as_executed(self) -> bool:
        """A late batch counts as failed with zero surplus, though its orders filled, so
        the surplus and reward sides agree on which winners delivered."""
        return self.in_time


@dataclass(frozen=True)
class SolverMatch:
    """Catalogue entry matching `--solver`, with its activity in the window."""

    address: str
    name: str
    environment: str
    active: bool
    solutions: int
    auctions_bid: int
    winning_solutions: int


@dataclass(frozen=True)
class Bid:
    auction_id: int
    uid: int
    solver: str
    score: int
    is_winner: bool
    filtered_out: bool
    orders: tuple[Order, ...]
    """Auction orders only; JIT orders are inert."""


@dataclass(frozen=True)
class AuctionBundle:
    auction_id: int
    native_prices: dict[str, int]
    bids: tuple[Bid, ...]
    """Ordered by `uid`: best-to-worst as recorded, which reference scores depend on."""
    reference_scores: dict[str, int]
    """Recorded; winners only."""
    block_deadline: int = 0
    fee_context: FeeContext = EMPTY_FEE_CONTEXT
    """`EMPTY_FEE_CONTEXT` when fees were not loaded: fees read unrecorded, not zero."""


_BLOCK_TIMES = "dbt.stg_rpc_data__block_timestamp"


def _first_block_at(param: str) -> str:
    """The first block whose time is at or after `%(param)s`, found by bisecting the block
    numbers: `min(block_number) where time >= …` reads every block from the table's start
    (12 s on arbitrum-one, 100M blocks), the bisection ~30 index lookups. Equal because a
    block's time never falls below its predecessor's (no inversion since 2026-09-01 on
    mainnet, base, arbitrum-one or bnb); missing block numbers are skipped over."""
    return f"""(
    with recursive search (lo, hi) as (
        select (select min(block_number) from {_BLOCK_TIMES}),
               (select max(block_number) from {_BLOCK_TIMES}) + 1
      union all
        select case when coalesce(probe.time >= %({param})s, true) then search.lo else mid.b + 1 end,
               case when coalesce(probe.time >= %({param})s, true) then mid.b else search.hi end
        from search
        cross join lateral (select search.lo + (search.hi - search.lo) / 2 as b) mid
        left join lateral (
            select time from {_BLOCK_TIMES}
            where block_number >= mid.b order by block_number limit 1
        ) probe on true
        where search.lo < search.hi
    )
    select (
        select block_number from {_BLOCK_TIMES}
        where block_number >= search.lo order by block_number limit 1
    )
    from search where search.lo = search.hi
)"""


WINDOW_BLOCKS = f"""between {_first_block_at("start")}
  and (
    select max(block_number) from {_BLOCK_TIMES}
    where block_number < coalesce(
        {_first_block_at("end")}, (select max(block_number) from {_BLOCK_TIMES}) + 1
    )
)"""
"""`between` the window's first block and its last: the first block at or after `start`,
and the last before `end` (the block before the first at or after it)."""


WINDOW_SQL = f"""
select distinct auction_id
from dbt.pre_stg__orders_per_auction_with_at_least_one_bid
where block_deadline {WINDOW_BLOCKS}
order by auction_id
"""

# The two tables `WINDOW_SQL` maps the window through sync apart from staging, and past
# their last row it finds nothing. Block times hold every block, so the last block at or
# before a deadline is the deadline's own. The model holds only auctions with a bid,
# which a quiet chain sees hours apart, so it is checked against staging instead.
COVERAGE_SQL = """
with last_block as (
    select time
    from dbt.stg_rpc_data__block_timestamp
    order by block_number desc
    limit 1
),
last_modelled as (
    select auction_id, block_deadline
    from dbt.pre_stg__orders_per_auction_with_at_least_one_bid
    order by auction_id desc
    limit 1
),
first_unmodelled as (
    -- The first auction with a bid in staging that the model does not hold yet.
    select ca.auction_id, ca.block_deadline
    from dbt.stg_backend_data__competition_auctions ca
    where ca.auction_id = (
        select min(pte.auction_id)
        from dbt.stg_backend_data__proposed_trade_executions pte
        where pte.auction_id > coalesce((select auction_id from last_modelled), -1)
    )
),
deadlines as (
    select
        (select b.time from dbt.stg_rpc_data__block_timestamp b
         where b.block_number <= (select block_deadline from last_modelled)
         order by b.block_number desc limit 1) as modelled,
        (select b.time from dbt.stg_rpc_data__block_timestamp b
         where b.block_number <= (select block_deadline from first_unmodelled)
         order by b.block_number desc limit 1) as unmodelled
)
select
    (select time from last_block)                             as blocks_end,
    coalesce((select time from last_block) >= %(end)s, false) as blocks_cover,
    (select auction_id from last_modelled)                    as model_auction,
    d.modelled                                                as model_end,
    (select auction_id from first_unmodelled)                 as unmodelled_auction,
    d.unmodelled                                              as unmodelled_deadline,
    coalesce(d.unmodelled >= %(end)s, true)                   as model_covers
from deadlines d
"""

SOLUTIONS_SQL = """
select auction_id, uid, encode(solver, 'hex') as solver, score, is_winner, filtered_out
from dbt.stg_backend_data__proposed_solutions
where auction_id = any(%(ids)s)
order by auction_id, uid
"""

# Auction orders only; JIT orders are inert. Exact only while
# `surplus_capturing_jit_order_owners` is empty, which is not read. Membership is tested
# once per (auction, order), not per execution: each test reads the auction's whole
# `order_uids` array (~300 kB on mainnet), which made this the costliest query of a run.
EXECUTIONS_SQL = """
with pairs as materialized (
    select distinct auction_id, order_uid
    from dbt.stg_backend_data__proposed_trade_executions
    where auction_id = any(%(ids)s)
),
members as materialized (
    select p.auction_id, p.order_uid
    from pairs p
    join dbt.stg_backend_data__competition_auctions ca on ca.auction_id = p.auction_id
    where p.order_uid = any(ca.order_uids)
)
select
    pte.auction_id,
    pte.solution_uid,
    encode(pte.order_uid, 'hex')    as order_uid,
    pte.executed_sell,
    pte.executed_buy,
    encode(o.sell_token, 'hex')     as sell_token,
    encode(o.buy_token, 'hex')      as buy_token,
    o.sell_amount,
    o.buy_amount,
    o.kind::text                    as kind,
    o.partially_fillable
from dbt.stg_backend_data__proposed_trade_executions pte
join members m on m.auction_id = pte.auction_id and m.order_uid = pte.order_uid
left join dbt.stg_backend_data__orders o
    on o.uid = pte.order_uid
where pte.auction_id = any(%(ids)s)
"""

AUCTIONS_SQL = """
select
    auction_id,
    block_deadline,
    price_tokens is null             as unpriced,
    fast_path_quote_id is not null   as fast_path
from dbt.stg_backend_data__competition_auctions
where auction_id = any(%(ids)s)
"""

# The `{auction_prices}` relation, restricted to `%(ids)s`; filled in by `fetch_prices`.
_ARRAY_PRICES = """
    select ca.auction_id, ap.token, ap.price
    from dbt.stg_backend_data__competition_auctions ca
    cross join lateral unnest(ca.price_tokens, ca.price_values) as ap (token, price)
    where ca.auction_id = any(%(ids)s)
"""

# Sell tokens too: the price-sanity check values each trade through both tokens. Each
# (auction, token) is looked up by its position in the price arrays; unnesting them
# instead walks the whole price list once per traded token.
PRICES_SQL = """
with traded as materialized (
    select distinct pte.auction_id, t.token
    from (
        select distinct auction_id, order_uid
        from dbt.stg_backend_data__proposed_trade_executions
        where auction_id = any(%(ids)s)
    ) pte
    join dbt.stg_backend_data__orders o on o.uid = pte.order_uid
    cross join lateral (values (o.sell_token), (o.buy_token)) as t (token)
)
select t.auction_id, encode(t.token, 'hex') as token, ca.price_values[p.position] as price
from traded t
join dbt.stg_backend_data__competition_auctions ca on ca.auction_id = t.auction_id
-- `offset 0` keeps the position computed once, not once per use
cross join lateral (
    select array_position(ca.price_tokens, t.token) as position offset 0
) as p
where p.position is not null
"""

FEE_POLICIES_SQL = """
select
    auction_id,
    encode(order_uid, 'hex') as order_uid,
    kind::text                as kind,
    surplus_factor,
    surplus_max_volume_factor,
    volume_factor,
    price_improvement_factor,
    price_improvement_max_volume_factor
from dbt.stg_backend_data__fee_policies
where auction_id = any(%(ids)s)
order by auction_id, order_uid, application_order
"""

# Every order proposed in the auctions, JIT ones too (they are never looked up). Non-zero
# counts only; an absent order has zero partners. The orders as an array, not `in (…)`:
# with the subquery the planner scans all orders (270 ms a chunk on mainnet, not 8).
PARTNER_COUNTS_SQL = """
select
    encode(o.uid, 'hex')                        as order_uid,
    array_length(ad.partner_fee_recipients, 1)  as partner_count
from dbt.stg_backend_data__orders o
join dbt.stg_backend_data__app_data ad on ad.contract_app_data = o.app_data
where o.uid = any(array(
    select distinct order_uid
    from dbt.stg_backend_data__proposed_trade_executions
    where auction_id = any(%(ids)s)
))
  and array_length(ad.partner_fee_recipients, 1) > 0
"""

# The quotes of the orders a price-improvement policy is recorded for in the auctions.
QUOTES_SQL = """
select
    encode(order_uid, 'hex') as order_uid,
    sell_amount,
    buy_amount,
    gas_amount,
    gas_price,
    sell_token_price
from dbt.stg_backend_data__order_quotes
where order_uid = any(array(
    select distinct order_uid
    from dbt.stg_backend_data__fee_policies
    where auction_id = any(%(ids)s)
      and kind = 'priceimprovement'
))
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

# One row per winning solution; an unsettled winner has a NULL `tx_hash`, not no row.
SETTLEMENTS_SQL = """
select
    auction_id,
    solution_uid,
    (tx_hash is not null)     as landed,
    is_settled_in_time        as in_time
from dbt.int_backend_data__winning_solutions_with_onchain_status
where auction_id = any(%(ids)s)
"""

# `lower_reward_cap` is only the flat floor: combine with `penalty_cap_native` through
# `rewards.lower_reward_cap` (docs/rewards.md#the-caps).
REWARD_INPUTS_SQL = """
select auction_id, solution_uid, encode(solver, 'hex') as solver, score,
       is_settled_in_time, upper_reward_cap, lower_reward_cap, penalty_cap_native,
       is_excluded
from dbt.int_backend_data__solution_data
where auction_id = any(%(ids)s)
"""

FCT_REWARDS_SQL = """
select auction_id, encode(solver, 'hex') as solver,
       competition_score, observed_score, reference_score, uncapped_reward,
       upper_reward_cap, lower_reward_cap, batch_reward_native
from dbt.fct_solver_rewards_per_auction
where auction_id = any(%(ids)s)
"""

# Query per period, never per block: the table is unindexed. A NULL rate means the period
# is not paid out yet ("not convertible yet"), not an error.
CONVERSION_RATES_SQL = """
select
    min(block_number) as first_block,
    max(block_number) as last_block,
    conversion_rate_cow_to_native
from dbt.int_accounting_period_data__conversion_rates
where block_number between %(first)s and %(last)s
group by accounting_period_start_time, conversion_rate_cow_to_native
"""

USD_PRICES_SQL = """
select ap.auction_id, encode(ap.token, 'hex') as token, ap.price
from {auction_prices} ap
where ap.token = any(%(tokens)s)
"""

# Names match exactly, case-insensitively (`Arc` is a substring of `Arctic`); one name can
# hold several rotated addresses, which go together. "Uncatalogued" is a bucket, not a
# solver: those addresses are named by address.
# Bound `ps.auction_id` by the window's id range (here and in `SOLVERS_SQL`) or the planner
# seq-scans `proposed_solutions`.
ALL_SOLVERS_SQL = f"""
with window_auctions as (
    select distinct auction_id
    from dbt.pre_stg__orders_per_auction_with_at_least_one_bid
    where block_deadline {WINDOW_BLOCKS}
),
bidders as (
    select distinct ps.solver
    from dbt.stg_backend_data__proposed_solutions ps
    join window_auctions w on w.auction_id = ps.auction_id
    where ps.auction_id between (select min(auction_id) from window_auctions)
                            and (select max(auction_id) from window_auctions)
)
select distinct coalesce(s.name, encode(b.solver, 'hex')) as label
from bidders b
left join dbt.dune_data__cow_protocol__solvers s
       on s.address = b.solver and s.name <> 'Uncatalogued'
order by label
"""

# Every catalogue entry with what it bid in the window; `--solver` names are matched
# against it in Python (`solver_matches`), so a run asks once however many it names.
SOLVERS_SQL = f"""
with window_auctions as (
    select distinct auction_id
    from dbt.pre_stg__orders_per_auction_with_at_least_one_bid
    where block_deadline {WINDOW_BLOCKS}
),
activity as (
    select
        ps.solver,
        count(*)                             as solutions,
        count(distinct ps.auction_id)        as auctions_bid,
        count(*) filter (where ps.is_winner) as winning_solutions
    from dbt.stg_backend_data__proposed_solutions ps
    join window_auctions using (auction_id)
    where ps.auction_id between (select min(auction_id) from window_auctions)
                            and (select max(auction_id) from window_auctions)
    group by 1
)
select
    encode(s.address, 'hex')          as address,
    s.name,
    s.environment,
    s.active,
    coalesce(a.solutions, 0)          as solutions,
    coalesce(a.auctions_bid, 0)       as auctions_bid,
    coalesce(a.winning_solutions, 0)  as winning_solutions
from dbt.dune_data__cow_protocol__solvers s
left join activity a on a.solver = s.address
"""


# --- Solana: the same questions, asked of its own tables ([docs](../docs/analytics-db.md#solana))
#
# Its staging holds only auctions with a winner, timed by their deadline slot, so the
# window is read from it directly. There is no fast path, and no fee policy, partner fee,
# quote, block or proposed-solution-data table to read.

SOLANA_WINDOW_SQL = """
select auction_id
from dbt.stg_solana_backend_data__competition_auctions
where auction_timestamp >= %(start)s and auction_timestamp < %(end)s
order by auction_id
"""

_SOLANA_SOLUTIONS_SQL = SOLUTIONS_SQL.replace(
    "stg_backend_data__", "stg_solana_backend_data__"
)

_SOLANA_EXECUTIONS_SQL = EXECUTIONS_SQL.replace(
    "stg_backend_data__", "stg_solana_backend_data__"
)

_SOLANA_AUCTIONS_SQL = """
select
    auction_id,
    deadline_slot                    as block_deadline,
    price_tokens is null             as unpriced,
    false                            as fast_path
from dbt.stg_solana_backend_data__competition_auctions
where auction_id = any(%(ids)s)
"""

# Lamports per atom × 1e9: `fetch_prices` scales them onto the EVM's 1e18 denominator.
_SOLANA_ARRAY_PRICES = """
    select ca.auction_id, ap.token, ap.price
    from dbt.stg_solana_backend_data__competition_auctions ca
    cross join lateral unnest(ca.price_tokens, ca.price_values) as ap (token, price)
    where ca.auction_id = any(%(ids)s)
"""

_SOLANA_PRICES_SQL = PRICES_SQL.replace("stg_backend_data__", "stg_solana_backend_data__")

_SOLANA_REFERENCE_SCORES_SQL = REFERENCE_SCORES_SQL.replace(
    "stg_backend_data__", "stg_solana_backend_data__"
)

_SOLANA_SETTLEMENTS_SQL = """
select
    auction_id,
    solution_uid,
    (tx_signature is not null)  as landed,
    is_settled_in_time          as in_time
from dbt.int_solana_backend_data__winning_solutions_with_onchain_status
where auction_id = any(%(ids)s)
"""

# `lower_reward_cap` is NULL but in no-penalty auctions: Solana has no flat floor.
_SOLANA_REWARD_INPUTS_SQL = REWARD_INPUTS_SQL.replace(
    "int_backend_data__solution_data", "int_solana_backend_data__solution_data"
)

_SOLANA_FCT_REWARDS_SQL = FCT_REWARDS_SQL.replace(
    "fct_solver_rewards_per_auction", "fct_solana_solver_rewards_per_auction"
)

SOLANA_SQL = {
    WINDOW_SQL: SOLANA_WINDOW_SQL,
    SOLUTIONS_SQL: _SOLANA_SOLUTIONS_SQL,
    EXECUTIONS_SQL: _SOLANA_EXECUTIONS_SQL,
    AUCTIONS_SQL: _SOLANA_AUCTIONS_SQL,
    PRICES_SQL: _SOLANA_PRICES_SQL,
    USD_PRICES_SQL: USD_PRICES_SQL,
    REFERENCE_SCORES_SQL: _SOLANA_REFERENCE_SCORES_SQL,
    SETTLEMENTS_SQL: _SOLANA_SETTLEMENTS_SQL,
    REWARD_INPUTS_SQL: _SOLANA_REWARD_INPUTS_SQL,
    FCT_REWARDS_SQL: _SOLANA_FCT_REWARDS_SQL,
}
"""EVM query -> its Solana counterpart, which returns the same columns."""

# The catalogue holds base58 strings, which SQL cannot decode, so solvers are matched in
# Python: the whole catalogue (a few dozen rows) and every window bidder's activity.
SOLANA_CATALOGUE_SQL = """
select name, solver, environment, is_active as active
from dbt.dune_data__solana_solvers
"""

SOLANA_ACTIVITY_SQL = """
with window_auctions as (
    select auction_id
    from dbt.stg_solana_backend_data__competition_auctions
    where auction_timestamp >= %(start)s and auction_timestamp < %(end)s
)
select
    encode(ps.solver, 'hex')             as solver,
    count(*)                             as solutions,
    count(distinct ps.auction_id)        as auctions_bid,
    count(*) filter (where ps.is_winner) as winning_solutions
from dbt.stg_solana_backend_data__proposed_solutions ps
join window_auctions using (auction_id)
where ps.auction_id between (select min(auction_id) from window_auctions)
                        and (select max(auction_id) from window_auctions)
group by 1
"""


class NoSolanaCounterpartError(KeyError):
    """An EVM query was sent to Solana, which has no table to answer it."""


def sql_for(sql: str, network: str) -> str:
    """`sql`, or on Solana its counterpart in `SOLANA_SQL`."""
    if network != SOLANA:
        return sql
    try:
        return SOLANA_SQL[sql]
    except KeyError:
        first = next(line for line in sql.splitlines() if line.strip())
        raise NoSolanaCounterpartError(
            f"Solana has no table for this query ({first.strip()} ...)"
        ) from None


def _on(conn: Connection, sql: str) -> str:
    return sql_for(sql, network_of(conn))


def window_queries(
    start: str, end: str, network: str = "mainnet", *, all_solvers: bool = False
) -> list[tuple[str, dict[str, str] | None]]:
    """The window-level reads every `analyse` makes first, for `db.prefetch`: the solver
    catalogue with its activity, the window's auctions and, for `--all-solvers`, its
    bidders by name."""
    params = {"start": start, "end": end}
    if network == SOLANA:
        return [
            (SOLANA_CATALOGUE_SQL, None),
            (SOLANA_ACTIVITY_SQL, params),
            (SOLANA_WINDOW_SQL, params),
        ]
    bidders = [(ALL_SOLVERS_SQL, params)] if all_solvers else []
    return [*bidders, (SOLVERS_SQL, params), (WINDOW_SQL, params)]


def require_coverage(
    conn: Connection, network: str, end: str, *, what: str | None = None
) -> None:
    """Raise `WindowNotCoveredError` unless the block times and the auction model reach
    `end`, the window's end unless `what` names it. Call it before anything maps a span
    ending at `end` to auctions, so no shortened result is kept in a snapshot. Solana
    reads its window from staging itself, which nothing lags, so it always passes."""
    if network == SOLANA:
        return

    def covers(rows: list[Row]) -> bool:
        # Tables only grow, so a span once covered stays covered.
        return settled(end) and rows[0]["blocks_cover"] and rows[0]["model_covers"]

    (row,) = fetch(conn, COVERAGE_SQL, {"end": end}, final=covers)
    reach = what or f"the window's end {end}"
    if not row["blocks_cover"]:
        raise WindowNotCoveredError(
            f"on {network}, dbt.stg_rpc_data__block_timestamp ends at "
            f"{_moment(row['blocks_end'])}, before {reach}: every auction after it would "
            f"be left out without a word. Rerun once the block times have caught up."
        )
    if not row["model_covers"]:
        raise WindowNotCoveredError(
            f"on {network}, dbt.pre_stg__orders_per_auction_with_at_least_one_bid ends "
            f"at auction {row['model_auction']} (deadline {_moment(row['model_end'])}), "
            f"but staging already holds auction {row['unmodelled_auction']} with bids "
            f"(deadline {_moment(row['unmodelled_deadline'])}), before {reach}: every "
            f"auction after the model's would be left out without a word. Rerun once the "
            f"model has caught up."
        )


def _moment(value: datetime | None) -> str:
    return "no row at all" if value is None else f"{value:%Y-%m-%d %H:%M:%S} UTC"


def auctions_in_window(conn: Connection, start: str, end: str) -> list[int]:
    """Auction ids with at least one bid in `[start, end)`; sparse, not contiguous."""
    rows = fetch(
        conn, _on(conn, WINDOW_SQL), {"start": start, "end": end},
        final=lambda _: settled(end),
    )
    return [row["auction_id"] for row in rows]


def price_query(sql: str, network: str = "mainnet") -> str:
    """`sql` for `network`, its `{auction_prices}` relation filled in from the price
    arrays."""
    relation = _SOLANA_ARRAY_PRICES if network == SOLANA else _ARRAY_PRICES
    return sql_for(sql, network).format(auction_prices=f"({relation})")


def fetch_prices(conn: Connection, sql: str, params: dict[str, Any]) -> list[Row]:
    """Run a query over `{auction_prices}` for the auctions in `params["ids"]`.

    Its `price` column comes back on the 1e18 denominator `price_in_eth` divides by:
    Solana's lamport prices are scaled onto it (`native_price_scale`), which values every
    amount in lamports, exactly as its autopilot does."""
    network = network_of(conn)
    rows = fetch(conn, price_query(sql, network), params)
    scale = native_price_scale(network)
    if scale != 1:
        for row in rows:
            row["price"] = row["price"] * scale
    return rows


def _auction_span(auction_ids: Collection[int]) -> str:
    if len(auction_ids) == 1:
        return f"auction {next(iter(auction_ids))}"
    return f"{len(auction_ids)} auctions in {min(auction_ids)}..{max(auction_ids)}"


def _id_chunks(auction_ids: Sequence[int], chunk_size: int) -> list[dict[str, Any]]:
    """The parameters of one query per chunk of `auction_ids`. A prefetch and the fetch it
    serves must build them alike: the parameters are part of the query's key."""
    return [{"ids": list(chunk)} for chunk in chunked(list(auction_ids), chunk_size)]


def auction_queries(
    auction_ids: Sequence[int],
    network: str = "mainnet",
    chunk_size: int = CHUNK_SIZE,
    with_fees: bool = True,
) -> list[tuple[str, dict[str, Any]]]:
    """The queries `load_auctions` will make on `network`, in its order, for
    `db.prefetch`. Solana has no fee tables to read."""
    wanted = [
        sql_for(AUCTIONS_SQL, network),
        sql_for(SOLUTIONS_SQL, network),
        sql_for(EXECUTIONS_SQL, network),
        price_query(PRICES_SQL, network),
        sql_for(REFERENCE_SCORES_SQL, network),
    ]
    if with_fees and network != SOLANA:
        wanted += [FEE_POLICIES_SQL, PARTNER_COUNTS_SQL, QUOTES_SQL]
    return [(sql, params) for params in _id_chunks(auction_ids, chunk_size) for sql in wanted]


def load_auctions(
    conn: Connection,
    auction_ids: Sequence[int],
    chunk_size: int = CHUNK_SIZE,
    with_fees: bool = True,
    fast_path: list[int] | None = None,
) -> Iterator[AuctionBundle]:
    """Yield an `AuctionBundle` per auction with solutions, in the order given.

    Fast-path auctions are not yielded; their ids go to `fast_path`
    ([why](../docs/winner-selection.md#fast-path-auctions-are-excluded)).
    """
    prefetch(conn, auction_queries(auction_ids, network_of(conn), chunk_size, with_fees))
    for params in _id_chunks(auction_ids, chunk_size):
        ids: list[int] = params["ids"]

        contexts = {
            row["auction_id"]: row for row in fetch(conn, _on(conn, AUCTIONS_SQL), params)
        }
        unpriced = {a for a, context in contexts.items() if context["unpriced"]}
        solution_rows = fetch(conn, _on(conn, SOLUTIONS_SQL), params)
        execution_rows = fetch(conn, _on(conn, EXECUTIONS_SQL), params)
        price_rows = fetch_prices(conn, PRICES_SQL, params)
        reference_rows = fetch(conn, _on(conn, REFERENCE_SCORES_SQL), params)
        fee_contexts = (
            _load_fee_contexts(conn, params, execution_rows, solution_rows)
            if with_fees
            else {}
        )

        prices: dict[int, dict[str, int]] = {}
        for row in price_rows:
            prices.setdefault(row["auction_id"], {})[row["token"]] = as_int(row["price"])

        references: dict[int, dict[str, int]] = {}
        for row in reference_rows:
            references.setdefault(row["auction_id"], {})[row["solver"]] = as_int(
                row["reference_score"]
            )

        executions: dict[tuple[int, int], list[Row]] = {}
        for row in execution_rows:
            key = (row["auction_id"], row["solution_uid"])
            if key in executions:
                executions[key].append(row)
            else:
                executions[key] = [row]

        limits: dict[str, _Limit] = {}
        solutions: dict[int, list[Row]] = {}
        for row in solution_rows:
            if row["auction_id"] in solutions:
                solutions[row["auction_id"]].append(row)
            else:
                solutions[row["auction_id"]] = [row]

        lost = sorted(a for a in unpriced if a in solutions)
        if lost:
            verb = "has" if len(lost) == 1 else "have"
            raise MissingPricesError(
                f"{_auction_span(lost)} {verb} bids but NULL price arrays in "
                f"stg_backend_data__competition_auctions, which begin 2026-09-15"
            )

        for auction_id in ids:
            if auction_id not in solutions:
                continue
            context = contexts.get(auction_id)
            if context is None:
                raise MissingAuctionContextError(
                    f"auction {auction_id} has proposed solutions but no row in "
                    f"stg_backend_data__competition_auctions"
                )
            if context["fast_path"]:
                if fast_path is not None:
                    fast_path.append(auction_id)
                continue

            bids = tuple(
                _build_bid(row, executions.get((auction_id, row["uid"]), []), limits)
                for row in solutions[auction_id]
            )

            yield AuctionBundle(
                auction_id=auction_id,
                native_prices=prices.get(auction_id, {}),
                bids=bids,
                reference_scores=references.get(auction_id, {}),
                block_deadline=as_int(context["block_deadline"]),
                fee_context=fee_contexts.get(auction_id, EMPTY_FEE_CONTEXT),
            )


def _load_fee_contexts(
    conn: Connection,
    params: dict[str, Any],
    execution_rows: list[Row],
    solution_rows: list[Row],
) -> dict[int, FeeContext]:
    """Policies are recorded iff the order is in a non-filtered-out solution."""
    ids: list[int] = params["ids"]
    known: dict[int, set[str]] = {}
    if network_of(conn) == SOLANA:
        # No order carries a fee on Solana: protocol fees are configured but empty and
        # partner fees do not exist (docs/analytics-db.md#solana), so every fee is known
        # to be zero. `validate` would see a fee as a score above the surplus.
        for row in execution_rows:
            known.setdefault(row["auction_id"], set()).add(row["order_uid"])
        return {
            auction_id: FeeContext(
                policies={}, quotes={}, partner_counts={},
                known=frozenset(known.get(auction_id, ())),
            )
            for auction_id in ids
        }
    filtered_out = {
        (row["auction_id"], row["uid"]): bool(row["filtered_out"]) for row in solution_rows
    }
    for row in execution_rows:
        key = (row["auction_id"], row["solution_uid"])
        if not filtered_out.get(key, True):
            known.setdefault(row["auction_id"], set()).add(row["order_uid"])

    # Appending keeps `application_order`, which `policy_fees` walks in reverse.
    collected: dict[int, dict[str, list[Policy]]] = {}
    for row in fetch(conn, FEE_POLICIES_SQL, params):
        kind: PolicyKind = row["kind"]
        by_order = collected.setdefault(row["auction_id"], {})
        by_order.setdefault(row["order_uid"], []).append(
            Policy(
                kind=kind,
                surplus_factor=row["surplus_factor"],
                surplus_max_volume_factor=row["surplus_max_volume_factor"],
                volume_factor=row["volume_factor"],
                price_improvement_factor=row["price_improvement_factor"],
                price_improvement_max_volume_factor=row[
                    "price_improvement_max_volume_factor"
                ],
            )
        )

    policies = {
        auction_id: {uid: tuple(items) for uid, items in by_order.items()}
        for auction_id, by_order in collected.items()
    }

    partner_counts = {
        row["order_uid"]: int(row["partner_count"])
        for row in fetch(conn, PARTNER_COUNTS_SQL, params)
    }

    # The orders with a price-improvement policy in these auctions.
    quotes: dict[str, Quote] = {}
    for row in fetch(conn, QUOTES_SQL, params):
        try:
            fee = quote_fee(row["gas_amount"], row["gas_price"], row["sell_token_price"])
        except FeeError:
            # Left out, so its orders report unknown fees.
            continue
        quotes[row["order_uid"]] = Quote(
            sell_amount=as_int(row["sell_amount"]),
            buy_amount=as_int(row["buy_amount"]),
            fee=fee,
        )

    return {
        auction_id: FeeContext(
            policies=policies.get(auction_id, {}),
            quotes=quotes,
            partner_counts=partner_counts,
            known=frozenset(known.get(auction_id, ())),
        )
        for auction_id in ids
    }


class _Limit(NamedTuple):
    """An order's own fields, joined from `stg_backend_data__orders` by uid: the same in
    every execution of it, so converted once per chunk."""

    sell_token: str
    buy_token: str
    sell_amount: int
    buy_amount: int
    side: Side
    partially_fillable: bool


def _build_bid(row: Row, execution_rows: list[Row], limits: dict[str, _Limit]) -> Bid:
    orders: list[Order] = []

    for execution in execution_rows:
        uid = execution["order_uid"]
        limit = limits.get(uid)
        if limit is None:
            if execution["sell_token"] is None:
                raise MissingOrderError(
                    f"auction {row['auction_id']} solution {row['uid']}: auction order "
                    f"{uid} is not in stg_backend_data__orders"
                )
            limit = limits[uid] = _Limit(
                execution["sell_token"],
                execution["buy_token"],
                as_int(execution["sell_amount"]),
                as_int(execution["buy_amount"]),
                execution["kind"],
                bool(execution["partially_fillable"]),
            )

        orders.append(
            Order(
                uid=uid,
                sell_token=limit.sell_token,
                buy_token=limit.buy_token,
                sell_amount=limit.sell_amount,
                buy_amount=limit.buy_amount,
                executed_sell=as_int(execution["executed_sell"]),
                executed_buy=as_int(execution["executed_buy"]),
                side=limit.side,
                partially_fillable=limit.partially_fillable,
            )
        )

    return Bid(
        auction_id=row["auction_id"],
        uid=row["uid"],
        solver=row["solver"],
        score=as_int(row["score"]),
        is_winner=bool(row["is_winner"]),
        filtered_out=bool(row["filtered_out"]),
        orders=tuple(orders),
    )


def settlement_queries(
    auction_ids: Sequence[int], network: str = "mainnet", chunk_size: int = CHUNK_SIZE
) -> list[tuple[str, dict[str, Any]]]:
    sql = sql_for(SETTLEMENTS_SQL, network)
    return [(sql, params) for params in _id_chunks(auction_ids, chunk_size)]


def load_settlement_outcomes(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, dict[int, Settlement]]:
    """Outcome per winning solution, keyed by auction then `solution_uid`."""
    queries = settlement_queries(auction_ids, network_of(conn), chunk_size)
    prefetch(conn, queries)
    outcomes: dict[int, dict[int, Settlement]] = {}
    for sql, params in queries:
        for row in fetch(conn, sql, params):
            outcomes.setdefault(row["auction_id"], {})[row["solution_uid"]] = Settlement(
                landed=bool(row["landed"]),
                in_time=bool(row["in_time"]),
            )
    return outcomes


@dataclass(frozen=True)
class SolutionCap:
    """Cap inputs of one recorded winning solution. `upper` is 0 for a batch that never
    settled; `lower` (the flat floor) and `excluded` are auction-level."""

    upper: Decimal
    lower: int | None
    """None on Solana, which has no flat floor (`rewards.AuctionCaps`)."""
    excluded: bool
    penalty: Decimal | None = None
    """CIP-87 penalty cap, independent of landing. `None` means none (flat `lower`
    applies); 0 is a real cap."""


@dataclass(frozen=True)
class RewardInputs:
    wins: tuple[Win, ...]
    caps: AuctionCaps


def reward_input_queries(
    auction_ids: Sequence[int], network: str = "mainnet", chunk_size: int = CHUNK_SIZE
) -> list[tuple[str, dict[str, Any]]]:
    sql = sql_for(REWARD_INPUTS_SQL, network)
    return [(sql, params) for params in _id_chunks(auction_ids, chunk_size)]


def _reward_input_rows(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int
) -> Iterator[Row]:
    queries = reward_input_queries(auction_ids, network_of(conn), chunk_size)
    prefetch(conn, queries)
    for sql, params in queries:
        yield from fetch(conn, sql, params)


def load_reward_inputs(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, RewardInputs]:
    """Reward-formula inputs per auction, from the model the rewards mart reads."""
    rows_by_auction: dict[int, list[Row]] = {}
    for row in _reward_input_rows(conn, auction_ids, chunk_size):
        rows_by_auction.setdefault(row["auction_id"], []).append(row)

    inputs: dict[int, RewardInputs] = {}
    for auction_id, rows in rows_by_auction.items():
        lower_caps = {_optional_int(r["lower_reward_cap"]) for r in rows}
        excluded = {bool(r["is_excluded"]) for r in rows}
        if len(lower_caps) != 1 or len(excluded) != 1:
            raise ValueError(
                f"auction {auction_id}: inconsistent lower_reward_cap/is_excluded "
                f"across its winning solutions"
            )
        inputs[auction_id] = RewardInputs(
            wins=tuple(
                Win(
                    solver=r["solver"],
                    score=as_int(r["score"]),
                    settled=bool(r["is_settled_in_time"]),
                    upper_cap=Decimal(r["upper_reward_cap"]),
                    penalty_cap=_optional_decimal(r["penalty_cap_native"]),
                )
                for r in rows
            ),
            caps=AuctionCaps(flat_floor=lower_caps.pop(), excluded=excluded.pop()),
        )
    return inputs


def load_solution_caps(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, dict[int, SolutionCap]]:
    """Cap inputs per recorded winner, keyed by auction then `solution_uid`. Winners
    only: a counterfactual replacement inherits the cap of the slot it displaced."""
    caps: dict[int, dict[int, SolutionCap]] = {}
    for row in _reward_input_rows(conn, auction_ids, chunk_size):
        caps.setdefault(row["auction_id"], {})[row["solution_uid"]] = SolutionCap(
            upper=Decimal(row["upper_reward_cap"]),
            lower=_optional_int(row["lower_reward_cap"]),
            excluded=bool(row["is_excluded"]),
            penalty=_optional_decimal(row["penalty_cap_native"]),
        )
    return caps


def _optional_int(value: Decimal | int | None) -> int | None:
    return None if value is None else as_int(value)


def _optional_decimal(value: Decimal | None) -> Decimal | None:
    """NULL stays `None`: a missing cap means no cap, not 0."""
    return None if value is None else Decimal(value)


def load_reference_scores(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, dict[str, int]]:
    """Recorded reference scores keyed by auction then solver; winners only."""
    references: dict[int, dict[str, int]] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        for row in fetch(conn, _on(conn, REFERENCE_SCORES_SQL), {"ids": list(chunk)}):
            references.setdefault(row["auction_id"], {})[row["solver"]] = as_int(
                row["reference_score"]
            )
    return references


def load_fct_rewards(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, dict[str, SolverReward]]:
    """Ground truth from `fct_solver_rewards_per_auction`, keyed by auction then solver."""
    rewards: dict[int, dict[str, SolverReward]] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        for row in fetch(conn, _on(conn, FCT_REWARDS_SQL), {"ids": list(chunk)}):
            rewards.setdefault(row["auction_id"], {})[row["solver"]] = SolverReward(
                solver=row["solver"],
                competition_score=as_int(row["competition_score"]),
                observed_score=as_int(row["observed_score"]),
                reference_score=as_int(row["reference_score"]),
                uncapped_reward=as_int(row["uncapped_reward"]),
                # NULL on Solana where a win lacks a penalty cap: no floor, no payout.
                upper_cap=_optional_decimal(row["upper_reward_cap"]),
                lower_cap=_optional_decimal(row["lower_reward_cap"]),
                capped_reward=_optional_decimal(row["batch_reward_native"]),
            )
    return rewards


def load_conversion_rates(
    conn: Connection, blocks: Sequence[int], span: tuple[int, int]
) -> dict[int, Decimal | None]:
    """COW->native rate per block; `None` where the period is not snapshotted or synced.

    The query covers `span`, the window's blocks, not just `blocks`, so that it does not
    depend on which auctions the analysis moved."""
    first, last = span

    def rates(rows: list[Row], wanted: Sequence[int]) -> dict[int, Decimal | None]:
        found: dict[int, Decimal | None] = dict.fromkeys(wanted)
        for block in found:
            for row in rows:
                if row["first_block"] <= block <= row["last_block"]:
                    rate = row["conversion_rate_cow_to_native"]
                    if rate is not None:
                        # Double precision: `str` keeps the displayed value, not the binary.
                        found[block] = Decimal(str(rate))
                    break
        return found

    if not blocks:
        return {}
    rows = fetch(
        conn,
        _on(conn, CONVERSION_RATES_SQL),
        {"first": first, "last": last},
        final=lambda rows: all(
            row["conversion_rate_cow_to_native"] is not None for row in rows
        )
        and None not in rates(rows, span).values(),
    )
    return rates(rows, blocks)


def usd_rates_cache(network: str) -> Path:
    """The file caching one network's USD rates, named by its reference tokens so a change
    to them starts a new file. In `db.cache_dir()`."""
    reference = usd_reference_tokens(network)
    key = hashlib.sha256(json.dumps(sorted(reference.items())).encode()).hexdigest()[:12]
    return cache_dir() / f"usd-rates-{network}-{key}.json"


def load_usd_rates(
    conn: Connection,
    auction_ids: Sequence[int],
    network: str = "mainnet",
    chunk_size: int = CHUNK_SIZE,
) -> dict[int, Decimal]:
    """USD per native token per auction, the median over reference stablecoins' prices.
    Auctions with none priced are absent.

    An auction's prices do not change once recorded, so rates are cached on disk and only
    auctions not in the cache are queried. Unpriced auctions are not cached: they are few,
    and an auction not yet in the price table must not be remembered as unpriced."""
    reference = usd_reference_tokens(network)
    path = usd_rates_cache(network)
    cached: dict[int, Decimal] = {}
    if path.exists():
        cached = {int(a): Decimal(r) for a, r in json.loads(path.read_text()).items()}
    wanted = set(auction_ids)
    missing = sorted(wanted - cached.keys())
    token_bytes = [bytes.fromhex(token) for token in reference]
    native = native_decimals(network)
    per_auction: dict[int, list[Decimal]] = {}
    for chunk in chunked(missing, chunk_size):
        ids = list(chunk)
        rows = fetch_prices(conn, USD_PRICES_SQL, {"ids": ids, "tokens": token_bytes})
        for row in rows:
            per_auction.setdefault(row["auction_id"], []).append(
                usd_per_native(as_int(row["price"]), reference[row["token"]], native)
            )
    fetched = {auction_id: median(rates) for auction_id, rates in per_auction.items()}
    if fetched:
        cached.update(fetched)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(f".{os.getpid()}.tmp")
        partial.write_text(json.dumps({str(a): str(r) for a, r in sorted(cached.items())}))
        partial.replace(path)
    return {a: cached[a] for a in wanted if a in cached}


def solver_directory(conn: Connection, start: str, end: str) -> list[SolverMatch]:
    """Every catalogue entry, with how much it bid in `[start, end)`."""
    if network_of(conn) == SOLANA:
        activity = _solana_activity(conn, start, end)
        return [
            SolverMatch(
                address=entry.address,
                name=entry.name,
                environment=entry.environment,
                active=entry.active,
                solutions=activity.get(entry.address, {}).get("solutions", 0),
                auctions_bid=activity.get(entry.address, {}).get("auctions_bid", 0),
                winning_solutions=activity.get(entry.address, {}).get("winning_solutions", 0),
            )
            for entry in _solana_catalogue(conn)
        ]
    rows = fetch(conn, SOLVERS_SQL, {"start": start, "end": end})
    return [SolverMatch(**row) for row in rows]


def solver_matches(
    directory: Sequence[SolverMatch], solver: str, network: str = "mainnet"
) -> list[SolverMatch]:
    """Catalogue entries named `solver` (exactly, ignoring case; never "Uncatalogued") or
    at address `solver` (with or without `0x`, and on Solana in base58 too), most
    solutions first."""
    # Strip a literal prefix: `lstrip("0x")` strips a character set and would eat an
    # address's leading zeroes.
    addresses = {(solver[2:] if solver[:2] in ("0x", "0X") else solver).lower()}
    if network == SOLANA:
        with contextlib.suppress(ValueError):  # not base58: a name
            addresses.add(base58_to_hex(solver))
    name = solver.lower()
    matches = [
        m
        for m in directory
        if (m.name.lower() == name and m.name != "Uncatalogued") or m.address in addresses
    ]
    return sorted(matches, key=lambda m: (-m.solutions, m.name))


def all_solvers(conn: Connection, start: str, end: str) -> list[str]:
    """Every competitor that bid in `[start, end)`, named as `--solver` would name it."""
    if network_of(conn) == SOLANA:
        return _solana_all_solvers(conn, start, end)
    rows = fetch(conn, ALL_SOLVERS_SQL, {"start": start, "end": end})
    return [row["label"] for row in rows]


class _Catalogued(NamedTuple):
    address: str
    """Hex, as the bid tables encode it."""
    name: str
    environment: str
    active: bool


def _solana_catalogue(conn: Connection) -> list[_Catalogued]:
    return [
        _Catalogued(base58_to_hex(row["solver"]), row["name"], row["environment"],
                    bool(row["active"]))
        for row in fetch(conn, SOLANA_CATALOGUE_SQL)
    ]


def _solana_activity(conn: Connection, start: str, end: str) -> dict[str, Row]:
    rows = fetch(conn, SOLANA_ACTIVITY_SQL, {"start": start, "end": end})
    return {row["solver"]: row for row in rows}


def _solana_all_solvers(conn: Connection, start: str, end: str) -> list[str]:
    """`ALL_SOLVERS_SQL` in Python; an uncatalogued address is named in base58."""
    names = {
        entry.address: entry.name
        for entry in _solana_catalogue(conn)
        if entry.name != "Uncatalogued"
    }
    return sorted(
        {names.get(address) or hex_to_base58(address)
         for address in _solana_activity(conn, start, end)}
    )


def resolve_solver(
    directory: Sequence[SolverMatch],
    solver: str,
    start: str,
    end: str,
    network: str = "mainnet",
) -> tuple[frozenset[str], list[SolverMatch]]:
    """Every address of `--solver` that bid in the window, and the matches behind them;
    `directory` is `solver_directory` of the window."""
    matches = solver_matches(directory, solver, network)
    if not matches:
        catalogue = (
            "dune_data__solana_solvers" if network == SOLANA
            else "dune_data__cow_protocol__solvers"
        )
        raise SolverResolutionError(
            f"{solver!r} matches no solver in {catalogue}. Names are matched exactly and "
            f"are case-insensitive; addresses may be given with or without a 0x prefix"
            f"{', or in base58' if network == SOLANA else ''}."
        )

    live = [m for m in matches if m.solutions]
    if not live:
        listed = ", ".join(f"{m.name}/{m.address[:8]} ({m.environment})" for m in matches)
        raise SolverResolutionError(
            f"{solver!r} matches {len(matches)} catalogued address(es) — {listed} — but "
            f"none of them submitted a solution in [{start}, {end}). Check the window."
        )

    return frozenset(m.address for m in live), live


def load_db_order_surplus(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[tuple[int, int, str], int]:
    """`order_surplus_atoms_in_surplus_token` by (auction, solution uid, order): an
    independent check on `valuation.order_surplus`, for auctions the lagging model has."""
    surplus: dict[tuple[int, int, str], int] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        for row in fetch(conn, _on(conn, DB_SURPLUS_SQL), {"ids": list(chunk)}):
            if row["surplus"] is None:
                continue
            key = (row["auction_id"], row["solution_uid"], row["order_uid"])
            surplus[key] = as_int(row["surplus"])
    return surplus
