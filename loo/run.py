"""The library entry point: leave-one-out analysis for solvers over one window.

`analyse_window` is the whole pipeline as one call — resolve the solvers, pull the
window's auctions, arbitrate each one with and without every removal scenario,
convert reward deltas to COW — and the `analyse` CLI subcommand is a thin rendering
around it. A scenario is one solver, or a comma-separated group removed **together**
as one joint counterfactual (D25).

Extraction dominates the cost (nearly all of a ~5-minute window run; the arbitration
itself is milliseconds per auction), so all scenarios share a single pass over the
auctions: analysing five costs one extraction, not five.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from decimal import Decimal
from types import MappingProxyType
from typing import Any, NamedTuple, cast

from . import ch, extract, quotes
from .aggregate import REPORT_FORMAT, SIGN_CONVENTION_ID
from .counterfactual import (
    OUTCOME_RULE,
    Analysis,
    AuctionCounterfactual,
    OutcomeRule,
    analyse_auction,
)
from .db import Connection
from .fees import Quote
from .primitives import MAX_WINNERS, wrapped_native_token
from .quotes import QuoteAnalysis, QuoteRanking, QuoteWindow
from .valuation import Mode


class GroupMember(NamedTuple):
    """One solver of a removal scenario, as the caller named it and as it resolved."""

    given: str
    addresses: frozenset[str]


def parse_removal_group(entry: str) -> list[str]:
    """The solver names of one `solvers` entry: one name, or several separated by
    commas, which are removed **together** as one joint counterfactual."""
    names = [part.strip() for part in entry.split(",") if part.strip()]
    if not names:
        raise extract.SolverResolutionError(
            f"{entry!r} names no solver — give a name/address, or several separated "
            f"by commas to remove them together"
        )
    return names


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


def moved_auctions(analysis: Analysis) -> list[AuctionCounterfactual]:
    """Only auctions retained in `changed` can carry a non-zero reward delta: rewards
    move only when the winner set or a reference score does, and both retain (D8)."""
    return [
        r
        for r in analysis.changed
        if r.delta_rewards or (r.delta_rewards_capped or 0) != 0
    ]


def convert_delta_rewards(
    analysis: Analysis, rates: Mapping[int, Decimal | None]
) -> CowConversion:
    """Convert one run's reward deltas at each auction's accounting-period rate.

    `rates` maps block deadlines to rates and is loaded once per window for all runs
    (`load_conversion_rates`), not per run: the rates table is expensive to touch and
    identical across solvers and outcome rules."""
    moved = moved_auctions(analysis)
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
    """One removal scenario's leave-one-out result over the window."""

    solver: str
    """The scenario as the caller gave it — a solver name or address, or the
    comma-joined members of a removal group — and the report's identity."""
    addresses: frozenset[str]
    """The union over the members: everything in it is removed at once."""
    matches: list[extract.SolverMatch]
    members: tuple[GroupMember, ...]
    """One entry per name the caller gave. The joint counterfactual attributes nothing
    to them; they are kept so a member's absence from the analysed auctions can be
    reported per member (`absent_members`)."""
    analysis: Analysis
    cow: CowConversion | None = None
    """Native -> COW conversion of the reward deltas; `None` in surplus mode, where no
    rewards exist to convert."""
    quote_substitutions: Mapping[str, Quote] = MappingProxyType({})
    """`order_uid -> counterfactual quote`, cached off `quote.counterfactual_quotes` so
    the auction loop does not rebuild it per auction."""
    quote: QuoteAnalysis | None = None
    """The quote side of this scenario; `None` when the run had no quote pass, which is
    the state a network without quote data is always in (Q-D10). Rule-independent — the
    quote competition has no settlement — so the runs of one scenario under two outcome
    rules share it."""


