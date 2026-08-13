"""The library entry point: leave-one-out analysis for solvers over one window.

`analyse_window` is the whole pipeline as one call — resolve the solvers, pull the
window's auctions, arbitrate each one with and without every solver, convert reward
deltas to COW — and the `analyse` CLI subcommand is a thin rendering around it.

Extraction dominates the cost (nearly all of a ~5-minute window run; the arbitration
itself is milliseconds per auction), so all solvers share a single pass over the
auctions: analysing five solvers costs one extraction, not five.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, NamedTuple, cast

from . import extract
from .aggregate import REPORT_FORMAT, SIGN_CONVENTION_ID
from .counterfactual import OUTCOME_RULE, Analysis, OutcomeRule, analyse_auction
from .db import Connection
from .primitives import MAX_WINNERS, wrapped_native_token
from .valuation import Mode


class CowConversion(NamedTuple):
    """Δrewards converted native -> COW at each auction's accounting-period rate."""

    cow_wei: Decimal
    cow_wei_capped: Decimal
    converted_native: int
    """The part of the native uncapped delta the conversion covers."""
    auctions_without_rate: int
    native_without_rate: int
    capped_without_rate: Decimal
    """Auctions whose accounting period has no snapshotted rate yet, with the native
    uncapped and capped delta they carry — left unconverted rather than guessed at."""


def convert_delta_rewards(conn: Connection, analysis: Analysis) -> CowConversion:
    """Only auctions retained in `changed` can carry a non-zero reward delta: rewards
    move only when the winner set or a reference score does, and both retain (D8)."""
    moved = [
        r
        for r in analysis.changed
        if r.delta_rewards or (r.delta_rewards_capped or 0) != 0
    ]
    rates = extract.load_conversion_rates(
        conn, sorted({r.block_deadline for r in moved})
    )
    cow_wei, cow_wei_capped = Decimal(0), Decimal(0)
    converted, missing, missing_native = 0, 0, 0
    missing_capped = Decimal(0)
    for result in moved:
        rate = rates.get(result.block_deadline)
        if rate:
            cow_wei += Decimal(result.delta_rewards) / rate
            if result.delta_rewards_capped is not None:
                cow_wei_capped += result.delta_rewards_capped / rate
            converted += result.delta_rewards
        else:
            missing += 1
            missing_native += result.delta_rewards
            if result.delta_rewards_capped is not None:
                missing_capped += result.delta_rewards_capped
    return CowConversion(
        cow_wei, cow_wei_capped, converted, missing, missing_native, missing_capped
    )


@dataclass
class SolverRun:
    """One solver's leave-one-out result over the window."""

    solver: str
    """The name or address as the caller gave it — the report's identity."""
    addresses: frozenset[str]
    matches: list[extract.SolverMatch]
    analysis: Analysis
    cow: CowConversion | None = None
    """Native -> COW conversion of the reward deltas; `None` in surplus mode, where no
    rewards exist to convert."""


@dataclass
class WindowAnalysis:
    """Everything one `analyse_window` call produced."""

    network: str
    start: str
    end: str
    auction_ids: list[int]
    missing_data: list[int]
    """Auctions excluded before arbitration because a traded order is in neither order
    table (D17). A property of the window, so shared by every run."""
    runs: list[SolverRun]


