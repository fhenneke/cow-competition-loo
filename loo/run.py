"""Leave-one-out analysis of removal scenarios over one window (`analyse` CLI core).

All scenarios and outcome rules share one extraction pass and, per auction, one valuation
and baseline arbitration (`PreparedAuction`); each scenario adds only its side without
the removed solvers.
"""

from __future__ import annotations

import gc
import json
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, NamedTuple, cast

from . import ch, db, extract, payouts, quotes
from .aggregate import REPORT_FORMAT, SIGN_CONVENTION_ID
from .counterfactual import (
    OUTCOME_RULE,
    Analysis,
    AuctionCounterfactual,
    OutcomeRule,
    PreparedAuction,
)
from .db import Connection, Snapshot
from .fees import Quote
from .primitives import MAX_WINNERS, SOLANA, wrapped_native_token
from .quotes import QuoteAnalysis, QuoteRanking, QuoteWindow
from .valuation import Mode


class GroupMember(NamedTuple):
    given: str
    addresses: frozenset[str]


def parse_removal_group(entry: str) -> list[str]:
    """Comma-separated names are removed together as one joint counterfactual."""
    names = [part.strip() for part in entry.split(",") if part.strip()]
    if not names:
        raise extract.SolverResolutionError(
            f"{entry!r} names no solver — give a name/address, or several separated "
            f"by commas to remove them together"
        )
    return names


class CowConversion(NamedTuple):
    cow_wei: Decimal
    cow_wei_capped: Decimal
    converted_native: int
    auctions_without_rate: int
    native_without_rate: int
    capped_without_rate: Decimal
    """Left unconverted: the accounting period has no rate yet."""


def moved_auctions(analysis: Analysis) -> list[AuctionCounterfactual]:
    """Reward deltas need a winner-set or reference-score change, which `changed` retains."""
    return [
        r
        for r in analysis.changed
        if r.delta_rewards or (r.delta_rewards_capped or 0) != 0
    ]


def convert_delta_rewards(
    analysis: Analysis, rates: Mapping[int, Decimal | None]
) -> CowConversion:
    """`rates` maps block deadline -> rate; load once per window, the table is costly."""
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
    solver: str
    """Scenario label as given (comma-joined for a group); the report's identity."""
    addresses: frozenset[str]
    matches: list[extract.SolverMatch]
    members: tuple[GroupMember, ...]
    """Only for `absent_members`; deltas are never attributed to members."""
    analysis: Analysis
    cow: CowConversion | None = None
    quote_substitutions: Mapping[str, Quote] = MappingProxyType({})
    quote: QuoteAnalysis | None = None
    """Rule-independent (quotes have no settlement): shared across a scenario's rules."""
    recorded: payouts.RecordedWindow | None = None
    """Every scenario's recorded rewards over the window, shared by all runs."""

    @property
    def paid(self) -> payouts.RecordedRewards | None:
        """This scenario's recorded rewards: not counterfactual, so the same under every
        rule. Sliced on use, since holding every scenario's per-auction amounts at once
        costs ~200 MiB per mainnet week under `--all-solvers`."""
        if self.recorded is None:
            return None
        return self.recorded.for_addresses(self.addresses)


def absent_members(solver_run: SolverRun) -> list[str]:
    """Members that bid in no clean analysed auction; the CLI treats any as an error."""
    seen = solver_run.analysis.addresses_seen
    return [m.given for m in solver_run.members if not (m.addresses & seen)]


@dataclass
class WindowAnalysis:
    network: str
    start: str
    end: str
    auction_ids: list[int]
    runs: list[SolverRun]
    quote_window: QuoteWindow | None = None
    fast_path: list[int] = field(default_factory=list[int])
    """Excluded before arbitration: no solver competition to re-run."""


def window_queries(
    network: str,
    start: str,
    end: str,
    *,
    with_quotes: bool = False,
    all_solvers: bool = False,
) -> list[tuple[str, dict[str, str] | None]]:
    """The reads `analyse_window` starts with, which need only the window, for
    `db.prefetch`: the solver catalogue, the window's auctions and, with the quote side,
    its orders and trades; `all_solvers` adds the window's bidders by name."""
    quoted = with_quotes and ch.has_quote_data(network)
    return [
        *extract.window_queries(start, end, network, all_solvers=all_solvers),
        *(quotes.window_queries(start, end) if quoted else []),
    ]