def absent_members(solver_run: SolverRun) -> list[str]:
    """Members of the scenario that bid in no clean analysed auction.

    `resolve_solver` guarantees every member bid somewhere in the window, but
    `--limit` can slice those auctions away and the price-suspect exclusion can eat
    the rest. A report labelled "A,B" when B never influenced an analysed auction
    would pass a single-solver result off as a joint finding, so the CLI treats a
    non-empty return the way it treats an all-zero single-solver run: an error, not
    a number."""
    seen = solver_run.analysis.addresses_seen
    return [m.given for m in solver_run.members if not (m.addresses & seen)]


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
    quote_window: QuoteWindow | None = None
    """The quote competition over the same window, loaded once for every scenario;
    `None` when the quote pass was off or the network has no quote data."""


def analyse_window(
    conn: Connection,
    solvers: Sequence[str],
    start: str,
    end: str,
    *,
    network: str = "mainnet",
    mode: Mode = "score",
    outcome_rules: Sequence[OutcomeRule] = ("inherited",),
    max_winners: int = MAX_WINNERS,
    include_price_suspects: bool = False,
    limit: int | None = None,
    with_quotes: bool = False,
    quote_ranking: QuoteRanking = "effective-sell",
    log: Callable[[str], None] | None = None,
) -> WindowAnalysis:
    """Remove each scenario's solvers from every auction in `[start, end)` and diff
    the outcomes.

    Each `solvers` entry is one removal scenario: a single name/address, or several
    separated by commas, which are removed **together** — one counterfactual with all
    of them absent, whose deltas are never attributed to individual members (D25).
    Overlapping scenarios are legitimate (`["Fractal", "Fractal,Sector"]` compares a
    single removal with a joint one); only two entries resolving to the *same* address
    set are rejected as a duplicate run, and one competitor may not appear twice
    within one group.

    One extraction serves every scenario **and every outcome rule**: the auction
    bundles are solver- and rule-independent, so each is arbitrated once per
    (scenario, rule) as it streams past. Extraction is nearly all of a run's cost, so
    asking for the second rule costs minutes of arbitration, not a second pass over
    the DB. Solvers are resolved before anything is extracted — a bad `--solver`
    fails in seconds, not after five minutes.

    Raises `extract.SolverResolutionError` when a solver does not resolve to exactly
    one competitor that bid in the window, and `counterfactual.MissingSettlementError`
    when the settlement source does not cover the window under the `inherited` rule.
    """
    if not outcome_rules:
        raise ValueError("outcome_rules must name at least one rule")
    if len(set(outcome_rules)) != len(outcome_rules):
        raise ValueError(f"duplicate outcome rules: {list(outcome_rules)}")
    emit: Callable[[str], None] = log or (lambda message: None)
    weth = wrapped_native_token(network)

    resolved: list[
        tuple[str, tuple[GroupMember, ...], frozenset[str], list[extract.SolverMatch]]
    ] = []
    for entry in solvers:
        members: list[GroupMember] = []
        matches: list[extract.SolverMatch] = []
        group_addresses: frozenset[str] = frozenset()
        for name in parse_removal_group(entry):
            addresses, member_matches = extract.resolve_solver(conn, name, start, end)
            if addresses & group_addresses:
                raise extract.SolverResolutionError(
                    f"{name!r} resolves to a competitor already named in {entry!r}; "
                    f"each solver goes into a removal group once"
                )
            for match in member_matches:
                emit(
                    f"solver {name!r} -> {match.address}  {match.name} "
                    f"({match.environment}, active={match.active})  "
                    f"{match.solutions} solutions in {match.auctions_bid} auctions, "
                    f"{match.winning_solutions} winning"
                )
            if len(member_matches) > 1:
                # A key rotation: the same competitor under two submission addresses.
                # All of them have to go together, or reference scores would treat one
                # half of the rotation as a rival of the other.
                emit(
                    f"note: {len(member_matches)} addresses resolved; removing all of "
                    f"them as one solver"
                )
            members.append(GroupMember(name, addresses))
            matches.extend(member_matches)
            group_addresses |= addresses
        label = ",".join(m.given for m in members)
        if len(members) > 1:
            emit(
                f"note: {label!r} removes {len(members)} solvers together — one joint "
                f"counterfactual, with nothing attributed to individual members"
            )
        for earlier_label, _, earlier_addresses, _ in resolved:
            # Overlap is legitimate — removing A alone and {A,B} together are two
            # different scenarios — but the same address set twice is the same run.
            if earlier_addresses == group_addresses:
                raise extract.SolverResolutionError(
                    f"{label!r} and {earlier_label!r} resolve to the same removal "
                    f"(identical addresses); give each scenario once"
                )
        resolved.append((label, tuple(members), group_addresses, matches))

    runs = [
        SolverRun(
            solver=label,
            addresses=addresses,
            matches=matches,
            members=members,
            analysis=Analysis(
                solver=label,
                addresses=addresses,
                mode=mode,
                outcome_rule=rule,
                capped_estimate=mode == "score",
                exclude_price_suspect=not include_price_suspects,
            ),
        )
        for label, members, addresses, matches in resolved
        for rule in outcome_rules
    ]

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

    # The quote side, loaded once for every scenario and every rule: the ladder and the
    # order match are scenario-independent, exactly as the auction bundles are (D18).
    # A network without quote data is refused here rather than silently reporting zeros.
    if with_quotes:
        if not ch.has_quote_data(network):
            raise ch.NoQuoteDataError(
                f"--quotes was asked for on {network!r}, which has no quote data; "
                f"quote coverage is {', '.join(sorted(ch.NETWORK_CHAIN_IDS))}"
            )
        window.quote_window = quotes.load_quote_window(
            ch.Client.from_env(),
            conn,
            network,
            start,
            end,
            ranking=quote_ranking,
            auction_ids=auction_ids,
            log=log,
        )
        by_scenario = {
            label: window.quote_window.analyse(label, addresses)
            for label, _, addresses, _ in resolved
        }
        for run in runs:
            run.quote = by_scenario[run.solver]
            run.quote_substitutions = run.quote.counterfactual_quotes
            emit(
                f"quote side {run.solver!r}: {run.quote.quote_wins} quote wins "
                f"({run.quote.no_quote} would leave no quote), "
                f"{len(run.quote_substitutions)} counterfactual quotes for the fee path"
            )

    settled: dict[int, dict[int, extract.Settlement]] = {}
    if any(rule != "assume-settled" for rule in outcome_rules):
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
                    outcome_rule=run.analysis.outcome_rule,
                    settled=settled.get(bundle.auction_id, {}),
                    solution_caps=caps.get(bundle.auction_id, {}),
                    counterfactual_quotes=run.quote_substitutions,
                )
            )

    for run in runs:
        # The exclusion is a property of the window, not of the solver being removed.
        run.analysis.missing_data_auctions = list(window.missing_data)

    if mode == "score":
        # One rate query for every run together: the rates are identical across
        # solvers and rules, and the table behind them must be touched sparingly
        # (see CONVERSION_RATES_SQL).
        rates = extract.load_conversion_rates(
            conn,
            sorted(
                {
                    r.block_deadline
                    for run in runs
                    for r in moved_auctions(run.analysis)
                }
            ),
        )
        for run in runs:
            run.cow = convert_delta_rewards(run.analysis, rates)

    return window


