"""Rewards recorded as paid to a solver over a window: performance, consistency, quote.

Performance rewards are per auction (`fct_solver_rewards_per_auction.batch_reward_native`).
Consistency and quote rewards are weekly, and each is linear in a per-auction metric with
period-level factors, so the window's part of a period reward is exactly

    reward(p, s) × Σ_{a ∈ window ∩ p} metric(a, s) / Σ_{a ∈ p} metric(a, s)

with the metric the solver's relative surplus (consistency) or its quote reward share
(quote), both summed from the int tables the marts read
([docs](../docs/rewards.md#rewards-paid-to-a-solver-recorded)). These are payouts of
record over every auction of the window, independent of `loo`'s valuation, and not a
counterfactual: removing a solver redistributes the two weekly budgets rather than saving
them.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext
from typing import Any, Literal

from . import extract
from .db import Connection, Row, chunked, fetch

# Performance rewards are ~40-digit numerics; the default 28-digit context rounds sums.
getcontext().prec = 78

Component = Literal["performance", "consistency", "quote"]
COMPONENTS: tuple[Component, ...] = ("performance", "consistency", "quote")
Weekly = Literal["consistency", "quote"]
WEEKLY: tuple[Weekly, ...] = ("consistency", "quote")

CHUNK_SIZE = 5000
"""Window auctions per per-auction query: ~10 bidders' metric rows per mainnet auction."""

RECONCILE_TOLERANCE = Decimal("1e-9")
"""Relative. Int-table sums and mart totals are sums of doubles in different orders
(worst seen 1e-14, mainnet 2026-11-10..17); a rebuild moves them by far more."""

DESCRIPTION = (
    "rewards recorded as paid to the solver over every auction in the window (price "
    "suspects and fast path included), gross of the service fee: performance per "
    "auction; consistency and quote as the window's pro-rata part of each weekly reward. "
    "Recorded amounts, not counterfactual deltas"
)

# Every auction of the window, all solvers: the rows also say which auctions are accounted.
PERFORMANCE_SQL = """
select auction_id, encode(solver, 'hex') as solver, batch_reward_native
from dbt.fct_solver_rewards_per_auction
where auction_id between %(first)s and %(last)s
"""

RELATIVE_SURPLUS_SQL = """
select auction_id, encode(solver, 'hex') as solver, sum(relative_surplus) as metric
from dbt.int_backend_data__solver_relative_surplus_on_executed_orders
where auction_id between %(first)s and %(last)s
  and solver = any(%(solvers)s)
group by auction_id, solver
having sum(relative_surplus) <> 0
"""

# Auction ids rise with `block_deadline`, so a period's id range holds exactly its auctions.
RELATIVE_SURPLUS_TOTALS_SQL = """
select encode(solver, 'hex') as solver, sum(relative_surplus) as metric
from dbt.int_backend_data__solver_relative_surplus_on_executed_orders
where auction_id between %(first)s and %(last)s
group by solver
"""

QUOTE_SHARES_SQL = """
select auction_id, encode(solver, 'hex') as solver, sum(quote_reward_share) as metric
from dbt.int_quote_data__quote_reward_shares
where auction_id between %(first)s and %(last)s
  and solver = any(%(solvers)s)
group by auction_id, solver
having sum(quote_reward_share) <> 0
"""

QUOTE_SHARE_TOTALS_SQL = """
select encode(solver, 'hex') as solver, sum(quote_reward_share) as metric
from dbt.int_quote_data__quote_reward_shares
where auction_id between %(first)s and %(last)s
group by solver
"""

CONSISTENCY_MART_SQL = """
select accounting_period, encode(solver, 'hex') as solver,
       consistency_reward_native as reward, total_relative_surplus
from dbt.fct_consistency_rewards_per_solver_and_accounting_period
where accounting_period = any(%(periods)s)
"""

QUOTE_MART_SQL = """
select accounting_period, encode(solver, 'hex') as solver,
       quote_reward_native as reward, total_quote_budget, num_quote_reward_orders
from dbt.fct_quote_rewards_per_solver_and_accounting_period
where accounting_period = any(%(periods)s)
"""

