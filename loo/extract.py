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
from statistics import median
from typing import Iterator, Sequence

from decimal import Decimal

from .db import as_int, chunked, fetch
from .primitives import order_owner, usd_per_native, usd_reference_tokens
from .rewards import SolverReward, Win
from .valuation import Order

CHUNK_SIZE = 200


class MissingOrderError(Exception):
    """A traded order uid is in neither `orders` nor `jit_orders`.

    Not a data glitch but a systematic gap: `jit_orders` is only populated when a batch
    settles, so the JIT orders of solutions that never settled — losing bids, and winners
    that reverted or landed late — are recorded nowhere and their tokens and limits are
    unrecoverable ([docs](../docs/analytics-db.md#jit-orders-are-recorded-only-when-the-batch-settles)).
    Callers that pass `missing_data` to `load_auctions` exclude the whole auction
    transparently instead of dying on it (D17)."""


class MissingAuctionContextError(Exception):
    """An auction has proposed solutions but no `competition_auctions` row.

    Tolerating it would corrupt the run twice over, both times silently: `jit_owners`
    would come out empty, flipping `contributes` for surplus-capturing JIT orders, and
    because `EXECUTIONS_SQL` inner-joins the table every execution would vanish — leaving
    zero-order bids whose empty `winner_pairs` are disjoint from everything, which
    `pick_winners` therefore always selects."""


class SolverResolutionError(Exception):
    """`--solver` did not name exactly one solver that bid in the window."""


@dataclass(frozen=True)
class Settlement:
    """On-chain outcome of one winning solution.

    Only the *status* is read from chain. Executed amounts always come from
    `proposed_trade_executions`, because a batch that lands executes exactly the amounts
    its solution proposed — verified to the atom on every order row of every settled
    winner ([docs](../docs/analytics-db.md#a-settled-proposal-is-exact--the-only-divergence-is-settling-at-all)).
    So the only thing chain data adds is whether it landed at all.

    `landed` and `in_time` come apart: a batch that arrives after its deadline still fills
    its orders, so the user does receive surplus, but the solver earns no reward for it.
    Measured over the M1 window: 8,768 of 10,301 winners landed, of which 16 were late.
    """

    landed: bool
    """Did a settlement transaction appear on chain at all (`tx_hash is not null`)?"""
    in_time: bool
    """Did it arrive before its deadline (`is_settled_in_time`)?"""
    tx_hash: str | None = None

    @property
    def counts_as_executed(self) -> bool:
        """Whether the counterfactual treats this batch as having executed.

        **A late settlement counts as a failure with zero surplus**, even though its orders
        really did fill. Deliberate, and the one place this analysis knowingly departs from
        what happened: a batch the protocol did not reward is not a batch the mechanism can
        be credited with, and the surplus and reward sides of M2/M3 have to agree on which
        winners delivered. The cost is the real surplus of 16 late winners in the window —
        `landed and not in_time` is kept so that cost stays visible rather than becoming
        invisible rounding.
        """
        return self.in_time


@dataclass(frozen=True)
class SolverMatch:
    """One catalogue entry for a `--solver` argument, with its activity in the window."""

    address: str
    name: str
    environment: str
    active: bool
    solutions: int
    auctions_bid: int
    winning_solutions: int


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
    jit_owners: frozenset[str]
    native_prices: dict[str, int]
    bids: tuple[Bid, ...]
    """Ordered by `uid`, i.e. best-to-worst as the autopilot recorded them."""
    reference_scores: dict[str, int]
    """Ground truth from `stg_backend_data__reference_scores`, winners only."""
    block_deadline: int = 0
    """The auction's deadline block. Rewards are converted native -> COW at the
    conversion rate of the accounting period this block falls in."""


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

