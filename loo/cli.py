"""Command line entry point.

M1 exposes one subcommand, `validate`, which reproduces the recorded competition over a
date window and reports every difference. `analyse` (the leave-one-out run itself)
arrives with M2.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import db, extract, validate
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
    check.add_argument(
        "--pair-proxy",
        default="scaled",
        choices=["scaled", "raw"],
        help="how a multi-pair solution's score is split across pairs (PLAN.md §2)",
    )
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
                validate.check_auction(
                    bundle,
                    weth,
                    pair_proxy=args.pair_proxy,
                    max_winners=args.max_winners,
                )
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


def report_summary(summary: validate.Summary, args) -> None:
    total = summary.auctions
    print(f"\n=== {total} auctions, {summary.solutions} solutions ===")
    print(f"pair proxy:                 {args.pair_proxy}")
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
        "\npair decomposition basis:   "
        + ", ".join(f"{k}={v}" for k, v in sorted(summary.basis_counts.items()))
    )
    print(
        f"filter proxy error:         "
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
            f"score={check.db_score:<22} pairs={check.n_pairs} basis={check.basis:<7} "
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
        "basis_counts": summary.basis_counts,
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