_METRIC_SQL: dict[Weekly, tuple[str, str]] = {
    "consistency": (RELATIVE_SURPLUS_SQL, RELATIVE_SURPLUS_TOTALS_SQL),
    "quote": (QUOTE_SHARES_SQL, QUOTE_SHARE_TOTALS_SQL),
}


# --- accounting periods: Tuesday 00:00 UTC to Tuesday, by the auction's block_deadline


def _moment(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment


def accounting_periods(start: str, end: str) -> list[tuple[datetime, datetime]]:
    """The periods overlapping `[start, end)`, in order."""
    first, last = _moment(start), _moment(end)
    day = first.replace(hour=0, minute=0, second=0, microsecond=0)
    begin = day - timedelta(days=(day.weekday() - 1) % 7)  # Tuesday is weekday 1
    periods: list[tuple[datetime, datetime]] = []
    while begin < last:
        periods.append((begin, begin + timedelta(days=7)))
        begin += timedelta(days=7)
    return periods


def period_label(begin: datetime, end: datetime) -> str:
    """As the weekly marts spell `accounting_period`."""
    return f"{begin:%Y-%m-%d %H:%M:%S} - {end:%Y-%m-%d %H:%M:%S}"


def _sql_time(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S}"


_ZERO = Decimal(0)
_WEI = Decimal(1)


def _decimal(value: float | Decimal | None) -> Decimal:
    """Double precision through `str`, as `extract.load_conversion_rates` does."""
    if value is None:
        return Decimal(0)
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= RECONCILE_TOLERANCE * max(abs(a), abs(b))


# --- what one report carries


@dataclass(frozen=True)
class Mismatch:
    """A full-period int-table sum that disagrees with the mart's own total: one of the two
    was rebuilt after the other. The window share stays defined (both its sums come from
    the int table) and the mart's reward is still what was paid; the flag says the split
    of that reward over the period's auctions is not the one the mart saw."""

    component: Weekly
    period: str
    solver: str
    int_table: Decimal
    """Consistency: Σ relative surplus. Quote: budget × Σ share / reward orders."""
    mart: Decimal
    """Consistency: `total_relative_surplus`. Quote: `quote_reward_native`."""


@dataclass(frozen=True)
class PeriodPart:
    """One accounting period's part of a window, for one scenario."""

    period: str
    auctions: int
    """Window auctions with bids in this period."""
    consistency_accounted: bool
    quote_accounted: bool
    """The weekly mart has the period: it is paid out."""
    consistency_period: Decimal
    consistency_window: Decimal
    quote_period: Decimal
    quote_window: Decimal
    """The scenario's mart reward for the whole period, and the window's part of it."""


@dataclass(frozen=True)
class RecordedRewards:
    """One scenario's recorded rewards over a window; amounts are native wei."""

    auctions: int
    """Window auctions with bids: the ones a reward row is expected for."""
    per_auction: Mapping[int, tuple[Decimal, Decimal, Decimal]]
    """auction -> (performance, consistency, quote), auctions with a non-zero amount only;
    weekly parts are rounded to the wei."""
    unaccounted: Mapping[Component, int]
    """Auctions with bids whose reward is not recorded yet: no performance row, or a period
    missing from the weekly mart. Never counted as zero."""
    unaccounted_any: int
    periods: tuple[PeriodPart, ...] = ()
    mismatches: tuple[Mismatch, ...] = ()

    def amounts(self, component: Component) -> dict[int, Decimal]:
        index = COMPONENTS.index(component)
        return {
            auction: values[index]
            for auction, values in self.per_auction.items()
            if values[index]
        }

    def amount(self, component: Component) -> Decimal:
        index = COMPONENTS.index(component)
        return sum((values[index] for values in self.per_auction.values()), Decimal(0))

    @property
    def total(self) -> Decimal:
        return sum((self.amount(c) for c in COMPONENTS), Decimal(0))

    def totals_by_auction(self) -> dict[int, Decimal]:
        return {a: sum(values, Decimal(0)) for a, values in self.per_auction.items()}

    def to_payload(self) -> dict[str, Any]:
        return {
            "description": DESCRIPTION,
            "auctions": self.auctions,
            **{c: str(self.amount(c)) for c in COMPONENTS},
            "total": str(self.total),
            "auctions_unaccounted": {
                **{c: self.unaccounted[c] for c in COMPONENTS},
                "any": self.unaccounted_any,
            },
            "periods": [
                {
                    "period": p.period,
                    "auctions": p.auctions,
                    "consistency_accounted": p.consistency_accounted,
                    "quote_accounted": p.quote_accounted,
                    "consistency_period": str(p.consistency_period),
                    "consistency_window": str(p.consistency_window),
                    "quote_period": str(p.quote_period),
                    "quote_window": str(p.quote_window),
                }
                for p in self.periods
            ],
            "mismatches": [
                {
                    "component": m.component,
                    "period": m.period,
                    "solver": m.solver,
                    "int_table": str(m.int_table),
                    "mart": str(m.mart),
                }
                for m in self.mismatches
            ],
            "per_auction": {
                str(auction): [str(v) for v in values]
                for auction, values in sorted(self.per_auction.items())
            },
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], source: str = "report"
    ) -> RecordedRewards:
        """Parse `to_payload`; its totals must equal the sums over `per_auction`."""
        per_auction: dict[int, tuple[Decimal, Decimal, Decimal]] = {}
        for auction, values in payload["per_auction"].items():
            performance, consistency, quote = (Decimal(v) for v in values)
            per_auction[int(auction)] = (performance, consistency, quote)
        unaccounted = payload["auctions_unaccounted"]
        parsed = cls(
            auctions=payload["auctions"],
            per_auction=per_auction,
            unaccounted={c: unaccounted[c] for c in COMPONENTS},
            unaccounted_any=unaccounted["any"],
            periods=tuple(
                PeriodPart(
                    period=p["period"],
                    auctions=p["auctions"],
                    consistency_accounted=p["consistency_accounted"],
                    quote_accounted=p["quote_accounted"],
                    consistency_period=Decimal(p["consistency_period"]),
                    consistency_window=Decimal(p["consistency_window"]),
                    quote_period=Decimal(p["quote_period"]),
                    quote_window=Decimal(p["quote_window"]),
                )
                for p in payload["periods"]
            ),
            mismatches=tuple(
                Mismatch(
                    component=m["component"],
                    period=m["period"],
                    solver=m["solver"],
                    int_table=Decimal(m["int_table"]),
                    mart=Decimal(m["mart"]),
                )
                for m in payload["mismatches"]
            ),
        )
        checks = [(c, parsed.amount(c)) for c in COMPONENTS]
        checks.append(("total", parsed.total))
        for name, summed in checks:
            stated = Decimal(payload[name])
            if stated != summed:
                raise ValueError(
                    f"{source}: recorded {name} rewards are {stated} but its auctions "
                    f"sum to {summed}; the file is truncated, edited, or from an "
                    f"incompatible analyse version"
                )
        return parsed