# The valuation itself only needs buy-token prices — `compute_order_score` converts
# through the buy token for both order sides — but the price-sanity check values each
# trade through *both* tokens and compares, so sell tokens ride along. Restricting to
# traded tokens keeps this from returning every price in every auction (~900 rows each).
PRICES_SQL = """
with traded as (
    select distinct pte.auction_id, t.token
    from dbt.stg_backend_data__proposed_trade_executions pte
    left join dbt.stg_backend_data__orders o on o.uid = pte.order_uid
    left join dbt.stg_backend_data__jit_orders j on j.uid = pte.order_uid
    cross join lateral (values
        (coalesce(o.sell_token, j.sell_token)),
        (coalesce(o.buy_token, j.buy_token))
    ) as t (token)
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

# One row per *winning* solution, always — the model left-joins settlements onto the
# winners, so a winner that never settled is a row with a null `tx_hash` rather than a
# missing row. That distinction is what lets a missing row mean "no data" and fail loudly.
# Verified over the M1 window: 10,301 winners, 10,301 rows, no duplicates, no rows for
# non-winners.
SETTLEMENTS_SQL = """
select
    auction_id,
    solution_uid,
    (tx_hash is not null)     as landed,
    is_settled_in_time        as in_time,
    encode(tx_hash, 'hex')    as tx_hash
from dbt.int_backend_data__winning_solutions_with_onchain_status
where auction_id = any(%(ids)s)
"""

# The reward formula's own inputs, straight from the model `fct_solver_rewards_per_auction`
# builds on: one row per winning solution with its score, settlement flag and caps. Used
# by the M3 gate, which recomputes both rewards from these and compares against the mart —
# so the only thing in the comparison's path is the formula transcription itself.
REWARD_INPUTS_SQL = """
select auction_id, solution_uid, encode(solver, 'hex') as solver, score,
       is_settled_in_time, upper_reward_cap, lower_reward_cap, is_excluded
from dbt.int_backend_data__solution_data
where auction_id = any(%(ids)s)
"""

FCT_REWARDS_SQL = """
select auction_id, encode(solver, 'hex') as solver,
       competition_score, observed_score, reference_score, uncapped_reward,
       upper_reward_cap, batch_reward_native
from dbt.fct_solver_rewards_per_auction
where auction_id = any(%(ids)s)
"""

# `int_accounting_period_data__conversion_rates` carries one row per block with the
# COW->native conversion rate of the accounting period (Tuesday to Tuesday) the block's
# time falls in. The rate is snapshotted from Dune only after the period is paid out, so
# recent blocks have a row with a NULL rate — callers must treat missing-rate as "not
# convertible yet", not as an error.
CONVERSION_RATES_SQL = """
select block_number, conversion_rate_cow_to_native
from dbt.int_accounting_period_data__conversion_rates
where block_number = any(%(blocks)s)
"""

# USD reference prices for display conversion (M4). The analytics DB has no USD price
# table; a major stablecoin's native price in the auction's own price vector implies
# the rate — see `primitives.USD_REFERENCE_TOKENS`.
USD_PRICES_SQL = """
select auction_id, encode(token, 'hex') as token, price
from dbt.stg_backend_data__auction_prices
where auction_id = any(%(ids)s)
  and token = any(%(tokens)s)