# --- the report file ----------------------------------------------------------------
# One JSON per (solver, window, rule), consumed by `loo compare` and the notebook via
# `aggregate.load_report`. The payload is derived from the dataclasses, so a statistic
# is named once — on its field or property — and the file follows.

_FEE_PROPERTIES = (
    "delta_protocol_and_partner_fees",
    "delta_partner_fees",
    "delta_protocol_fees",
    "protocol_fees_base",
    "protocol_fees_loo",
)
_ANALYSIS_PROPERTIES = (
    "delta_surplus",
    "delta_rewards",
    "delta_rewards_capped",
    "quote_fee_recharge_protocol",
    *_FEE_PROPERTIES,
)
_AUCTION_PROPERTIES = (
    "delta_surplus",
    "delta_rewards",
    "delta_rewards_capped",
    "fees_complete",
    "protocol_and_partner_fees_base",
    "protocol_and_partner_fees_loo",
    "partner_fees_base",
    "partner_fees_loo",
    "quote_fee_recharge",
    "quote_fee_recharge_partner",
    "quote_fee_recharge_protocol",
    *_FEE_PROPERTIES,
)


def _fields_and_properties(obj: Any, properties: Sequence[str]) -> dict[str, Any]:
    """Every field and the named properties, except fields declared
    `metadata={"report": False}` — bookkeeping behind a reported count, not a result."""
    data = {
        f.name: getattr(obj, f.name)
        for f in fields(obj)
        if f.metadata.get("report", True)
    }
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