@contextmanager
def sparse_gc(young: int = 100_000) -> Generator[None]:
    """Run the cyclic garbage collector every `young` net allocations instead of 700.

    An analysis keeps millions of objects to its end and makes next to no cyclic garbage
    (reference counting frees the rest), and at the default the collector re-scanned
    them so often that it took 12 s of a 67 s two-week mainnet replay (2026-11-26).
    Collections still run, so a cycle is still freed, only later."""
    thresholds = gc.get_threshold()
    gc.set_threshold(young, *thresholds[1:])
    try:
        yield
    finally:
        gc.set_threshold(*thresholds)


@sparse_gc()
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
    """Remove each scenario's solvers from every auction in `[start, end)` and diff.

    Each `solvers` entry is one scenario; comma-separated names are removed jointly.
    Overlapping scenarios are fine; identical address sets are rejected as duplicates.
    """
    if not outcome_rules:
        raise ValueError("outcome_rules must name at least one rule")
    if len(set(outcome_rules)) != len(outcome_rules):
        raise ValueError(f"duplicate outcome rules: {list(outcome_rules)}")
    emit: Callable[[str], None] = log or (lambda message: None)
    weth = wrapped_native_token(network)
    # The window-level reads, ahead of all else; the rest is queued once the auctions are
    # known.
    db.prefetch(
        conn, window_queries(network, start, end, with_quotes=with_quotes), first=True
    )

    resolved: list[
        tuple[str, tuple[GroupMember, ...], frozenset[str], list[extract.SolverMatch]]
    ] = []
    directory = extract.solver_directory(conn, start, end)
    for entry in solvers:
        members: list[GroupMember] = []
        matches: list[extract.SolverMatch] = []
        group_addresses: frozenset[str] = frozenset()
        for name in parse_removal_group(entry):
            addresses, member_matches = extract.resolve_solver(
                directory, name, start, end, network
            )
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
                # Key rotation: removing only some addresses leaves the solver as its own rival.
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

    extract.require_current_mechanism(start)
    auction_ids = extract.auctions_in_window(conn, start, end)
    if limit:
        auction_ids = auction_ids[:limit]
    emit(f"{len(auction_ids)} auctions in [{start}, {end}) on {network}")
    window = WindowAnalysis(
        network=network,
        start=start,
        end=end,
        auction_ids=auction_ids,
        runs=runs,
    )
    if not auction_ids:
        return window

    inherited = any(rule != "assume-settled" for rule in outcome_rules)
    # Solana's accounting has no quote-share table to split a period by (below).
    paid = network != SOLANA
    if with_quotes and not ch.has_quote_data(network):
        raise ch.NoQuoteDataError(
            f"--quotes was asked for on {network!r}, which has no quote data; "
            f"quote coverage is {', '.join(sorted(ch.NETWORK_CHAIN_IDS))}"
        )
    # Everything below is read ahead on parallel connections, in the order it is used;
    # the quote side, used first, puts its queries ahead of these as it learns them.
    db.prefetch(
        conn,
        [
            *(extract.settlement_queries(auction_ids, network) if inherited else []),
            *(
                extract.reward_input_queries(auction_ids, network)
                if mode == "score"
                else []
            ),
            *(payouts.plan_queries(start, end) if paid else []),
            *extract.auction_queries(auction_ids, network),
            *(payouts.performance_queries(auction_ids) if paid else []),
        ],
    )

    if with_quotes:
        window.quote_window = quotes.load_quote_window(
            ch.Client.from_env(conn if isinstance(conn, Snapshot) else None),
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
    if inherited:
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

    # Every scenario's solvers: the payouts of record are read in one pass for all.
    every_address = frozenset[str]().union(*(addresses for _, _, addresses, _ in resolved))
    plan = (
        payouts.plan_recorded(conn, auction_ids, start, end, every_address) if paid else None
    )

    with_bids: list[int] = []
    deadlines: list[int] = []
    for bundle in extract.load_auctions(conn, auction_ids, fast_path=window.fast_path):
        with_bids.append(bundle.auction_id)
        deadlines.append(bundle.block_deadline)
        # Valued and arbitrated once; each run adds only its side without the removal.
        prepared = PreparedAuction(
            bundle,
            weth,
            mode=mode,
            max_winners=max_winners,
            settled=settled.get(bundle.auction_id, {}),
            solution_caps=caps.get(bundle.auction_id, {}),
        )
        for run in runs:
            run.analysis.add(
                prepared.analyse(
                    run.addresses,
                    outcome_rule=run.analysis.outcome_rule,
                    counterfactual_quotes=run.quote_substitutions,
                )
            )

    for run in runs:
        run.analysis.fast_path_auctions = list(window.fast_path)

    if not paid:
        # Its weekly accounting has no quote-share table to split a period by, and every
        # Solana reward budget is zero to date: the payouts and COW columns stay unknown.
        emit("recorded rewards and COW conversion: not available on solana")
        return window

    # Payouts of record over every window auction, fast path included; one pass for all.
    recorded = payouts.load_recorded(
        conn,
        auction_ids,
        [*with_bids, *window.fast_path],
        start,
        end,
        every_address,
        plan=plan,
    )
    for run in runs:
        run.recorded = recorded
    # Each scenario's own totals are in its report: slicing one out costs as much as the
    # report's own slice, so it is not done twice just to log it.
    emit(
        f"recorded rewards for {len(resolved)} scenarios over {len(recorded.auctions)} "
        f"auctions"
        + (f", {len(recorded.mismatches)} int-table/mart mismatches" if recorded.mismatches else "")
    )

    if mode == "score":
        # One rate query for all runs; the table must be touched sparingly.
        rates = extract.load_conversion_rates(
            conn,
            sorted(
                {
                    r.block_deadline
                    for run in runs
                    for r in moved_auctions(run.analysis)
                }
            ),
            (min(deadlines, default=0), max(deadlines, default=0)),
        )
        for run in runs:
            run.cow = convert_delta_rewards(run.analysis, rates)

    return window


# --- the report file: one JSON per (scenario, window, rule), read by `aggregate.load_report`

_FEE_PROPERTIES = (
    "delta_protocol_and_partner_fees",
    "delta_partner_fees",
    "delta_protocol_fees",
    "protocol_fees_base",
    "protocol_fees_loo",
)
_ANALYSIS_PROPERTIES = (
    "delta_surplus",
    "delta_score",
    "delta_rewards",
    "delta_rewards_capped",
    "delta_reference_margin",
    "delta_margin_above_cap",
    "delta_pressed_rewards_capped",
    "delta_settled_margin",
    "delta_consistency_budget",
    "quote_fee_recharge_protocol",
    *_FEE_PROPERTIES,
)
_AUCTION_PROPERTIES = (
    "delta_surplus",
    "delta_score",
    "delta_rewards",
    "delta_rewards_capped",
    "delta_reference_margin",
    "delta_margin_above_cap",
    "delta_pressed_rewards_capped",
    "delta_settled_margin",
    "delta_consistency_budget",
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


_REPORTED: dict[type[Any], tuple[tuple[str, ...], bool]] = {}
"""Per dataclass, its reported field names and whether that is every field; looked up
once rather than per row."""


def _fields_and_properties(obj: Any, properties: Sequence[str]) -> dict[str, Any]:
    kind: type[Any] = obj.__class__
    known = _REPORTED.get(kind)
    if known is None:
        every = fields(obj)
        names = tuple(f.name for f in every if f.metadata.get("report", True))
        known = _REPORTED[kind] = (names, len(names) == len(every))
    names, everything = known
    # A dataclass keeps its fields in the instance dict, in declaration order; when
    # every one is reported and nothing is cached beside them, that dict is the row.
    state = obj.__dict__
    data = (
        dict(state)
        if everything and len(state) == len(names)
        else {name: state[name] for name in names}
    )
    data.update({name: getattr(obj, name) for name in properties})
    return data


_BY_TYPE: dict[type[Any], Callable[[Any], Any]] = {Decimal: str, frozenset: sorted, set: sorted}
"""`_json_default` for the exact types it meets most, looked up before the general tests."""


def _json_default(value: Any) -> Any:
    convert = _BY_TYPE.get(value.__class__)
    if convert is not None:
        return convert(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, frozenset | set):
        return sorted(cast("Iterable[Any]", value))
    if hasattr(value, "__dataclass_fields__"):
        return _fields_and_properties(value, ())
    raise TypeError(f"cannot serialise {type(value).__name__}: {value!r}")


_QUOTE_PROPERTIES = (
    "window_bps",
    "conditional_bps",
    "no_quote_share",
)


def _quote_payload(analysis: QuoteAnalysis) -> dict[str, Any]:
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
            "winner": diff.winner,
            "replacement": diff.replacement,
            "delta_bps": diff.delta_bps,
            "volume_native": diff.volume_native,
            "partially_fillable": diff.partially_fillable,
            "streamed": diff.streamed,
            "commit_dependent": diff.commit_dependent,
        }
        for diff in analysis.diffs
    ]
    return data