# --- loading, once per window for every scenario


@dataclass(frozen=True)
class WeeklyPeriod:
    """One weekly component of one accounted period."""

    reward: Mapping[str, Decimal]
    """Mart reward per solver; absent means none was paid."""
    totals: Mapping[str, Decimal]
    """Int-table metric over the whole period, per solver."""
    window: Mapping[str, Mapping[int, float]]
    """Int-table metric per window auction, scenario solvers only; kept as the double it
    is until used, since `--all-solvers` holds ~380k of them per mainnet week."""

    def part(self, solver: str) -> dict[int, Decimal]:
        """`reward × metric(a) / Σ_period metric` per window auction, to the wei."""
        reward = self.reward.get(solver, Decimal(0))
        total = self.totals.get(solver, Decimal(0))
        if not reward or not total:
            return {}
        return {
            auction: (reward * _decimal(metric) / total).quantize(_WEI)
            for auction, metric in self.window.get(solver, {}).items()
        }


@dataclass(frozen=True)
class Period:
    label: str
    auctions: frozenset[int]
    """Window auctions with bids in this period."""
    consistency: WeeklyPeriod | None
    quote: WeeklyPeriod | None
    """`None`: the period is missing from that mart, so not paid out yet."""

    def weekly(self, component: Weekly) -> WeeklyPeriod | None:
        return self.consistency if component == "consistency" else self.quote


