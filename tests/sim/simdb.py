"""The analytics DB, simulated: a psycopg2-shaped connection serving a `World`.

Each query `loo` sends is recognised by its exact text — the module's own SQL constant,
formatted the way the module formats it — and answered from the world with that query's
semantics: its filters, its joins, its ordering. Queries without an `order by` come back
in a seeded random order, so nothing downstream may depend on row order the SQL does not
promise. An unrecognised query is a failure: the pipeline asked for something the
simulation does not know, which is exactly what a changed query looks like.

`Faults` injects what a real connection does wrong: it drops mid-run, a statement times
out, a table loses rows.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import psycopg2
import psycopg2.errors

from loo import extract, payouts, quotes
from loo.db import Connection

from .world import NATIVE, World, auction_times, reward_rows

Row = dict[str, Any]


@dataclass
class Faults:
    lose_connection_at: int | None = None
    """The n-th query (1-based) finds the connection gone."""
    time_out_at: int | None = None
    """The n-th query is cancelled by the statement timeout."""
    fail_other_at: int | None = None
    """The n-th query fails with an error that is neither of the two."""
    drop_orders: frozenset[str] = frozenset()
    """Orders missing from `stg_backend_data__orders`."""
    drop_settlements: frozenset[int] = frozenset()
    """Auctions whose winners have no settlement rows."""
    drop_contexts: frozenset[int] = frozenset()
    """Auctions missing from `competition_auctions`."""
    unpriced: frozenset[int] = frozenset()
    """Auctions whose price arrays are NULL."""
    drop_prices: frozenset[tuple[int, str]] = frozenset()
    """(auction, token) pairs missing from the price arrays."""
    score_offsets: dict[tuple[int, int], int] = field(default_factory=dict[tuple[int, int], int])
    """(auction, uid) -> what the recorded score is off by."""
    flip_filtered: frozenset[tuple[int, int]] = frozenset()
    """(auction, uid) whose recorded `filtered_out` contradicts the mechanism."""


@dataclass
class SimServer:
    world: World
    faults: Faults = field(default_factory=Faults)
    shuffle_seed: int = 0
    queries: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        self._rng = random.Random(self.shuffle_seed)
        self._handlers = self._routes()

    # --- dispatch ---------------------------------------------------------------------

    def _routes(self) -> dict[str, Callable[[dict[str, Any]], list[Row]]]:
        q = extract.price_query
        network = self.world.network
        orders_sql = quotes.ORDERS_SQL.format(raw_schema="raw_prod_" + network.replace("-", "_"))
        return {
            extract.WINDOW_SQL: self._window,
            extract.AUCTIONS_SQL: self._auctions,
            extract.SOLUTIONS_SQL: self._solutions,
            extract.EXECUTIONS_SQL: self._executions,
            q(extract.PRICES_SQL): self._prices,
            extract.REFERENCE_SCORES_SQL: self._references,
            extract.FEE_POLICIES_SQL: self._policies,
            extract.PARTNER_COUNTS_SQL: self._partner_counts,
            extract.QUOTES_SQL: self._order_quotes,
            extract.SETTLEMENTS_SQL: self._settlements,
            extract.REWARD_INPUTS_SQL: self._reward_inputs,
            extract.FCT_REWARDS_SQL: self._mart,
            extract.CONVERSION_RATES_SQL: self._rates,
            q(extract.USD_PRICES_SQL): self._usd_prices,
            extract.SOLVER_SQL: self._solver,
            extract.ALL_SOLVERS_SQL: self._all_solvers,
            extract.DB_SURPLUS_SQL: self._db_surplus,
            orders_sql: self._quote_orders,
            q(quotes.TOKEN_PRICES_SQL): self._token_medians,
            quotes.ORDER_TRADES_SQL: self._order_trades,
            q(quotes.TRADE_PRICES_SQL): self._trade_prices,
            payouts.PERFORMANCE_SQL: self._performance,
            payouts.RELATIVE_SURPLUS_SQL: lambda p: self._metric(p, self._relative_rows()),
            payouts.RELATIVE_SURPLUS_TOTALS_SQL: lambda p: self._metric_totals(
                p, self._relative_rows()),
            payouts.QUOTE_SHARES_SQL: lambda p: self._metric(p, self._share_rows()),
            payouts.QUOTE_SHARE_TOTALS_SQL: lambda p: self._metric_totals(
                p, self._share_rows()),
            payouts.CONSISTENCY_MART_SQL: self._consistency_mart,
            payouts.QUOTE_MART_SQL: self._quote_mart,
        }

    def query(self, conn: SimConnection, sql: str, params: Any) -> list[Row]:
        self.queries.append(sql)
        n = len(self.queries)
        if self.faults.lose_connection_at == n:
            conn.closed = 2
            raise psycopg2.OperationalError("SSL SYSCALL error: Operation timed out")
        if self.faults.time_out_at == n:
            raise psycopg2.errors.QueryCanceled(
                "canceling statement due to statement timeout"
            )
        if self.faults.fail_other_at == n:
            raise psycopg2.errors.UndefinedColumn("column x does not exist")
        handler = self._handlers.get(sql)
        if handler is None:
            raise AssertionError(f"the simulated DB does not know this query:\n{sql}")
        return handler(dict(params or {}))

    def _unordered(self, rows: list[Row]) -> list[Row]:
        self._rng.shuffle(rows)
        return rows

    # --- the window and its auctions --------------------------------------------------

    def _in_window(self, params: dict[str, Any]) -> list[int]:
        """By deadline time; shadows and bidless ids included (`world.auction_times`)."""
        start = datetime.fromisoformat(params["start"])
        end = datetime.fromisoformat(params["end"])
        return sorted(a for a, when in auction_times(self.world).items() if start <= when < end)

    def _window(self, params: dict[str, Any]) -> list[Row]:
        return [{"auction_id": a} for a in self._in_window(params)]

    def _auctions_by_id(self, params: dict[str, Any]) -> Iterator[Any]:
        ids = set(params["ids"])
        return (a for a in self.world.auctions if a.auction_id in ids)

    def _auctions(self, params: dict[str, Any]) -> list[Row]:
        return self._unordered([
            {
                "auction_id": a.auction_id,
                "block_deadline": Decimal(a.block),
                "unpriced": a.unpriced or a.auction_id in self.faults.unpriced,
                "fast_path": a.fast_path,
            }
            for a in self._auctions_by_id(params)
            if a.auction_id not in self.faults.drop_contexts
        ])

    def _solutions(self, params: dict[str, Any]) -> list[Row]:
        rows = [
            {
                "auction_id": a.auction_id,
                "uid": s.uid,
                "solver": s.solver,
                "score": Decimal(s.score + self.faults.score_offsets.get((a.auction_id, s.uid), 0)),
                "is_winner": s.is_winner,
                "filtered_out": self._filtered(a.auction_id, s.uid, s.filtered_out),
            }
            for a in self._auctions_by_id(params)
            for s in a.solutions
        ]
        return sorted(rows, key=lambda r: (r["auction_id"], r["uid"]))

    def _executions(self, params: dict[str, Any]) -> list[Row]:
        rows: list[Row] = []
        for a in self._auctions_by_id(params):
            if a.auction_id in self.faults.drop_contexts:
                continue  # inner join on competition_auctions
            for s in a.solutions:
                for t in s.trades:
                    if t.uid not in a.orders:
                        continue
                    present = t.uid not in self.faults.drop_orders
                    order = self.world.orders[t.uid]
                    rows.append({
                        "auction_id": a.auction_id,
                        "solution_uid": s.uid,
                        "order_uid": t.uid,
                        "executed_sell": Decimal(t.executed_sell),
                        "executed_buy": t.executed_buy,
                        "sell_token": order.sell_token if present else None,
                        "buy_token": order.buy_token if present else None,
                        "sell_amount": Decimal(order.sell_amount) if present else None,
                        "buy_amount": Decimal(order.buy_amount) if present else None,
                        "kind": order.kind if present else None,
                        "partially_fillable": order.partially_fillable if present else None,
                    })
        return self._unordered(rows)

    def _price_rows(self, auction_ids: set[int]) -> Iterator[tuple[int, str, int]]:
        for a in self.world.auctions:
            if a.auction_id not in auction_ids:
                continue
            if a.unpriced or a.auction_id in self.faults.unpriced:
                continue
            for token, price in a.prices.items():
                if (a.auction_id, token) not in self.faults.drop_prices:
                    yield a.auction_id, token, price

    def _prices(self, params: dict[str, Any]) -> list[Row]:
        ids = set(params["ids"])
        traded = {
            (a.auction_id, token)
            for a in self.world.auctions
            if a.auction_id in ids
            for s in a.solutions
            for t in s.trades
            if t.uid not in self.faults.drop_orders
            for token in (t.sell_token, t.buy_token)
        }
        return self._unordered([
            {"auction_id": a, "token": token, "price": Decimal(price)}
            for a, token, price in self._price_rows(ids)
            if (a, token) in traded
        ])

    def _references(self, params: dict[str, Any]) -> list[Row]:
        return self._unordered([
            {"auction_id": a.auction_id, "solver": solver, "reference_score": Decimal(score)}
            for a in self._auctions_by_id(params)
            for solver, score in a.references.items()
        ])

    def _covered(self, auction_ids: set[int]) -> set[tuple[int, str]]:
        """Fee policies are stored for every order of every solution kept by the filter."""
        return {
            (a.auction_id, t.uid)
            for a in self.world.auctions
            if a.auction_id in auction_ids
            for s in a.solutions
            if not self._filtered(a.auction_id, s.uid, s.filtered_out)
            for t in s.trades
        }

    def _filtered(self, auction_id: int, uid: int, truth: bool) -> bool:
        return truth != ((auction_id, uid) in self.faults.flip_filtered)

    def _policies(self, params: dict[str, Any]) -> list[Row]:
        ids = set(params["ids"])
        rows: list[Row] = []
        for auction_id, uid in sorted(self._covered(ids)):
            for position, policy in enumerate(self.world.orders[uid].policies):
                rows.append({
                    "auction_id": auction_id,
                    "order_uid": uid,
                    "position": position,
                    "kind": policy.kind,
                    "surplus_factor": policy.factor if policy.kind == "surplus" else None,
                    "surplus_max_volume_factor": (
                        policy.max_volume_factor if policy.kind == "surplus" else None
                    ),
                    "volume_factor": policy.factor if policy.kind == "volume" else None,
                    "price_improvement_factor": (
                        policy.factor if policy.kind == "priceimprovement" else None
                    ),
                    "price_improvement_max_volume_factor": (
                        policy.max_volume_factor if policy.kind == "priceimprovement" else None
                    ),
                })
        rows.sort(key=lambda r: (r["auction_id"], r["order_uid"], r["position"]))
        for row in rows:
            del row["position"]
        return rows

    def _uids(self, params: dict[str, Any]) -> set[str]:
        return {bytes(uid).hex() for uid in params["uids"]}

    def _partner_counts(self, params: dict[str, Any]) -> list[Row]:
        return self._unordered([
            {"order_uid": uid, "partner_count": self.world.orders[uid].partners}
            for uid in self._uids(params)
            if uid in self.world.orders and self.world.orders[uid].partners > 0
        ])

    def _order_quotes(self, params: dict[str, Any]) -> list[Row]:
        rows: list[Row] = []
        for uid in self._uids(params):
            order = self.world.orders.get(uid)
            if order is None or order.quote is None:
                continue
            q = order.quote
            rows.append({
                "order_uid": uid,
                "sell_amount": Decimal(q.sell_amount),
                "buy_amount": Decimal(q.buy_amount),
                "gas_amount": q.gas_amount,
                "gas_price": q.gas_price,
                "sell_token_price": q.sell_token_price,
            })
        return self._unordered(rows)

    def _settlements(self, params: dict[str, Any]) -> list[Row]:
        return self._unordered([
            {
                "auction_id": a.auction_id,
                "solution_uid": uid,
                "landed": settlement.landed,
                "in_time": settlement.in_time,
            }
            for a in self._auctions_by_id(params)
            if a.auction_id not in self.faults.drop_settlements
            for uid, settlement in a.settlements.items()
        ])

    def _reward_inputs(self, params: dict[str, Any]) -> list[Row]:
        rows: list[Row] = []
        for a in self._auctions_by_id(params):
            for s in a.winners:
                cap = a.caps[s.uid]
                rows.append({
                    "auction_id": a.auction_id,
                    "solution_uid": s.uid,
                    "solver": s.solver,
                    "score": Decimal(s.score),
                    "is_settled_in_time": a.settlements[s.uid].in_time,
                    "upper_reward_cap": cap.upper,
                    "lower_reward_cap": Decimal(a.flat_lower),
                    "penalty_cap_native": cap.penalty,
                    "is_excluded": a.excluded,
                })
        return self._unordered(rows)

    def _unaccounted(self, auction_id: int) -> bool:
        cut = self.world.fct_cut
        return cut is not None and auction_id > cut

    def _mart(self, params: dict[str, Any]) -> list[Row]:
        rows: list[Row] = []
        for a in self._auctions_by_id(params):
            if self._unaccounted(a.auction_id):
                continue
            for solver, r in a.mart.items():
                off = (a.auction_id, solver) in self.world.mart_off
                rows.append({
                    "auction_id": a.auction_id,
                    "solver": solver,
                    "competition_score": Decimal(r.competition),
                    "observed_score": Decimal(r.observed),
                    "reference_score": Decimal(r.reference),
                    "uncapped_reward": Decimal(r.uncapped + (1 if off else 0)),
                    "upper_reward_cap": r.upper if r.upper is not None else Decimal(0),
                    "lower_reward_cap": r.lower if r.lower is not None else Decimal(0),
                    "batch_reward_native": r.capped if r.capped is not None else Decimal(0),
                })
        return self._unordered(rows)

    def _rates(self, params: dict[str, Any]) -> list[Row]:
        first, last = params["first"], params["last"]
        rows: list[Row] = []
        for low, high, rate in self.world.periods:
            lo, hi = max(low, first), min(high, last)
            if lo <= hi:
                rows.append({"first_block": lo, "last_block": hi,
                             "conversion_rate_cow_to_native": rate})
        return self._unordered(rows)

    def _usd_prices(self, params: dict[str, Any]) -> list[Row]:
        tokens = {bytes(t).hex() for t in params["tokens"]}
        return self._unordered([
            {"auction_id": a, "token": token, "price": Decimal(price)}
            for a, token, price in self._price_rows(set(params["ids"]))
            if token in tokens
        ])

    # --- the accounting tables: rewards paid -----------------------------------------

    def _performance(self, params: dict[str, Any]) -> list[Row]:
        first, last = params["first"], params["last"]
        return self._unordered([
            {"auction_id": a, "solver": solver, "batch_reward_native": capped}
            for a, rows in reward_rows(self.world).items()
            if first <= a <= last and not self._unaccounted(a)
            for solver, (_, capped) in rows.items()
        ])

    def _relative_rows(self) -> Iterator[tuple[int, str, float]]:
        for a, rows in self.world.relative.items():
            for solver, _, value in rows:
                yield a, solver, value

    def _share_rows(self) -> Iterator[tuple[int, str, float]]:
        for a, rows in self.world.shares.items():
            for _, solver, value in rows:
                yield a, solver, value

    def _metric(self, params: dict[str, Any], rows: Iterator[tuple[int, str, float]]
                ) -> list[Row]:
        """`sum(metric)` grouped by (auction, solver) over the id range and the solvers,
        non-zero groups only."""
        first, last = params["first"], params["last"]
        solvers = {bytes(s).hex() for s in params["solvers"]}
        sums: dict[tuple[int, str], float] = {}
        for a, solver, value in rows:
            if first <= a <= last and solver in solvers:
                sums[(a, solver)] = sums.get((a, solver), 0.0) + value
        return self._unordered([
            {"auction_id": a, "solver": solver, "metric": value}
            for (a, solver), value in sums.items()
            if value != 0
        ])

    def _metric_totals(self, params: dict[str, Any], rows: Iterator[tuple[int, str, float]]
                       ) -> list[Row]:
        first, last = params["first"], params["last"]
        sums: dict[str, float] = {}
        for a, solver, value in rows:
            if first <= a <= last:
                sums[solver] = sums.get(solver, 0.0) + value
        return self._unordered([{"solver": s, "metric": v} for s, v in sums.items()])

    def _consistency_mart(self, params: dict[str, Any]) -> list[Row]:
        return self._unordered([
            {"accounting_period": period, "solver": solver, "reward": reward,
             "total_relative_surplus": total}
            for period in params["periods"]
            for solver, (reward, total) in self.world.consistency_mart.get(period, {}).items()
        ])

    def _quote_mart(self, params: dict[str, Any]) -> list[Row]:
        return self._unordered([
            {"accounting_period": period, "solver": solver, "reward": reward,
             "total_quote_budget": budget, "num_quote_reward_orders": orders}
            for period in params["periods"]
            for solver, (reward, budget, orders) in self.world.quote_mart.get(period, {}).items()
        ])

    # --- solvers ----------------------------------------------------------------------

    def _activity(self, params: dict[str, Any]) -> dict[str, tuple[int, set[int], int]]:
        window = set(self._in_window(params))
        activity: dict[str, tuple[int, set[int], int]] = {}
        for a in self.world.auctions:
            if a.auction_id not in window:
                continue
            for s in a.solutions:
                n, auctions, won = activity.get(s.solver, (0, set[int](), 0))
                auctions.add(a.auction_id)
                activity[s.solver] = (n + 1, auctions, won + s.is_winner)
        return activity

    def _solver(self, params: dict[str, Any]) -> list[Row]:
        raw: str = params["solver"]
        hexish = (raw[2:] if raw[:2].lower() == "0x" else raw).lower()
        activity = self._activity(params)
        rows: list[Row] = []
        for solver in self.world.solvers:
            for address in solver.addresses:
                by_name = solver.name.lower() == raw.lower() and solver.name != "Uncatalogued"
                if not by_name and address != hexish:
                    continue
                n, auctions, won = activity.get(address, (0, set[int](), 0))
                rows.append({
                    "address": address,
                    "name": solver.name,
                    "environment": solver.environment,
                    "active": solver.active,
                    "solutions": n,
                    "auctions_bid": len(auctions),
                    "winning_solutions": won,
                })
        rows.sort(key=lambda r: (-r["solutions"], r["name"]))
        return rows

    def _all_solvers(self, params: dict[str, Any]) -> list[Row]:
        bidders = set(self._activity(params))
        labels: set[str] = set()
        for address in bidders:
            solver = self.world.solver_of(address)
            if solver.name == "Uncatalogued":
                labels.add(address)
            else:
                labels.add(solver.name)
        return [{"label": label} for label in sorted(labels)]

    def _db_surplus(self, params: dict[str, Any]) -> list[Row]:
        from .oracle import Refused, user_surplus

        rows: list[Row] = []
        for a in self._auctions_by_id(params):
            for s in a.solutions:
                for t in s.trades:
                    key = (a.auction_id, s.uid, t.uid)
                    try:
                        value: Decimal | None = Decimal(user_surplus(t))
                    except Refused:
                        continue
                    if key in self.world.surplus_model_off:
                        value = value + 1
                    if key in self.world.surplus_model_null:
                        value = None
                    rows.append({"auction_id": a.auction_id, "solution_uid": s.uid,
                                 "order_uid": t.uid, "surplus": value})
        return self._unordered(rows)

    # --- the quote side's Postgres half -----------------------------------------------

    def _executed(self) -> dict[str, list[tuple[int, int, int]]]:
        """Trades on chain: every order of every winner that landed, late or not."""
        trades: dict[str, list[tuple[int, int, int]]] = {}
        for a in self.world.auctions:
            for s in a.winners:
                if not a.settlements[s.uid].landed:
                    continue
                for t in s.trades:
                    trades.setdefault(t.uid, []).append(
                        (a.auction_id, t.executed_sell, t.executed_buy))
        return trades

    def _created_in(self, params: dict[str, Any]) -> list[Any]:
        start = datetime.fromisoformat(params["start"])
        end = datetime.fromisoformat(params["end"])
        return [o for o in self.world.orders.values() if start <= o.created < end]

    def _quote_orders(self, params: dict[str, Any]) -> list[Row]:
        trades = self._executed()
        rows: list[Row] = []
        for order in self._created_in(params):
            q = order.quote
            if q is None:
                continue
            rows.append({
                "order_uid": order.uid,
                "creation_timestamp": order.created,
                "kind": order.kind,
                "partially_fillable": order.partially_fillable,
                "sell_token": order.sell_token,
                "buy_token": order.buy_token,
                "quote_solver": q.solver,
                "quote_sell_amount": Decimal(q.sell_amount),
                "quote_buy_amount": Decimal(q.buy_amount),
                "gas_amount": q.gas_amount,
                "gas_price": q.gas_price,
                "sell_token_price": q.sell_token_price,
                "quote_verified": q.verified,
                "app_code": order.app_code,
                "executed_sell": Decimal(sum(t[1] for t in trades.get(order.uid, []))),
                "quote_id": q.quote_id,
            })
        rows.sort(key=lambda r: r["creation_timestamp"])
        return rows

    def _token_medians(self, params: dict[str, Any]) -> list[Row]:
        tokens = {bytes(t).hex() for t in params["tokens"]}
        by_token: dict[str, list[int]] = {}
        for _, token, price in self._price_rows(set(params["ids"])):
            if token in tokens:
                by_token.setdefault(token, []).append(price)
        return self._unordered([
            {"token": token, "price": float(statistics.median(prices))}
            for token, prices in by_token.items()
        ])

    def _order_trades(self, params: dict[str, Any]) -> list[Row]:
        created = {o.uid for o in self._created_in(params)}
        return self._unordered([
            {"order_uid": uid, "auction_id": auction_id,
             "sell_amount": Decimal(sold), "buy_amount": Decimal(bought)}
            for uid, trades in self._executed().items()
            if uid in created
            for auction_id, sold, bought in trades
        ])

    def _trade_prices(self, params: dict[str, Any]) -> list[Row]:
        wanted = set(zip(params["pair_auctions"],
                         (bytes(t).hex() for t in params["pair_tokens"]), strict=True))
        return self._unordered([
            {"auction_id": a, "token": token, "price": Decimal(price)}
            for a, token, price in self._price_rows(set(params["ids"]))
            if (a, token) in wanted
        ])


class SimCursor:
    def __init__(self, conn: SimConnection) -> None:
        self._conn = conn
        self._rows: list[Row] = []

    def __enter__(self) -> SimCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._rows = self._conn.server.query(self._conn, sql, params)

    def fetchall(self) -> list[Row]:
        return self._rows


class SimConnection:
    """Enough of `psycopg2.extensions.connection` for `loo.db.fetch`."""

    def __init__(self, server: SimServer, network: str) -> None:
        self.server = server
        self.closed = 0
        self.dsn = f"dbname=prod_{network} user=sim host=sim"

    def cursor(self, cursor_factory: object = None) -> SimCursor:
        return SimCursor(self)

    def close(self) -> None:
        self.closed = 1


def connection(server: SimServer) -> Connection:
    return cast(Connection, SimConnection(server, server.world.network))


__all__ = ["NATIVE", "Faults", "SimConnection", "SimServer", "connection"]