def _quote_window_payload(quote_window: QuoteWindow) -> dict[str, Any]:
    validation = quote_window.validation
    return {
        "ranking": quote_window.ranking,
        "ranking_description": quotes.RANKING[quote_window.ranking],
        "start": quote_window.start,
        "end": quote_window.end,
        "orders": len(quote_window.orders),
        "orders_dropped": quote_window.orders_dropped,
        "trivial_orders": quote_window.trivial_orders,
        "link": {
            "orders": quote_window.link.orders,
            "linked": quote_window.link.linked,
            "requoted": quote_window.link.requoted,
            "streamed": quote_window.link.streamed,
            "streamed_early": quote_window.link.streamed_early,
            "unlinked": dict(quote_window.link.unlinked),
        },
        "matched": len(quote_window.matches),
        "volume_native": quote_window.window_volume_native,
        "quoted_volume_native": quote_window.window_quoted_volume_native,
        "price_suspects": len(quote_window.price_suspects),
        "price_suspects_one_sided": quote_window.one_sided_suspects,
        "price_suspects_two_sided": quote_window.two_sided_suspects,
        "price_suspect_volume_native": quote_window.suspect_volume_native,
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
        "pick_tie_mismatches": validation.tie_mismatches,
        "pick_streams": validation.streams,
        "pick_streams_reproduced": validation.streams_reproduced,
        "pick_stream_disagreements": validation.stream_disagreements[:20],
        "pick_unpriced": validation.unpriced,
        "pick_price_suspect": validation.price_suspect,
        "pick_disagreements": validation.disagreements[:20],
    }