@dataclass(frozen=True)
class RecordedWindow:
    """Recorded rewards over one window for every scenario solver; `for_addresses` slices
    out one scenario."""

    auctions: frozenset[int]
    """Window auctions with bids."""
    performance: Mapping[str, Mapping[int, Decimal]]
    performance_unaccounted: frozenset[int]
    periods: tuple[Period, ...]
    unassigned: frozenset[int]
    """Auctions with bids that no period's window query returned; unaccounted for both
    weekly components (not expected: the periods partition the window's blocks)."""
    mismatches: tuple[Mismatch, ...]

    def for_addresses(self, addresses: Collection[str]) -> RecordedRewards:
        """A joint removal sums its members' addresses; matching is exact."""
        chosen = sorted(set(addresses))
        per_auction: dict[int, list[Decimal]] = {}

        def add(index: int, amounts: Mapping[int, Decimal]) -> None:
            for auction, amount in amounts.items():
                if amount:
                    values = per_auction.get(auction)
                    if values is None:
                        values = per_auction[auction] = [_ZERO, _ZERO, _ZERO]
                    values[index] += amount

        for solver in chosen:
            add(0, self.performance.get(solver, {}))

        unaccounted: dict[Component, set[int]] = {
            "performance": set(self.performance_unaccounted),
            "consistency": set(self.unassigned),
            "quote": set(self.unassigned),
        }
        parts: list[PeriodPart] = []
        for period in self.periods:
            sums: dict[Weekly, tuple[Decimal, Decimal]] = {}
            for component in WEEKLY:
                weekly = period.weekly(component)
                whole = window = Decimal(0)
                if weekly is None:
                    unaccounted[component] |= period.auctions
                else:
                    for solver in chosen:
                        amounts = weekly.part(solver)
                        add(COMPONENTS.index(component), amounts)
                        whole += weekly.reward.get(solver, Decimal(0))
                        window += sum(amounts.values(), Decimal(0))
                sums[component] = (whole, window)
            parts.append(
                PeriodPart(
                    period=period.label,
                    auctions=len(period.auctions),
                    consistency_accounted=period.consistency is not None,
                    quote_accounted=period.quote is not None,
                    consistency_period=sums["consistency"][0],
                    consistency_window=sums["consistency"][1],
                    quote_period=sums["quote"][0],
                    quote_window=sums["quote"][1],
                )
            )

        mine = set(chosen)
        return RecordedRewards(
            auctions=len(self.auctions),
            per_auction={
                auction: (values[0], values[1], values[2])
                for auction, values in sorted(per_auction.items())
            },
            unaccounted={c: len(unaccounted[c]) for c in COMPONENTS},
            unaccounted_any=len(set[int]().union(*unaccounted.values())),
            periods=tuple(parts),
            mismatches=tuple(m for m in self.mismatches if m.solver in mine),
        )


def _metric_rows(
    conn: Connection,
    sql: str,
    piece: Sequence[int],
    window: Collection[int],
    solvers: list[bytes],
    chunk_size: int,
) -> dict[str, dict[int, float]]:
    by_solver: dict[str, dict[int, float]] = {}
    for chunk in chunked(list(piece), chunk_size):
        params = {"first": chunk[0], "last": chunk[-1], "solvers": solvers}
        for row in fetch(conn, sql, params):
            if row["auction_id"] in window:
                by_solver.setdefault(row["solver"], {})[row["auction_id"]] = row["metric"]
    return by_solver


def _totals(conn: Connection, sql: str, first: int, last: int) -> dict[str, Decimal]:
    rows = fetch(conn, sql, {"first": first, "last": last})
    return {row["solver"]: _decimal(row["metric"]) for row in rows}


def _by_period(rows: Iterable[Row]) -> dict[str, dict[str, Row]]:
    grouped: dict[str, dict[str, Row]] = {}
    for row in rows:
        grouped.setdefault(row["accounting_period"], {})[row["solver"]] = row
    return grouped