def analyse_window(
    conn: Connection,
    solvers: Sequence[str],
    start: str,
    end: str,
    *,
    network: str = "mainnet",
    mode: Mode = "score",
    outcome_rule: OutcomeRule = "inherited",
    max_winners: int = MAX_WINNERS,
    include_price_suspects: bool = False,
    limit: int | None = None,
    log: Callable[[str], None] | None = None,
) -> WindowAnalysis:
    """Remove each solver from every auction in `[start, end)` and diff the outcomes.

    One extraction serves every solver: the auction bundles are solver-independent, so
    each is arbitrated once per solver as it streams past. Solvers are resolved before
    anything is extracted — a bad `--solver` fails in seconds, not after five minutes.

    Raises `extract.SolverResolutionError` when a solver does not resolve to exactly
    one competitor that bid in the window, and `counterfactual.MissingSettlementError`
    when the settlement source does not cover the window under the `inherited` rule.
    """
    emit: Callable[[str], None] = log or (lambda message: None)
    weth = wrapped_native_token(network)

    runs: list[SolverRun] = []
    for solver in solvers:
        addresses, matches = extract.resolve_solver(conn, solver, start, end)
        for match in matches:
            emit(
                f"solver {solver!r} -> {match.address}  {match.name} "
                f"({match.environment}, active={match.active})  "
                f"{match.solutions} solutions in {match.auctions_bid} auctions, "
                f"{match.winning_solutions} winning"
            )
        if len(matches) > 1:
            # A key rotation: the same competitor under two submission addresses. All of
            # them have to go together, or reference scores would treat one half of the
            # rotation as a rival of the other.
            emit(
                f"note: {len(matches)} addresses resolved; removing all of them as one "
                f"solver"
            )
        for earlier in runs:
            if earlier.addresses & addresses:
                raise extract.SolverResolutionError(
                    f"{solver!r} and {earlier.solver!r} resolve to the same competitor "
                    f"(shared address); give each solver once"
                )
        runs.append(
            SolverRun(
                solver=solver,
                addresses=addresses,
                matches=matches,
                analysis=Analysis(
                    solver=solver,
                    addresses=addresses,
                    mode=mode,
                    outcome_rule=outcome_rule,
                    capped_estimate=mode == "score",
                    exclude_price_suspect=not include_price_suspects,
                ),
            )
        )

    auction_ids = extract.auctions_in_window(conn, start, end)
    if limit:
        auction_ids = auction_ids[:limit]
    emit(f"{len(auction_ids)} auctions in [{start}, {end}) on {network}")
    window = WindowAnalysis(
        network=network,
        start=start,
        end=end,
        auction_ids=auction_ids,
        missing_data=[],
        runs=runs,
    )
    if not auction_ids:
        return window

    settled: dict[int, dict[int, extract.Settlement]] = {}
    if outcome_rule != "assume-settled":
        settled = extract.load_settlement_outcomes(conn, auction_ids)
        emit(
            f"settlement outcomes for {sum(len(v) for v in settled.values())} "
            f"winning solutions across {len(settled)} auctions"
        )

    caps: dict[int, dict[int, extract.SolutionCap]] = {}
    if mode == "score":
        caps = extract.load_solution_caps(conn, auction_ids)
        emit(
            f"reward caps for {sum(len(v) for v in caps.values())} "
            f"recorded winning solutions"
        )

    for bundle in extract.load_auctions(
        conn, auction_ids, missing_data=window.missing_data
    ):
        for run in runs:
            run.analysis.add(
                analyse_auction(
                    bundle,
                    weth,
                    run.addresses,
                    mode=mode,
                    max_winners=max_winners,
                    outcome_rule=outcome_rule,
                    settled=settled.get(bundle.auction_id, {}),
                    solution_caps=caps.get(bundle.auction_id, {}),
                )
            )

    for run in runs:
        # The exclusion is a property of the window, not of the solver being removed.
        run.analysis.missing_data_auctions = list(window.missing_data)
        if mode == "score":
            run.cow = convert_delta_rewards(conn, run.analysis)

    return window


# --- the report file ----------------------------------------------------------------
# One JSON per (solver, window, rule), consumed by `loo compare` and the notebook via
# `aggregate.load_report`. The payload is derived from the dataclasses, so a statistic
# is named once — on its field or property — and the file follows.

_ANALYSIS_PROPERTIES = ("delta_surplus", "delta_rewards", "delta_rewards_capped")
_AUCTION_PROPERTIES = ("delta_surplus", "delta_rewards", "delta_rewards_capped")


def _fields_and_properties(obj: Any, properties: Sequence[str]) -> dict[str, Any]:
    data = {f.name: getattr(obj, f.name) for f in fields(obj)}
    data.update({name: getattr(obj, name) for name in properties})
    return data


def _json_default(value: Any) -> Any:
    """JSON encoding for the report's non-primitive values.

    Decimals become strings (their exactness must survive the file); sets become
    sorted lists; nested dataclasses (`SolverReward`, `OrderDiff`) become dicts.
    Integers stay integers — JSON has no precision limit and every consumer of these
    files is Python. Anything else is a bug, not a value to coerce.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, frozenset | set):
        # The cast restores the element type the isinstance check erased from `Any`.
        return sorted(cast("Iterable[Any]", value))
    if hasattr(value, "__dataclass_fields__"):
        return _fields_and_properties(value, ())
    raise TypeError(f"cannot serialise {type(value).__name__}: {value!r}")


def report_payload(window: WindowAnalysis, run: SolverRun) -> dict[str, Any]:
    """The `analyse --out` payload for one run.

    Keys are the `Analysis` and `AuctionCounterfactual` field and property names —
    single-sourced from the dataclasses — plus the window metadata and format markers
    `aggregate.load_report` verifies.
    """
    analysis = run.analysis
    payload: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "sign_convention": SIGN_CONVENTION_ID,
        "network": window.network,
        "start": window.start,
        "end": window.end,
        "outcome_rule_description": OUTCOME_RULE[analysis.outcome_rule],
        "cow": run.cow._asdict() if run.cow is not None else None,
    }
    payload.update(_fields_and_properties(analysis, _ANALYSIS_PROPERTIES))
    payload["changed"] = [
        _fields_and_properties(result, _AUCTION_PROPERTIES)
        for result in analysis.changed
    ]
    return payload


def write_report(path: str, window: WindowAnalysis, run: SolverRun) -> None:
    with open(path, "w") as handle:
        json.dump(report_payload(window, run), handle, indent=2, default=_json_default)