def report_payload(
    window: WindowAnalysis,
    run: SolverRun,
    paid: payouts.RecordedRewards | Literal["slice"] | None = "slice",
) -> dict[str, Any]:
    """The report; `changed` still holds the results, which `write_report` turns into
    rows one at a time. `paid` is `run.paid`, sliced here unless the caller has it."""
    analysis = run.analysis
    if paid == "slice":
        paid = run.paid
    payload: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "sign_convention": SIGN_CONVENTION_ID,
        "network": window.network,
        "start": window.start,
        "end": window.end,
        "outcome_rule_description": OUTCOME_RULE[analysis.outcome_rule],
        "cow": run.cow._asdict() if run.cow is not None else None,
        "quote": _quote_payload(run.quote) if run.quote is not None else None,
        "paid": paid.to_payload() if paid is not None else None,
        "quote_window": (
            _quote_window_payload(window.quote_window)
            if window.quote_window is not None
            else None
        ),
    }
    payload.update(_fields_and_properties(analysis, _ANALYSIS_PROPERTIES))
    return payload


def write_report(
    path: str,
    window: WindowAnalysis,
    run: SolverRun,
    paid: payouts.RecordedRewards | Literal["slice"] | None = "slice",
) -> None:
    """One top-level key per line and one changed auction per line, written as it is
    encoded. `indent` would make `json` fall back to its pure-Python encoder, several
    times slower on a large report."""
    encode = json.JSONEncoder(default=_json_default).encode
    items = list(report_payload(window, run, paid).items())
    with open(path, "w") as handle:
        handle.write("{\n")
        for index, (key, value) in enumerate(items):
            comma = "," if index < len(items) - 1 else ""
            if key != "changed":
                handle.write(f"  {encode(key)}: {encode(value)}{comma}\n")
                continue
            results = cast("list[AuctionCounterfactual]", value)
            handle.write(f"  {encode(key)}: [")
            for row, result in enumerate(results):
                handle.write(",\n    " if row else "\n    ")
                handle.write(encode(_fields_and_properties(result, _AUCTION_PROPERTIES)))
            handle.write(f"\n  ]{comma}\n" if results else f"]{comma}\n")
        handle.write("}\n")
