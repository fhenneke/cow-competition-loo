"""Command line entry point.

Two subcommands:

- `validate` (M1) reproduces the recorded competition over a date window and accounts for
  every difference. It is the gate the counterfactual rests on.
- `analyse` (M2) removes one solver from those auctions, re-runs the competition and
  reports what users and the protocol would have lost or saved.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import counterfactual, db, extract, validate
from .primitives import MAX_WINNERS, wrapped_native_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser(
        "validate", help="reproduce recorded winners, filter and reference scores"
    )
    check.add_argument("--network", default="mainnet")
    check.add_argument("--start", required=True, help="inclusive, e.g. 2026-08-01")
    check.add_argument("--end", required=True, help="exclusive, e.g. 2026-08-02")
    check.add_argument("--max-winners", type=int, default=MAX_WINNERS)
    check.add_argument("--limit", type=int, help="only the first N auctions in the window")
    check.add_argument(
        "--cross-check-surplus",
        action="store_true",
        help="also diff per-order surplus against int_backend_data__proposed_solution_data",
    )
    check.add_argument("--out", help="write the full report as JSON")
    check.add_argument(
        "--show", type=int, default=20, help="how many mismatching auctions to print"
    )
    check.set_defaults(func=run_validate)

    analyse = sub.add_parser(
        "analyse", help="remove one solver, re-run the competition, and diff the outcomes"
    )
    analyse.add_argument("--network", default="mainnet")
    analyse.add_argument(
        "--solver",
        required=True,
        help="solver name (as in dune_data__cow_protocol__solvers) or submission address",
    )
    analyse.add_argument("--start", required=True, help="inclusive, e.g. 2026-08-01")
    analyse.add_argument("--end", required=True, help="exclusive, e.g. 2026-08-02")
    analyse.add_argument(
        "--mode",
        choices=("score", "surplus"),
        default="score",
        help="what ranks a solution: recorded score (default) or user surplus",
    )
    analyse.add_argument(
        "--outcome-rule",
        choices=("inherited", "observed", "proposed"),
        default="inherited",
        help=(
            "what a replacement winner is taken to do: inherit the settlement of the slot "
            "it displaced (default), assume it settles (pessimistic bound), or assume "
            "every winner on both sides settles (optimistic bound). See PLAN.md section 5"
        ),
    )
    analyse.add_argument("--max-winners", type=int, default=MAX_WINNERS)
    analyse.add_argument("--limit", type=int, help="only the first N auctions in the window")
    analyse.add_argument("--out", help="write the full report as JSON")
    analyse.add_argument(
        "--show", type=int, default=10, help="how many changed auctions to print"
    )
    analyse.set_defaults(func=run_analyse)

    args = parser.parse_args(argv)
    return args.func(args)


def run_validate(args) -> int:
    weth = wrapped_native_token(args.network)
    conn = db.connect(args.network)

    try:
        window = extract.auctions_in_window(conn, args.start, args.end)
        auction_ids = [auction_id for auction_id, _ in window]
        if args.limit:
            auction_ids = auction_ids[: args.limit]

        print(
            f"{len(auction_ids)} auctions in [{args.start}, {args.end}) on {args.network}",
            file=sys.stderr,
        )
        if not auction_ids:
            return 1

        db_surplus = {}
        if args.cross_check_surplus:
            db_surplus = extract.load_db_order_surplus(conn, auction_ids)
            print(f"{len(db_surplus)} DB surplus rows for cross-check", file=sys.stderr)
            if not db_surplus:
                # `int_backend_data__proposed_solution_data` lags the staging tables by
                # over a week. Silently skipping a check that was explicitly asked for
                # would let the report read as though the surplus path was verified.
                print(
                    "ERROR: --cross-check-surplus was requested but "
                    "int_backend_data__proposed_solution_data covers none of this "
                    "window. Pick an earlier window or drop the flag.",
                    file=sys.stderr,
                )
                return 3

        summary = validate.Summary()
        surplus = validate.SurplusCrossCheck()

        for bundle in extract.load_auctions(conn, auction_ids):
            summary.add(
                validate.check_auction(bundle, weth, max_winners=args.max_winners)
            )
            if db_surplus:
                surplus.merge(validate.check_surplus_against_db(bundle, db_surplus))
    finally:
        conn.close()

    report_summary(summary, args)
    if db_surplus:
        report_surplus(surplus)

    if args.out:
        write_json(args.out, summary, surplus)
        print(f"\nfull report written to {args.out}", file=sys.stderr)

    # The M1 gate is that every difference has a named cause, not that there are no
    # differences: the per-pair proxy of PLAN.md §2 is a known and accepted one.
    return 0 if not summary.unexplained else 2


def run_analyse(args) -> int:
    weth = wrapped_native_token(args.network)
    conn = db.connect(args.network)

    try:
        try:
            addresses, matches = extract.resolve_solver(
                conn, args.solver, args.start, args.end
            )
        except extract.SolverResolutionError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 4
        for match in matches:
            print(
                f"solver {args.solver!r} -> {match.address}  {match.name} "
                f"({match.environment}, active={match.active})  "
                f"{match.solutions} solutions in {match.auctions_bid} auctions, "
                f"{match.winning_solutions} winning",
                file=sys.stderr,
            )
        if len(matches) > 1:
            # A key rotation: the same competitor under two submission addresses. All of
            # them have to go together, or reference scores would treat one half of the
            # rotation as a rival of the other.
            print(
                f"note: {len(matches)} addresses resolved; removing all of them as one "
                f"solver",
                file=sys.stderr,
            )

        window = extract.auctions_in_window(conn, args.start, args.end)
        auction_ids = [auction_id for auction_id, _ in window]
        if args.limit:
            auction_ids = auction_ids[: args.limit]
        print(
            f"{len(auction_ids)} auctions in [{args.start}, {args.end}) on {args.network}",
            file=sys.stderr,
        )
        if not auction_ids:
            return 1

        settled: dict[int, dict[int, extract.Settlement]] = {}
        if args.outcome_rule != "proposed":
            settled = extract.load_settlement_outcomes(conn, auction_ids)
            print(
                f"settlement outcomes for {sum(len(v) for v in settled.values())} "
                f"winning solutions across {len(settled)} auctions",
                file=sys.stderr,
            )

        analysis = counterfactual.Analysis(
            solver=args.solver,
            addresses=addresses,
            mode=args.mode,
            outcome_rule=args.outcome_rule,
        )
        try:
            for bundle in extract.load_auctions(conn, auction_ids):
                analysis.add(
                    counterfactual.analyse_auction(
                        bundle,
                        weth,
                        addresses,
                        mode=args.mode,
                        max_winners=args.max_winners,
                        outcome_rule=args.outcome_rule,
                        settled=settled.get(bundle.auction_id, {}),
                    )
                )
        except counterfactual.MissingSettlementError as error:
            # The settlement source does not reach this window. Reporting a partial run
            # would understate baseline surplus in exactly the auctions it dropped.
            print(f"ERROR: {error}", file=sys.stderr)
            return 5
    finally:
        conn.close()

    report_analysis(analysis, args)

    if args.out:
        write_analysis_json(args.out, analysis, args)
        print(f"\nfull report written to {args.out}", file=sys.stderr)

    if not analysis.auctions_with_solver:
        # `resolve_solver` checks the whole window, so `--limit` can still slice down to
        # auctions the solver never bid in. Every delta is then trivially zero, which reads
        # as "removing this solver costs users nothing" — the one misreading worth an exit
        # code. Checked here rather than at resolution time so it holds for any reason the
        # solver ends up absent.
        print(
            f"\nERROR: {args.solver} did not bid in any of the "
            f"{analysis.auctions} auctions analysed, so every delta above is trivially "
            f"zero rather than a finding. Widen the window or drop --limit.",
            file=sys.stderr,
        )
        return 4

    # A valuation failure means an auction could not be arbitrated at all, so its
    # contribution to every number above is silently missing. M1 measured zero of them,
    # so any at all is worth a non-zero exit rather than a line in the report.
    return 2 if analysis.auctions_skipped else 0


def eth(wei: int, places: int = 6) -> str:
    """Format native wei as a decimal string, by integer arithmetic only."""
    sign = "-" if wei < 0 else ""
    scaled = abs(wei) * 10**places // 10**18
    return f"{sign}{scaled // 10**places}.{scaled % 10**places:0{places}d}"


def pct(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "n/a"


def report_analysis(analysis: counterfactual.Analysis, args) -> None:
    total = analysis.auctions
    print(f"\n=== leave one out: {analysis.solver} ===")
    print(f"window          {args.start} .. {args.end} on {args.network}")
    print(f"ranking mode    {analysis.mode}")
    print(f"outcome rule    {analysis.outcome_rule} — {counterfactual.OUTCOME_RULE[analysis.outcome_rule]}")

    print(f"\nauctions in window            {total}")
    print(
        f"  solver bid                  {analysis.auctions_with_solver} "
        f"({pct(analysis.auctions_with_solver, total)})"
    )
    print(f"  solver won (recomputed)     {analysis.auctions_solver_won_baseline}")
    print(f"  solver won (recorded)       {analysis.auctions_solver_won_db}")
    print(f"  winner set changed          {analysis.auctions_winner_set_changed}")
    print(f"  fairness filter relaxed     {analysis.auctions_filter_relaxed}")
    print(f"    a newly kept solution won {analysis.auctions_newly_kept_won}")
    print(f"  baseline differs from DB    {analysis.auctions_baseline_differs_from_db}")
    if analysis.auctions_skipped:
        print(f"  ABANDONED (valuation)       {analysis.auctions_skipped}")

    print(f"\nuser surplus with solver      {eth(analysis.surplus_base)} ETH")
    print(f"user surplus without solver   {eth(analysis.surplus_loo)} ETH")
    print(f"delta surplus                 {eth(analysis.delta_surplus)} ETH")

    print(f"\nuser orders compared          {analysis.orders_compared}")
    print(
        f"  executed only with solver   {analysis.orders_only_with_solver} "
        f"({pct(analysis.orders_only_with_solver, analysis.orders_compared)})"
    )
    print(
        f"  executed only without       {analysis.orders_only_without_solver}"
        "   <- a batch the solver was blocking, or a settlement it lost"
    )
    print(
        f"  lost to a failed settlement {analysis.orders_unsettled_base}"
        "   <- the baseline's own reverted batches"
    )
    print(f"JIT orders only with solver   {analysis.jit_orders_only_with_solver}")
    print(f"JIT orders only without       {analysis.jit_orders_only_without_solver}")
    if analysis.outcome_rule == "observed" and analysis.orders_unsettled_base:
        print(
            "\nNOTE: under --outcome-rule observed the baseline is charged for its own\n"
            "settlement failures while every counterfactual replacement is assumed to\n"
            "settle, so this delta is a lower bound. --outcome-rule inherited (the default)\n"
            "hands a replacement the settlement of the slot it displaced instead."
        )

    print("\nwinners that never won for real, so nothing was recorded about them:")
    print(
        f"  baseline side               {analysis.replacements_base}"
        "   <- our baseline winner differs from the record"
    )
    print(
        f"  counterfactual side         {analysis.replacements_loo}"
        "   <- a replacement winner, unavoidable"
    )
    if analysis.outcome_rule == "inherited":
        print(
            f"    settlement inherited from the displaced slot, reverting "
            f"{analysis.inherited_reverts_loo} of them"
        )
    print(
        f"  settlement assumed anyway   {analysis.orphans_base + analysis.orphans_loo}"
        "   <- nothing held that pair; the mapping failures"
    )
    print(
        f"\nsolver helped set another solver's reference score in "
        f"{analysis.reference_influence} auction-solver pairs"
        "  <- what moves in M3"
    )

    if analysis.valuation_failures:
        print(f"\nvaluation failures ({len(analysis.valuation_failures)}):")
        for auction_id, uid, error in analysis.valuation_failures[:10]:
            print(f"    auction {auction_id} uid={uid}  {error}")

    moves = analysis.largest_moves(args.show)
    if moves:
        print(f"\n--- {len(moves)} largest changed auctions by |delta surplus| ---")
        for result in moves:
            print_counterfactual(result)


def print_counterfactual(result: counterfactual.AuctionCounterfactual) -> None:
    print(
        f"\nauction {result.auction_id}  {result.n_solutions} solutions  "
        f"delta={eth(result.delta_surplus)} ETH  "
        f"winners {sorted(result.baseline_winner_uids)} -> "
        f"{sorted(result.loo_winner_uids)}"
        + (f"  un-filtered={sorted(result.un_filtered_uids)}" if result.filter_relaxed else "")
    )
    for diff in result.order_diffs:
        if not diff.delta_surplus and diff.executed_base == diff.executed_loo:
            continue
        print(
            f"    order {diff.order_uid[:16]} "
            f"executed {diff.executed_base}->{diff.executed_loo} "
            f"surplus {diff.surplus_base}->{diff.surplus_loo} "
            f"solver {(diff.solver_base or '-')[:8]}->{(diff.solver_loo or '-')[:8]}"
            + ("" if diff.contributes else "  (JIT, not user surplus)")
        )


def write_analysis_json(path: str, analysis: counterfactual.Analysis, args) -> None:
    payload = {
        "solver": analysis.solver,
        "addresses": sorted(analysis.addresses),
        "network": args.network,
        "start": args.start,
        "end": args.end,
        "mode": analysis.mode,
        "settlement": analysis.outcome_rule,
        "outcome_rule": counterfactual.OUTCOME_RULE[analysis.outcome_rule],
        "auctions": analysis.auctions,
        "auctions_with_solver": analysis.auctions_with_solver,
        "auctions_skipped": analysis.auctions_skipped,
        "auctions_solver_won_baseline": analysis.auctions_solver_won_baseline,
        "auctions_solver_won_db": analysis.auctions_solver_won_db,
        "auctions_winner_set_changed": analysis.auctions_winner_set_changed,
        "auctions_filter_relaxed": analysis.auctions_filter_relaxed,
        "auctions_newly_kept_won": analysis.auctions_newly_kept_won,
        "auctions_baseline_differs_from_db": analysis.auctions_baseline_differs_from_db,
        "surplus_base_wei": str(analysis.surplus_base),
        "surplus_loo_wei": str(analysis.surplus_loo),
        "delta_surplus_wei": str(analysis.delta_surplus),
        "orders_compared": analysis.orders_compared,
        "orders_only_with_solver": analysis.orders_only_with_solver,
        "orders_only_without_solver": analysis.orders_only_without_solver,
        "orders_unsettled_base": analysis.orders_unsettled_base,
        "jit_orders_only_with_solver": analysis.jit_orders_only_with_solver,
        "jit_orders_only_without_solver": analysis.jit_orders_only_without_solver,
        "replacements_base": analysis.replacements_base,
        "replacements_loo": analysis.replacements_loo,
        "inherited_reverts_loo": analysis.inherited_reverts_loo,
        "orphans_base": analysis.orphans_base,
        "orphans_loo": analysis.orphans_loo,
        "reference_influence": analysis.reference_influence,
        "valuation_failures": analysis.valuation_failures,
        "changed": [
            {
                "auction_id": r.auction_id,
                "n_solutions": r.n_solutions,
                "delta_surplus_wei": str(r.delta_surplus),
                "solver_won_baseline": r.solver_won_baseline,
                "solver_won_db": r.solver_won_db,
                "baseline_winner_uids": sorted(r.baseline_winner_uids),
                "loo_winner_uids": sorted(r.loo_winner_uids),
                "baseline_winning_total": str(r.baseline_winning_total),
                "loo_winning_total": str(r.loo_winning_total),
                "baseline_reference_scores": {
                    k: str(v) for k, v in r.baseline_reference_scores.items()
                },
                "loo_reference_scores": {
                    k: str(v) for k, v in r.loo_reference_scores.items()
                },
                "solver_set_reference_for": sorted(r.solver_set_reference_for),
                "un_filtered_uids": sorted(r.un_filtered_uids),
                "un_filtered_winner_uids": sorted(r.un_filtered_winner_uids),
                "replacements_base": sorted(r.replacements_base),
                "replacements_loo": sorted(r.replacements_loo),
                "inherited_reverts_loo": sorted(r.inherited_reverts_loo),
                "orphans_base": sorted(r.orphans_base),
                "orphans_loo": sorted(r.orphans_loo),
                "baseline_matches_db": r.baseline_matches_db,
                "order_diffs": [
                    {
                        **{
                            k: (str(v) if isinstance(v, int) and k.startswith("surplus") else v)
                            for k, v in asdict(d).items()
                        },
                        "delta_surplus_wei": str(d.delta_surplus),
                    }
                    for d in r.order_diffs
                ],
            }
            for r in analysis.changed
        ],
    }
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2, default=str)


def report_summary(summary: validate.Summary, args) -> None:
    total = summary.auctions
    print(f"\n=== {total} auctions, {summary.solutions} solutions ===")
    print(f"winner set matches:         {summary.auctions_winners_match}/{total}")
    print(f"filtered-out set matches:   {summary.auctions_filter_match}/{total}")
    print(
        f"reference scores (ours):    "
        f"{summary.auctions_reference_match_recomputed}/{total}"
    )
    print(
        f"reference scores (observed):"
        f" {summary.auctions_reference_match_observed}/{total}"
    )
    print(f"pick on observed kept set:  {summary.auctions_pick_match_observed}/{total}")
    print(f"valuation failures:         {summary.valuation_failures}")
    print(
        f"\nfilter differs from DB:     "
        f"{summary.multi_pair_filter_mismatch}/{summary.multi_pair_solutions} "
        f"multi-pair solutions ({summary.proxy_error_rate:.4%})"
    )
    print(
        "multi-pair bracket:         "
        + ", ".join(f"{k}={v}" for k, v in sorted(summary.bracket_counts.items()))
    )
    if summary.filter_causes:
        print(
            "filter difference cause:    "
            + ", ".join(f"{k}={v}" for k, v in sorted(summary.filter_causes.items()))
        )

    if not summary.mismatched:
        print("\nno disagreements.")
    else:
        kinds: dict[str, int] = {}
        for report in summary.mismatched:
            kinds[report.disagreement] = kinds.get(report.disagreement, 0) + 1
        print(
            f"\n{len(summary.mismatched)} auctions disagree: "
            + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
        )

    if summary.unexplained:
        print(f"\nUNEXPLAINED in {len(summary.unexplained)} auctions — M1 gate not met:")
        reasons: dict[str, int] = {}
        for _, reason in summary.unexplained:
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in sorted(reasons.items()):
            examples = [a for a, r in summary.unexplained if r == reason][:5]
            print(f"    {reason}: {count}  e.g. {examples}")
    else:
        print("\nevery difference has a named cause — M1 gate met.")

    if summary.mismatched:
        print(f"\n--- first {min(args.show, len(summary.mismatched))} ---")
        for report in summary.mismatched[: args.show]:
            print_report(report)


def print_report(report) -> None:
    db_only, ours_only = report.swapped
    print(
        f"\nauction {report.auction_id}  {report.disagreement}  "
        f"{report.n_solutions} solutions  "
        f"partially_fillable={report.any_partially_fillable}  "
        f"score_gap={report.score_gap}  "
        f"unexplained={report.unexplained}"
    )
    for failure in report.valuation_failures:
        print(f"    valuation failure  uid={failure[0]}  {failure[1]}")
    for check in report.checks:
        if not (check.winner_differs or check.filter_differs):
            continue
        print(
            f"    uid={check.uid:<3} solver={check.solver[:8]} "
            f"score={check.db_score:<22} pairs={check.n_pairs} "
            f"winner {check.db_winner}->{check.our_winner} "
            f"filtered {check.db_filtered}->{check.our_filtered} "
            f"partial={check.partially_fillable} "
            f"bracket={check.bracket} cause={check.filter_cause}"
        )
    for solver in sorted(set(report.reference_db) | set(report.reference_recomputed)):
        ours = report.reference_recomputed.get(solver)
        observed = report.reference_observed.get(solver)
        theirs = report.reference_db.get(solver)
        if ours != theirs or observed != theirs:
            print(
                f"    reference {solver[:8]}  db={theirs}  ours={ours}  "
                f"from_observed_ranking={observed}"
            )


def report_surplus(surplus: validate.SurplusCrossCheck) -> None:
    print(
        f"\nsurplus cross-check vs int_backend_data__proposed_solution_data: "
        f"{surplus.agreed}/{surplus.compared} orders agree "
        f"({surplus.skipped} unvaluable, excluded)"
    )
    diffs = [ours - theirs for _, _, _, ours, theirs in surplus.mismatches]
    if diffs:
        print(
            f"    differences: min={min(diffs)} max={max(diffs)} — "
            f"one-atom differences in our favour are the dbt model's rounding, "
            f"see docs/analytics-db.md"
        )
    for auction_id, uid, order_uid, ours, theirs in surplus.mismatches[:10]:
        print(
            f"    auction {auction_id} uid={uid} order={order_uid[:16]} "
            f"ours={ours} db={theirs} diff={ours - theirs}"
        )


def write_json(path: str, summary, surplus: validate.SurplusCrossCheck) -> None:
    payload = {
        "auctions": summary.auctions,
        "solutions": summary.solutions,
        "auctions_winners_match": summary.auctions_winners_match,
        "auctions_filter_match": summary.auctions_filter_match,
        "auctions_reference_match_recomputed": summary.auctions_reference_match_recomputed,
        "auctions_reference_match_observed": summary.auctions_reference_match_observed,
        "auctions_pick_match_observed": summary.auctions_pick_match_observed,
        "valuation_failures": summary.valuation_failures,
        "multi_pair_solutions": summary.multi_pair_solutions,
        "multi_pair_filter_mismatch": summary.multi_pair_filter_mismatch,
        "proxy_error_rate": summary.proxy_error_rate,
        "bracket_counts": summary.bracket_counts,
        "filter_causes": summary.filter_causes,
        "unexplained": summary.unexplained,
        "surplus_cross_check": {
            "requested": bool(surplus.compared or surplus.skipped),
            "compared": surplus.compared,
            "agreed": surplus.agreed,
            "skipped": surplus.skipped,
            "mismatches": surplus.mismatches[:1000],
        },
        "mismatched": [
            {
                "auction_id": r.auction_id,
                "disagreement": r.disagreement,
                "n_solutions": r.n_solutions,
                "any_partially_fillable": r.any_partially_fillable,
                "score_gap": r.score_gap,
                "unexplained": r.unexplained,
                "filter_causes": r.filter_causes,
                "valuation_failures": r.valuation_failures,
                "reference_db": r.reference_db,
                "reference_recomputed": r.reference_recomputed,
                "reference_observed": r.reference_observed,
                "checks": [
                    {
                        **{
                            k: v
                            for k, v in asdict(c).items()
                            if k not in ("pair_values", "pair_surplus")
                        },
                        "filter_cause": c.filter_cause,
                    }
                    for c in r.checks
                    if c.winner_differs or c.filter_differs
                ],
            }
            for r in summary.mismatched
        ],
    }
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2, default=str)


if __name__ == "__main__":
    raise SystemExit(main())