"""

# `--solver` takes a name or an address. Names are matched **exactly** (case-insensitively):
# among solvers active in the M1 window `Arc` is a substring of `Arctic` and both bid, so
# any `like '%…%'` match would silently remove two competitors. `Uncatalogued` is excluded
# because it is `coalesce(name, 'Uncatalogued')` — a bucket for addresses missing from the
# Dune seed, not a solver; those must be named by address.
#
# The catalogue holds several addresses per name where a solver has rotated keys, so the
# result is intersected with what actually bid in the window. Rotations never overlap, so
# in practice this collapses to one address; when it does not, all of them are the same
# competitor and all must go — `compute_reference_scores` removes *all* of a solver's
# solutions, so splitting a rotation across two "solvers" would give wrong reference scores.
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
    where ps.solver in (select address from candidates)
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


def auctions_in_window(conn, start: str, end: str) -> list[int]:
    """Auction ids with at least one bid in `[start, end)`.

    This is the same auction universe the dbt reward models use. Auction ids are sparse
    — do not assume the returned range is contiguous.
    """
    rows = fetch(conn, WINDOW_SQL, {"start": start, "end": end})
    return [row["auction_id"] for row in rows]


def load_auctions(
    conn,
    auction_ids: Sequence[int],
    chunk_size: int = CHUNK_SIZE,
    missing_data: list[int] | None = None,
) -> Iterator[AuctionBundle]:
    """Yield one `AuctionBundle` per auction, in the order given.

    Queries are batched over chunks of auction ids; auctions with no recorded solutions
    are skipped silently, since they carry no bids to arbitrate.

    `missing_data` selects the policy for auctions where a traded order is in neither
    order table (D17): when given, the whole auction is skipped and its id appended —
    even one such order leaves the solution's pair claims unknowable, so nothing about
    the auction can be arbitrated faithfully. When `None`, `MissingOrderError`
    propagates, for callers that would rather die than lose coverage silently.
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
            if context is None:
                raise MissingAuctionContextError(
                    f"auction {auction_id} has proposed solutions but no row in "
                    f"stg_backend_data__competition_auctions"
                )
            jit_owners = frozenset(context["jit_owners"])

            try:
                bids = tuple(
                    _build_bid(row, executions.get((auction_id, row["uid"]), []), jit_owners)
                    for row in solutions[auction_id]
                )
            except MissingOrderError:
                if missing_data is None:
                    raise
                missing_data.append(auction_id)
                continue

            yield AuctionBundle(
                auction_id=auction_id,
                jit_owners=jit_owners,
                native_prices=prices.get(auction_id, {}),
                bids=bids,
                reference_scores=references.get(auction_id, {}),
                block_deadline=as_int(context["block_deadline"]),
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


def load_settlement_outcomes(
    conn, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, dict[int, Settlement]]:
    """On-chain outcome per winning solution, keyed by auction then `solution_uid`.

    Only winners have rows, which is all the counterfactual needs: a solution that did not
    win never had a chance to settle. An auction absent from the result had no recorded
    winners at all.
    """
    outcomes: dict[int, dict[int, Settlement]] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        for row in fetch(conn, SETTLEMENTS_SQL, {"ids": list(chunk)}):
            outcomes.setdefault(row["auction_id"], {})[row["solution_uid"]] = Settlement(
                landed=bool(row["landed"]),
                in_time=bool(row["in_time"]),
                tx_hash=row["tx_hash"],
            )
    return outcomes


@dataclass(frozen=True)
class SolutionCap:
    """One recorded winning solution's cap inputs from `int_backend_data__solution_data`.

    `upper` is that solution's contribution to its solver's upper reward cap —
    `scaling_factor × realised protocol fees`, so genuinely fractional (`Decimal`) and
    **0 for a batch that never settled**, since unrealised fees are no fees. `lower`
    and `excluded` are auction-level facts that ride along on every row.
    """

    upper: Decimal
    lower: int
    excluded: bool


@dataclass(frozen=True)
class RewardInputs:
    """Everything the reward formula consumes for one auction, from the DB's record."""

    wins: tuple[Win, ...]
    lower_cap: int
    excluded: bool


def load_reward_inputs(
    conn, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, RewardInputs]:
    """The reward formula's inputs per auction — each winning solution's solver, score,
    settled-in-time flag and cap — from the same model the dbt rewards mart reads."""
    rows_by_auction: dict[int, list[dict]] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        for row in fetch(conn, REWARD_INPUTS_SQL, {"ids": list(chunk)}):
            rows_by_auction.setdefault(row["auction_id"], []).append(row)

    inputs: dict[int, RewardInputs] = {}
    for auction_id, rows in rows_by_auction.items():
        lower_caps = {as_int(r["lower_reward_cap"]) for r in rows}
        excluded = {bool(r["is_excluded"]) for r in rows}
        if len(lower_caps) != 1 or len(excluded) != 1:
            # Both are auction-level facts; two values on one auction means the model
            # changed shape and the comparison below would be against the wrong caps.
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
                )
                for r in rows
            ),
            lower_cap=lower_caps.pop(),
            excluded=excluded.pop(),
        )
    return inputs


