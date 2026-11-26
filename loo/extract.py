"""Load bids, orders and prices from the analytics DB.

Reads staging, not `int_backend_data__proposed_solution_data` (lags; no `filtered_out`).
`proposed_trade_executions.solution_uid` joins `proposed_solutions.uid`, never `.id`.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from .db import Connection, Row, as_int, chunked, fetch
from .fees import (
    EMPTY_FEE_CONTEXT,
    FeeContext,
    FeeError,
    Policy,
    PolicyKind,
    Quote,
    quote_fee,
)
from .primitives import usd_per_native, usd_reference_tokens
from .rewards import SolverReward, Win
from .valuation import Order

CHUNK_SIZE = 200


MECHANISM_START = "2026-10-28"
"""First day replayed faithfully: services#4902 (JIT orders claim no token pairs)."""


class UnsupportedWindowError(Exception):
    """The window starts before `MECHANISM_START`."""


def require_current_mechanism(start: str) -> None:
    if start[:10] < MECHANISM_START:
        raise UnsupportedWindowError(
            f"the window starts {start}, before {MECHANISM_START}: this code replays "
            f"only the current mechanism (JIT orders ignored, services#4902) and reads "
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
    """Auction orders only; JIT orders are inert (services#4902)."""


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


WINDOW_SQL = """
select distinct auction_id
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

# Auction orders only; JIT orders are inert (services#4902). Exact only while
# `surplus_capturing_jit_order_owners` is empty, which is not read.
EXECUTIONS_SQL = """
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
join dbt.stg_backend_data__competition_auctions ca
    on ca.auction_id = pte.auction_id
left join dbt.stg_backend_data__orders o
    on o.uid = pte.order_uid
where pte.auction_id = any(%(ids)s)
  and pte.order_uid = any(ca.order_uids)
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

# Sell tokens too: the price-sanity check values each trade through both tokens.
PRICES_SQL = """
with traded as (
    select distinct pte.auction_id, t.token
    from dbt.stg_backend_data__proposed_trade_executions pte
    join dbt.stg_backend_data__orders o on o.uid = pte.order_uid
    cross join lateral (values (o.sell_token), (o.buy_token)) as t (token)
    where pte.auction_id = any(%(ids)s)
)
select ap.auction_id, encode(ap.token, 'hex') as token, ap.price
from {auction_prices} ap
join traded t on t.auction_id = ap.auction_id and t.token = ap.token
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

# Non-zero counts only; an absent order has zero partners.
PARTNER_COUNTS_SQL = """
select
    encode(o.uid, 'hex')                        as order_uid,
    array_length(ad.partner_fee_recipients, 1)  as partner_count
from dbt.stg_backend_data__orders o
join dbt.stg_backend_data__app_data ad on ad.contract_app_data = o.app_data
where o.uid = any(%(uids)s)
  and array_length(ad.partner_fee_recipients, 1) > 0
"""

QUOTES_SQL = """
select
    encode(order_uid, 'hex') as order_uid,
    sell_amount,
    buy_amount,
    gas_amount,
    gas_price,
    sell_token_price
from dbt.stg_backend_data__order_quotes
where order_uid = any(%(uids)s)
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
# Bound `ps.auction_id` by the window's id range (here and in `SOLVER_SQL`) or the planner
# seq-scans `proposed_solutions`.
ALL_SOLVERS_SQL = """
with window_auctions as (
    select distinct auction_id
    from dbt.pre_stg__orders_per_auction_with_at_least_one_bid
    where block_deadline between
          (select min(block_number) from dbt.stg_rpc_data__block_timestamp where time >= %(start)s)
      and (select max(block_number) from dbt.stg_rpc_data__block_timestamp where time <  %(end)s)
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

SOLVER_SQL = """
with window_auctions as (
    select distinct auction_id
    from dbt.pre_stg__orders_per_auction_with_at_least_one_bid
    where block_deadline between
          (select min(block_number) from dbt.stg_rpc_data__block_timestamp where time >= %(start)s)
      and (select max(block_number) from dbt.stg_rpc_data__block_timestamp where time <  %(end)s)
),
needle as (
    -- `substr(raw, 3)` rather than `ltrim(raw, '0x')`: ltrim strips a character *set*, so
    -- an address of leading zeroes would lose them and silently mismatch.
    select
        %(solver)s::text as raw,
        lower(case
            when %(solver)s like '0x%%' or %(solver)s like '0X%%' then substr(%(solver)s, 3)
            else %(solver)s
        end) as hexish
),
candidates as (
    select s.address, s.name, s.environment, s.active
    from dbt.dune_data__cow_protocol__solvers s, needle n
    where (lower(s.name) = lower(n.raw) and s.name <> 'Uncatalogued')
       or encode(s.address, 'hex') = n.hexish
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
      and ps.solver in (select address from candidates)
    group by 1
)
select
    encode(c.address, 'hex')          as address,
    c.name,
    c.environment,
    c.active,
    coalesce(a.solutions, 0)          as solutions,
    coalesce(a.auctions_bid, 0)       as auctions_bid,
    coalesce(a.winning_solutions, 0)  as winning_solutions
from candidates c
left join activity a on a.solver = c.address
order by coalesce(a.solutions, 0) desc, c.name
"""


def auctions_in_window(conn: Connection, start: str, end: str) -> list[int]:
    """Auction ids with at least one bid in `[start, end)`; sparse, not contiguous."""
    rows = fetch(conn, WINDOW_SQL, {"start": start, "end": end})
    return [row["auction_id"] for row in rows]


def price_query(sql: str) -> str:
    """`sql` with its `{auction_prices}` relation filled in from the price arrays."""
    return sql.format(auction_prices=f"({_ARRAY_PRICES})")


def fetch_prices(conn: Connection, sql: str, params: dict[str, Any]) -> list[Row]:
    """Run a query over `{auction_prices}` for the auctions in `params["ids"]`."""
    return fetch(conn, price_query(sql), params)


def _auction_span(auction_ids: Collection[int]) -> str:
    if len(auction_ids) == 1:
        return f"auction {next(iter(auction_ids))}"
    return f"{len(auction_ids)} auctions in {min(auction_ids)}..{max(auction_ids)}"


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
    for chunk in chunked(list(auction_ids), chunk_size):
        ids = list(chunk)
        params = {"ids": ids}

        contexts = {
            row["auction_id"]: row for row in fetch(conn, AUCTIONS_SQL, params)
        }
        unpriced = {a for a, context in contexts.items() if context["unpriced"]}
        solution_rows = fetch(conn, SOLUTIONS_SQL, params)
        execution_rows = fetch(conn, EXECUTIONS_SQL, params)
        price_rows = fetch_prices(conn, PRICES_SQL, params)
        reference_rows = fetch(conn, REFERENCE_SCORES_SQL, params)
        fee_contexts = (
            _load_fee_contexts(conn, ids, execution_rows, solution_rows)
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
            executions.setdefault(
                (row["auction_id"], row["solution_uid"]), []
            ).append(row)

        solutions: dict[int, list[Row]] = {}
        for row in solution_rows:
            solutions.setdefault(row["auction_id"], []).append(row)

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
                _build_bid(row, executions.get((auction_id, row["uid"]), []))
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
    conn: Connection, ids: list[int], execution_rows: list[Row], solution_rows: list[Row]
) -> dict[int, FeeContext]:
    """Policies are recorded iff the order is in a non-filtered-out solution."""
    known: dict[int, set[str]] = {}
    filtered_out = {
        (row["auction_id"], row["uid"]): bool(row["filtered_out"]) for row in solution_rows
    }
    for row in execution_rows:
        key = (row["auction_id"], row["solution_uid"])
        if not filtered_out.get(key, True):
            known.setdefault(row["auction_id"], set()).add(row["order_uid"])

    # Appending keeps `application_order`, which `policy_fees` walks in reverse.
    collected: dict[int, dict[str, list[Policy]]] = {}
    needs_quote: set[str] = set()
    for row in fetch(conn, FEE_POLICIES_SQL, {"ids": ids}):
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
        if kind == "priceimprovement":
            needs_quote.add(row["order_uid"])

    policies = {
        auction_id: {uid: tuple(items) for uid, items in by_order.items()}
        for auction_id, by_order in collected.items()
    }

    traded = sorted({row["order_uid"] for row in execution_rows})
    partner_counts: dict[str, int] = {}
    for chunk in chunked(traded, 5000):
        uids = [bytes.fromhex(uid) for uid in chunk]
        for row in fetch(conn, PARTNER_COUNTS_SQL, {"uids": uids}):
            partner_counts[row["order_uid"]] = int(row["partner_count"])

    quotes: dict[str, Quote] = {}
    for chunk in chunked(sorted(needs_quote), 5000):
        uids = [bytes.fromhex(uid) for uid in chunk]
        for row in fetch(conn, QUOTES_SQL, {"uids": uids}):
            try:
                fee = quote_fee(
                    row["gas_amount"], row["gas_price"], row["sell_token_price"]
                )
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


def _build_bid(row: Row, execution_rows: list[Row]) -> Bid:
    orders: list[Order] = []

    for execution in execution_rows:
        uid = execution["order_uid"]
        if execution["sell_token"] is None:
            raise MissingOrderError(
                f"auction {row['auction_id']} solution {row['uid']}: auction order "
                f"{uid} is not in stg_backend_data__orders"
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

    return Bid(
        auction_id=row["auction_id"],
        uid=row["uid"],
        solver=row["solver"],
        score=as_int(row["score"]),
        is_winner=bool(row["is_winner"]),
        filtered_out=bool(row["filtered_out"]),
        orders=tuple(orders),
    )


def load_settlement_outcomes(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, dict[int, Settlement]]:
    """Outcome per winning solution, keyed by auction then `solution_uid`."""
    outcomes: dict[int, dict[int, Settlement]] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        for row in fetch(conn, SETTLEMENTS_SQL, {"ids": list(chunk)}):
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
    lower: int
    excluded: bool
    penalty: Decimal | None = None
    """CIP-87 penalty cap, independent of landing. `None` means none (flat `lower`
    applies); 0 is a real cap."""


@dataclass(frozen=True)
class RewardInputs:
    wins: tuple[Win, ...]
    lower_cap: int
    excluded: bool


def _reward_input_rows(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int
) -> Iterator[Row]:
    for chunk in chunked(list(auction_ids), chunk_size):
        yield from fetch(conn, REWARD_INPUTS_SQL, {"ids": list(chunk)})


def load_reward_inputs(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, RewardInputs]:
    """Reward-formula inputs per auction, from the model the rewards mart reads."""
    rows_by_auction: dict[int, list[Row]] = {}
    for row in _reward_input_rows(conn, auction_ids, chunk_size):
        rows_by_auction.setdefault(row["auction_id"], []).append(row)

    inputs: dict[int, RewardInputs] = {}
    for auction_id, rows in rows_by_auction.items():
        lower_caps = {as_int(r["lower_reward_cap"]) for r in rows}
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
            lower_cap=lower_caps.pop(),
            excluded=excluded.pop(),
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
            lower=as_int(row["lower_reward_cap"]),
            excluded=bool(row["is_excluded"]),
            penalty=_optional_decimal(row["penalty_cap_native"]),
        )
    return caps


def _optional_decimal(value: Decimal | None) -> Decimal | None:
    """NULL stays `None`: a missing cap means no cap, not 0."""
    return None if value is None else Decimal(value)


def load_reference_scores(
    conn: Connection, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, dict[str, int]]:
    """Recorded reference scores keyed by auction then solver; winners only."""
    references: dict[int, dict[str, int]] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        for row in fetch(conn, REFERENCE_SCORES_SQL, {"ids": list(chunk)}):
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
        for row in fetch(conn, FCT_REWARDS_SQL, {"ids": list(chunk)}):
            rewards.setdefault(row["auction_id"], {})[row["solver"]] = SolverReward(
                solver=row["solver"],
                competition_score=as_int(row["competition_score"]),
                observed_score=as_int(row["observed_score"]),
                reference_score=as_int(row["reference_score"]),
                uncapped_reward=as_int(row["uncapped_reward"]),
                upper_cap=Decimal(row["upper_reward_cap"]),
                lower_cap=Decimal(row["lower_reward_cap"]),
                capped_reward=Decimal(row["batch_reward_native"]),
            )
    return rewards


def load_conversion_rates(
    conn: Connection, blocks: Sequence[int]
) -> dict[int, Decimal | None]:
    """COW->native rate per block; `None` where the period is not snapshotted or synced."""
    rates: dict[int, Decimal | None] = {block: None for block in blocks}
    if not rates:
        return rates
    periods = [
        (row["first_block"], row["last_block"], row["conversion_rate_cow_to_native"])
        for row in fetch(
            conn, CONVERSION_RATES_SQL, {"first": min(rates), "last": max(rates)}
        )
    ]
    for block in rates:
        for first, last, rate in periods:
            if first <= block <= last:
                if rate is not None:
                    # Double precision: `str` keeps the displayed value, not the binary.
                    rates[block] = Decimal(str(rate))
                break
    return rates


def usd_rates_cache(network: str) -> Path:
    """The file caching one network's USD rates, named by its reference tokens so a change
    to them starts a new file. In `$LOO_CACHE_DIR`, default `~/.cache/loo`."""
    reference = usd_reference_tokens(network)
    key = hashlib.sha256(json.dumps(sorted(reference.items())).encode()).hexdigest()[:12]
    root = Path(os.environ.get("LOO_CACHE_DIR") or Path.home() / ".cache" / "loo")
    return root / f"usd-rates-{network}-{key}.json"


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
    per_auction: dict[int, list[Decimal]] = {}
    for chunk in chunked(missing, chunk_size):
        ids = list(chunk)
        rows = fetch_prices(conn, USD_PRICES_SQL, {"ids": ids, "tokens": token_bytes})
        for row in rows:
            per_auction.setdefault(row["auction_id"], []).append(
                usd_per_native(as_int(row["price"]), reference[row["token"]])
            )
    fetched = {auction_id: median(rates) for auction_id, rates in per_auction.items()}
    if fetched:
        cached.update(fetched)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(f".{os.getpid()}.tmp")
        partial.write_text(json.dumps({str(a): str(r) for a, r in sorted(cached.items())}))
        partial.replace(path)
    return {a: cached[a] for a in wanted if a in cached}


def solver_matches(conn: Connection, solver: str, start: str, end: str) -> list[SolverMatch]:
    """Catalogue entries matching `solver`, with how much each one bid in the window."""
    rows = fetch(conn, SOLVER_SQL, {"solver": solver, "start": start, "end": end})
    return [SolverMatch(**row) for row in rows]


def all_solvers(conn: Connection, start: str, end: str) -> list[str]:
    """Every competitor that bid in `[start, end)`, named as `--solver` would name it."""
    rows = fetch(conn, ALL_SOLVERS_SQL, {"start": start, "end": end})
    return [row["label"] for row in rows]


def resolve_solver(
    conn: Connection, solver: str, start: str, end: str
) -> tuple[frozenset[str], list[SolverMatch]]:
    """Every address of `--solver` that bid in the window, and the matches behind them."""
    matches = solver_matches(conn, solver, start, end)
    if not matches:
        raise SolverResolutionError(
            f"{solver!r} matches no solver in dune_data__cow_protocol__solvers. Names are "
            f"matched exactly and are case-insensitive; addresses may be given with or "
            f"without a 0x prefix."
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
        for row in fetch(conn, DB_SURPLUS_SQL, {"ids": list(chunk)}):
            if row["surplus"] is None:
                continue
            key = (row["auction_id"], row["solution_uid"], row["order_uid"])
            surplus[key] = as_int(row["surplus"])
    return surplus