def _reconcile(
    component: Weekly, label: str, mart: Mapping[str, Row], totals: Mapping[str, Decimal]
) -> list[Mismatch]:
    """The int table's full-period sum against the mart's own total, per solver."""
    found: list[Mismatch] = []
    any_row = next(iter(mart.values()))
    for solver in sorted(set(mart) | {s for s, v in totals.items() if v}):
        row = mart.get(solver)
        metric = totals.get(solver, Decimal(0))
        if component == "consistency":
            ours = metric
            theirs = _decimal(row["total_relative_surplus"]) if row else Decimal(0)
        else:
            orders = int(any_row["num_quote_reward_orders"] or 0)
            budget = _decimal(any_row["total_quote_budget"])
            ours = budget * metric / orders if orders else Decimal(0)
            theirs = _decimal(row["reward"]) if row else Decimal(0)
        if not _close(ours, theirs):
            found.append(Mismatch(component, label, solver, ours, theirs))
    return found


def load_recorded(
    conn: Connection,
    auction_ids: Sequence[int],
    with_bids: Collection[int],
    start: str,
    end: str,
    addresses: Collection[str],
    *,
    chunk_size: int = CHUNK_SIZE,
) -> RecordedWindow:
    """Recorded rewards over `auction_ids` (the window `[start, end)`) for `addresses`.

    `with_bids` are the window auctions with a recorded solution: those, and only those,
    need a reward row to count as accounted."""
    window = sorted(set(auction_ids))
    in_window = frozenset(window)
    expected = in_window & frozenset(with_bids)
    wanted = frozenset(addresses)
    solvers = [bytes.fromhex(a) for a in sorted(wanted)]

    performance: dict[str, dict[int, Decimal]] = {}
    covered: set[int] = set()
    for chunk in chunked(window, chunk_size):
        due = expected.intersection(chunk)
        rows = fetch(
            conn,
            PERFORMANCE_SQL,
            {"first": chunk[0], "last": chunk[-1]},
            final=lambda rows, due=due: due <= {row["auction_id"] for row in rows},
        )
        for row in rows:
            auction = row["auction_id"]
            if auction not in in_window:
                continue
            covered.add(auction)
            amount = _decimal(row["batch_reward_native"])
            if row["solver"] in wanted and amount:
                performance.setdefault(row["solver"], {})[auction] = amount

    spans = accounting_periods(start, end)
    labels = [period_label(begin, finish) for begin, finish in spans]

    def paid_out(rows: list[Row]) -> bool:
        return set(labels) <= {row["accounting_period"] for row in rows}

    marts: dict[Weekly, dict[str, dict[str, Row]]] = {
        "consistency": _by_period(
            fetch(conn, CONSISTENCY_MART_SQL, {"periods": labels}, final=paid_out)
        ),
        "quote": _by_period(fetch(conn, QUOTE_MART_SQL, {"periods": labels}, final=paid_out)),
    }

    periods: list[Period] = []
    mismatches: list[Mismatch] = []
    assigned: set[int] = set()
    for (begin, finish), label in zip(spans, labels, strict=True):
        period_ids = extract.auctions_in_window(conn, _sql_time(begin), _sql_time(finish))
        piece = [a for a in period_ids if a in in_window]
        if not piece:
            continue
        assigned.update(piece)
        weekly: dict[Weekly, WeeklyPeriod | None] = {}
        for component in WEEKLY:
            mart = marts[component].get(label)
            if not mart:
                weekly[component] = None
                continue
            per_auction_sql, totals_sql = _METRIC_SQL[component]
            totals = _totals(conn, totals_sql, period_ids[0], period_ids[-1])
            weekly[component] = WeeklyPeriod(
                reward={s: _decimal(row["reward"]) for s, row in mart.items()},
                totals=totals,
                window=_metric_rows(
                    conn, per_auction_sql, piece, in_window, solvers, chunk_size
                ),
            )
            mismatches.extend(_reconcile(component, label, mart, totals))
        periods.append(
            Period(
                label=label,
                auctions=frozenset(piece) & expected,
                consistency=weekly["consistency"],
                quote=weekly["quote"],
            )
        )

    return RecordedWindow(
        auctions=expected,
        performance=performance,
        performance_unaccounted=expected - covered,
        periods=tuple(periods),
        unassigned=expected - assigned,
        mismatches=tuple(mismatches),
    )