def load_solution_caps(
    conn, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, dict[int, SolutionCap]]:
    """Cap inputs per recorded winning solution, keyed by auction then `solution_uid`.

    Winners only, by construction of `int_backend_data__solution_data` — a
    counterfactual replacement has no row here, which is exactly why its cap has to be
    inherited from the slot it displaced."""
    caps: dict[int, dict[int, SolutionCap]] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        for row in fetch(conn, REWARD_INPUTS_SQL, {"ids": list(chunk)}):
            caps.setdefault(row["auction_id"], {})[row["solution_uid"]] = SolutionCap(
                upper=Decimal(row["upper_reward_cap"]),
                lower=as_int(row["lower_reward_cap"]),
                excluded=bool(row["is_excluded"]),
            )
    return caps


def load_reference_scores(
    conn, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, dict[str, int]]:
    """Recorded reference scores keyed by auction then solver — winners only, by
    construction of the table."""
    references: dict[int, dict[str, int]] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        for row in fetch(conn, REFERENCE_SCORES_SQL, {"ids": list(chunk)}):
            references.setdefault(row["auction_id"], {})[row["solver"]] = as_int(
                row["reference_score"]
            )
    return references


def load_fct_rewards(
    conn, auction_ids: Sequence[int], chunk_size: int = CHUNK_SIZE
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
                capped_reward=Decimal(row["batch_reward_native"]),
            )
    return rewards


def load_conversion_rates(
    conn, blocks: Sequence[int], chunk_size: int = CHUNK_SIZE
) -> dict[int, Decimal | None]:
    """COW->native conversion rate per block, `None` where the accounting period has
    not been snapshotted yet. A block missing entirely also maps to `None`."""
    rates: dict[int, Decimal | None] = {block: None for block in blocks}
    for chunk in chunked(list(blocks), chunk_size):
        for row in fetch(conn, CONVERSION_RATES_SQL, {"blocks": list(chunk)}):
            rate = row["conversion_rate_cow_to_native"]
            # The column is double precision; going through `str` keeps the value the
            # DB displays rather than the binary expansion of the float.
            rates[row["block_number"]] = Decimal(str(rate)) if rate is not None else None
    return rates


def load_usd_rates(
    conn, auction_ids: Sequence[int], network: str = "mainnet", chunk_size: int = CHUNK_SIZE
) -> dict[int, Decimal]:
    """USD per native token, per auction, implied by stablecoin native prices.

    Per auction the median across the network's reference stablecoins is taken, so one
    bad price cannot set the rate (the D14 lesson, applied to the one place a price is
    used without a counterparty to cross-check it). Auctions where no reference token
    was priced are simply absent — callers fall back to a window median rather than
    treating that as an error, since USD figures are display only.
    """
    reference = usd_reference_tokens(network)
    token_bytes = [bytes.fromhex(token) for token in reference]
    per_auction: dict[int, list[Decimal]] = {}
    for chunk in chunked(list(auction_ids), chunk_size):
        rows = fetch(conn, USD_PRICES_SQL, {"ids": list(chunk), "tokens": token_bytes})
        for row in rows:
            per_auction.setdefault(row["auction_id"], []).append(
                usd_per_native(as_int(row["price"]), reference[row["token"]])
            )
    return {
        auction_id: median(rates) for auction_id, rates in per_auction.items()
    }


def solver_matches(conn, solver: str, start: str, end: str) -> list[SolverMatch]:
    """Catalogue entries matching `solver`, with how much each one bid in the window."""
    rows = fetch(conn, SOLVER_SQL, {"solver": solver, "start": start, "end": end})
    return [SolverMatch(**row) for row in rows]


def resolve_solver(conn, solver: str, start: str, end: str) -> tuple[frozenset[str], list[SolverMatch]]:
    """Submission addresses to remove for `--solver`, and the matches behind them.

    Returns every matching address that bid in the window — see `SOLVER_SQL` on why a
    single competitor can hold more than one. Raises `SolverResolutionError` when nothing
    matched or when the matches never bid, rather than quietly analysing the removal of a
    solver that was not there: that would produce a clean run of all-zero deltas, which
    reads as a finding.
    """
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