_QUOTE_PROPERTIES = (
    "window_bps",
    "window_bps_all_tiers",
    "conditional_bps",
    "no_quote_share",
)


def _quote_payload(analysis: QuoteAnalysis) -> dict[str, Any]:
    """One run's quote side, with the per-order diffs reduced to what a reader of the
    file needs: the leaf, its weight and its bps. The counterfactual `Quote` itself is
    dropped — it is an input to the fee path, not a result, and it is the one field that
    would double the file."""
    data = {
        f.name: getattr(analysis, f.name)
        for f in fields(analysis)
        if f.name != "diffs"
    }
    data.update({name: getattr(analysis, name) for name in _QUOTE_PROPERTIES})
    data["diffs"] = [
        {
            "order_uid": diff.order_uid,
            "leaf": diff.leaf,
            "tier": diff.tier,
            "winner": diff.winner,
            "replacement": diff.replacement,
            "delta_bps": diff.delta_bps,
            "volume_native": diff.volume_native,
            "partially_fillable": diff.partially_fillable,
        }
        for diff in analysis.diffs
    ]
    return data


def _quote_window_payload(quote_window: QuoteWindow) -> dict[str, Any]:
    """The window's quote metadata — shared by every scenario, so it says what the
    quote side could see rather than what one solver did with it."""
    validation = quote_window.validation
    return {
        "ranking": quote_window.ranking,
        "ranking_description": quotes.RANKING[quote_window.ranking],
        "coverage_start": quote_window.coverage_start,
        "start": quote_window.start,
        "end": quote_window.end,
        "orders": len(quote_window.orders),
        "orders_dropped": quote_window.orders_dropped,
        "orders_before_coverage": quote_window.orders_before_coverage,
        "matched": len(quote_window.matches),
        "matched_corroborated": sum(
            1 for m in quote_window.matches if m.tier == "corroborated"
        ),
        "matched_raw": quote_window.matched_raw,
        "volume_native": quote_window.window_volume_native,
        "quoted_volume_native": quote_window.window_quoted_volume_native,
        "price_suspects": len(quote_window.price_suspects),
        "coverage_by_app": [
            {"app_code": app, "orders": n, "matched": m}
            for app, n, m in quote_window.coverage_by_app()[:25]
        ],
        "estimators_mapped": len(quote_window.estimator_map),
        "pick_requests": validation.with_recorded_winner,
        "pick_reproduced": validation.reproduced,
        "pick_rate": validation.rate,
        "pick_mixed_class": validation.mixed_class,
        "pick_mixed_class_correct": validation.mixed_class_correct,
        "pick_tied": validation.tied,
        "pick_unpriced": validation.unpriced,
        "pick_price_suspect": validation.price_suspect,
        "pick_disagreements": validation.disagreements[:20],
    }


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
        "quote": _quote_payload(run.quote) if run.quote is not None else None,
        "quote_window": (
            _quote_window_payload(window.quote_window)
            if window.quote_window is not None
            else None
        ),
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
