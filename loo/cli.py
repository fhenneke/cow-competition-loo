"""Command line entry point.

Four subcommands:

- `analyse` removes one or more solvers from a window's auctions, re-runs the
  competition and reports what users and the protocol would have lost or saved.
- `compare` aggregates several `analyse --out` reports into the per-solver
  comparison table, with medians beside the sums and USD conversion.
- `validate` reproduces the recorded competition over a date window and accounts for
  every difference. It is the gate the counterfactual rests on.
- `validate-rewards` recomputes every solver's uncapped reward from the DB's own
  inputs and compares against `fct_solver_rewards_per_auction`. No approximation is in
  its path, so it must match exactly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from typing import cast

from . import aggregate, counterfactual, db, extract, rewards, run, validate
from .aggregate import cow_amount, eth, pct
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

    check_rewards = sub.add_parser(
        "validate-rewards",
        help="reproduce uncapped rewards from recorded inputs and diff against the mart",
    )
    check_rewards.add_argument("--network", default="mainnet")
    check_rewards.add_argument("--start", required=True, help="inclusive, e.g. 2026-08-01")
    check_rewards.add_argument("--end", required=True, help="exclusive, e.g. 2026-08-02")
    check_rewards.add_argument(
        "--limit", type=int, help="only the first N auctions in the window"
    )
    check_rewards.add_argument("--out", help="write the full report as JSON")
    check_rewards.add_argument(
        "--show", type=int, default=20, help="how many mismatching rows to print"
    )
    check_rewards.set_defaults(func=run_validate_rewards)

    analyse = sub.add_parser(
        "analyse",
        help="remove one or more solvers, re-run the competition, and diff the outcomes",
    )
    analyse.add_argument("--network", default="mainnet")
    analyse.add_argument(
        "--solver",
        action="append",
        required=True,
        help=(
            "solver name (as in dune_data__cow_protocol__solvers) or submission "
            "address; repeatable — every solver shares one extraction pass, so "
            "analysing several costs barely more than one"
        ),
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
        choices=("inherited", "assume-settled"),
        action="append",
        dest="outcome_rules",
        help=(
            "the settlement scenario: a replacement winner inherits the outcome of the "
            "slot it displaced (default), or every winner on both sides is assumed to "
            "settle in time. Repeatable — all rules share the one extraction pass, so "
            "asking for both costs barely more than one. See README.md, \"The outcome "
            "rule\""
        ),
    )
    analyse.add_argument(
        "--include-price-suspects",
        action="store_true",
        help=(
            "keep auctions whose native prices fail the two-sided sanity check in "
            "every statistic instead of excluding them (they are excluded by default "
            "because a wrong price fabricates score, surplus and rewards)"
        ),
    )
    analyse.add_argument("--max-winners", type=int, default=MAX_WINNERS)
    analyse.add_argument("--limit", type=int, help="only the first N auctions in the window")
    analyse.add_argument(
        "--out",
        help=(
            "write the full report as JSON; a {solver} placeholder is substituted "
            "with the solver's slug and a {rule} placeholder with the outcome rule — "
            "each is required when several of its values are given"
        ),
    )
    analyse.add_argument(
        "--show", type=int, default=10, help="how many changed auctions to print"
    )
    analyse.set_defaults(func=run_analyse)

    compare = sub.add_parser(
        "compare",
        help="aggregate analyse reports into the per-solver comparison table",
    )
    compare.add_argument(
        "reports",
        nargs="+",
        help="JSON files written by `analyse --out`; give every outcome-rule run of a "
        "solver-window together, and `inherited` must be among them",
    )
    compare.add_argument(
        "--skip-usd",
        action="store_true",
        help="no DB connection; omit the USD conversion columns",
    )
    compare.add_argument(
        "--markdown", action="store_true", help="render the table as GitHub markdown"
    )
    compare.add_argument("--out", help="also write the rendered comparison to a file")
    compare.set_defaults(func=run_compare)

    args = parser.parse_args(argv)
    return args.func(args)


def run_validate(args: argparse.Namespace) -> int:
    weth = wrapped_native_token(args.network)
    conn = db.connect(args.network)

    try:
        auction_ids = extract.auctions_in_window(conn, args.start, args.end)
        if args.limit:
            auction_ids = auction_ids[: args.limit]

        print(
            f"{len(auction_ids)} auctions in [{args.start}, {args.end}) on {args.network}",
            file=sys.stderr,
        )
        if not auction_ids:
            return 1

        db_surplus: dict[tuple[int, int, str], int] = {}
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
        missing_data: list[int] = []

        for bundle in extract.load_auctions(conn, auction_ids, missing_data=missing_data):
            summary.add(
                validate.check_auction(bundle, weth, max_winners=args.max_winners)
            )
            if db_surplus:
                surplus.merge(validate.check_surplus_against_db(bundle, db_surplus))
    finally:
        conn.close()

    if missing_data:
        print(
            f"{len(missing_data)} auctions excluded for missing order data "
            f"(an order in neither orders nor jit_orders — unsettled JIT, D17)",
            file=sys.stderr,
        )
    report_summary(summary, args)
    if db_surplus:
        report_surplus(surplus)

    if args.out:
        write_json(args.out, summary, surplus, missing_data)
        print(f"\nfull report written to {args.out}", file=sys.stderr)

    # The gate is that every difference has a named cause, not that there are no
    # differences: the per-pair surplus proxy is a known and accepted one.
    return 0 if not summary.unexplained else 2


def run_validate_rewards(args: argparse.Namespace) -> int:
    """The rewards gate: recorded winning solutions + settlement flags + recorded
    reference scores -> the reward formula -> compare with the dbt mart, row by row.

    Nothing in this path is approximated — the inputs are the mart's own — so unlike
    `validate` there is no accepted-difference category: anything but an exact match on
    every row is a transcription bug or a data problem, and the run exits non-zero.
    """
    conn = db.connect(args.network)
    try:
        auction_ids = extract.auctions_in_window(conn, args.start, args.end)
        if args.limit:
            auction_ids = auction_ids[: args.limit]
        print(
            f"{len(auction_ids)} auctions in [{args.start}, {args.end}) on {args.network}",
            file=sys.stderr,
        )
        if not auction_ids:
            return 1

        inputs = extract.load_reward_inputs(conn, auction_ids)
        references = extract.load_reference_scores(conn, auction_ids)
        fct = extract.load_fct_rewards(conn, auction_ids)
        print(
            f"{sum(len(v.wins) for v in inputs.values())} winning solutions, "
            f"{sum(len(v) for v in fct.values())} mart reward rows",
            file=sys.stderr,
        )
    finally:
        conn.close()

    validation = rewards.RewardValidation()
    for auction_id in auction_ids:
        auction_inputs = inputs.get(auction_id)
        try:
            ours = (
                rewards.uncapped_rewards(
                    auction_inputs.wins,
                    references.get(auction_id, {}),
                    lower_cap=auction_inputs.lower_cap,
                    excluded=auction_inputs.excluded,
                )
                if auction_inputs
                else {}
            )
        except rewards.MissingReferenceScoreError as error:
            # A winner without a reference score means the inputs themselves are broken —
            # comparing anything after that would attribute a data gap to the formula.
            print(f"ERROR: auction {auction_id}: {error}", file=sys.stderr)
            return 3
        validation.check_auction(auction_id, ours, fct.get(auction_id, {}))

    report_reward_validation(validation, args)

    if args.out:
        write_reward_validation_json(args.out, validation)
        print(f"\nfull report written to {args.out}", file=sys.stderr)

    if validation.mismatches:
        return 2
    if validation.auctions_missing_from_fct:
        return 3
    return 0


def report_reward_validation(
    validation: rewards.RewardValidation, args: argparse.Namespace
) -> None:
    print(
        f"\n=== uncapped rewards vs fct_solver_rewards_per_auction: "
        f"{validation.auctions} auctions, {validation.auctions_with_winners} with winners ==="
    )
    print(f"solver-reward rows:         {validation.rows}")
    print(f"rows matching exactly:      {validation.rows_matched}/{validation.rows}")

    if validation.auctions_missing_from_fct:
        missing = validation.auctions_missing_from_fct
        print(
            f"\nauctions absent from the mart entirely: {len(missing)} — a coverage gap "
            f"(the mart is incremental), not a formula disagreement. "
            f"e.g. {missing[:5]}"
        )

    if not validation.mismatches:
        if validation.gate_met:
            print("\nevery row matches — the rewards gate is met.")
        return

    fields: dict[str, int] = {}
    for mismatch in validation.mismatches:
        for name in mismatch.differing_fields:
            fields[name] = fields.get(name, 0) + 1
    print(
        f"\n{len(validation.mismatches)} rows disagree — the rewards gate is NOT met: "
        + ", ".join(f"{k}={v}" for k, v in sorted(fields.items()))
    )
    for mismatch in validation.mismatches[: args.show]:
        print(f"\nauction {mismatch.auction_id} solver {mismatch.solver[:8]}")
        for name in rewards.RewardMismatch.COMPARED_FIELDS:
            mine = getattr(mismatch.ours, name) if mismatch.ours else None
            theirs = getattr(mismatch.theirs, name) if mismatch.theirs else None
            marker = "  <-" if mine != theirs else ""
            print(f"    {name:<18} ours={mine}  db={theirs}{marker}")


def write_reward_validation_json(path: str, validation: rewards.RewardValidation) -> None:
    payload = {
        "auctions": validation.auctions,
        "auctions_with_winners": validation.auctions_with_winners,
        "rows": validation.rows,
        "rows_matched": validation.rows_matched,
        "gate_met": validation.gate_met,
        "auctions_missing_from_fct": validation.auctions_missing_from_fct,
        "mismatches": [
            {
                "auction_id": m.auction_id,
                "solver": m.solver,
                "differing_fields": list(m.differing_fields),
                "ours": reward_as_json(m.ours),
                "theirs": reward_as_json(m.theirs),
            }
            for m in validation.mismatches
        ],
    }
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2, default=str)


def reward_as_json(reward: rewards.SolverReward | None) -> dict[str, str | None] | None:
    """A `SolverReward` as JSON-safe strings, keeping `None` a null rather than 'None'."""
    if reward is None:
        return None
    return {
        k: (str(v) if v is not None else None) for k, v in asdict(reward).items()
    }


def report_path(template: str, solver: str, rule: str) -> str:
    """`--out` with `{solver}` substituted by the solver's slug and `{rule}` by the
    outcome rule."""
    slug = re.sub(r"[^a-z0-9]+", "-", solver.lower()).strip("-")
    return template.replace("{solver}", slug).replace("{rule}", rule)


def run_analyse(args: argparse.Namespace) -> int:
    # argparse's `choices` already constrains the values; the cast restores the
    # literal type it erased.
    rules = cast(
        "list[counterfactual.OutcomeRule]", args.outcome_rules or ["inherited"]
    )
    if len(args.solver) > 1 and args.out and "{solver}" not in args.out:
        print(
            "ERROR: --out needs a {solver} placeholder when several solvers are "
            "analysed, or each report would overwrite the last",
            file=sys.stderr,
        )
        return 4
    if len(rules) > 1 and args.out and "{rule}" not in args.out:
        print(
            "ERROR: --out needs a {rule} placeholder when several outcome rules are "
            "analysed, or each report would overwrite the last",
            file=sys.stderr,
        )
        return 4

    conn = db.connect(args.network)
    try:
        try:
            window = run.analyse_window(
                conn,
                args.solver,
                args.start,
                args.end,
                network=args.network,
                mode=args.mode,
                outcome_rules=tuple(rules),
                max_winners=args.max_winners,
                include_price_suspects=args.include_price_suspects,
                limit=args.limit,
                log=lambda message: print(message, file=sys.stderr),
            )
        except (extract.SolverResolutionError, ValueError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 4
        except counterfactual.MissingSettlementError as error:
            # The settlement source does not reach this window. Reporting a partial run
            # would understate baseline surplus in exactly the auctions it dropped.
            print(f"ERROR: {error}", file=sys.stderr)
            return 5
    finally:
        conn.close()

    if not window.auction_ids:
        return 1

    never_bid: list[str] = []
    skipped = False
    for solver_run in window.runs:
        report_analysis(solver_run.analysis, args, solver_run.cow)
        if args.out:
            path = report_path(
                args.out, solver_run.solver, solver_run.analysis.outcome_rule
            )
            run.write_report(path, window, solver_run)
            print(f"\nfull report written to {path}", file=sys.stderr)
        if (
            not solver_run.analysis.auctions_with_solver
            and solver_run.solver not in never_bid
        ):
            never_bid.append(solver_run.solver)
        skipped = skipped or bool(solver_run.analysis.auctions_skipped)

    if never_bid:
        # `resolve_solver` checks the whole window, so `--limit` can still slice down to
        # auctions the solver never bid in. Every delta is then trivially zero, which reads
        # as "removing this solver costs users nothing" — the one misreading worth an exit
        # code. Checked here rather than at resolution time so it holds for any reason the
        # solver ends up absent.
        print(
            f"\nERROR: {', '.join(never_bid)} did not bid in any of the "
            f"{len(window.auction_ids)} auctions analysed, so every delta above is "
            f"trivially zero rather than a finding. Widen the window or drop --limit.",
            file=sys.stderr,
        )
        return 4

    # A valuation failure means an auction could not be arbitrated at all, so its
    # contribution to every number above is silently missing. Validation measured zero
    # of them, so any at all is worth a non-zero exit rather than a line in the report.
    return 2 if skipped else 0


def run_compare(args: argparse.Namespace) -> int:
    """The per-solver comparison table, from `analyse --out` files.

    Pure post-processing of the reports; the only DB work is fetching the stablecoin
    prices behind the USD columns, and `--skip-usd` removes even that.
    """
    try:
        reports = [aggregate.load_report(path) for path in args.reports]
        windows = aggregate.group_reports(reports)
    except (ValueError, OSError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 4

    usd_by_network: dict[str, aggregate.UsdContext] = {}
    if not args.skip_usd:
        # One rate query per network, over the union of every report's changed
        # auctions — those are the only auctions that carry a non-zero delta.
        by_network: dict[str, set[int]] = {}
        for report in reports:
            by_network.setdefault(report.network, set()).update(
                move.auction_id for move in report.moves
            )
        for network, auction_ids in sorted(by_network.items()):
            conn = db.connect(network)
            try:
                rates = extract.load_usd_rates(conn, sorted(auction_ids), network)
            except KeyError as error:
                # No curated stablecoins for this network — USD is display sugar, so
                # say so and carry on rather than failing the comparison.
                print(f"note: no USD rates for {network}: {error}", file=sys.stderr)
                continue
            finally:
                conn.close()
            context = aggregate.usd_context(rates)
            if context is not None:
                usd_by_network[network] = context
                print(
                    f"USD rates for {len(rates)} {network} auctions, window median "
                    f"{aggregate.usd_amount(context.fallback)}/native",
                    file=sys.stderr,
                )

    table = aggregate.comparison(windows, usd_by_network)
    render = aggregate.render_markdown if args.markdown else aggregate.render_text
    rendered = render(table)

    caveats = "\n".join(
        f"{i}. {caveat}" for i, caveat in enumerate(aggregate.CAVEATS, 1)
    )
    output = f"{rendered}\n\ncaveats:\n{caveats}\n"
    print(output)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(output)
        print(f"comparison written to {args.out}", file=sys.stderr)
    return 0


def report_analysis(
    analysis: counterfactual.Analysis,
    args: argparse.Namespace,
    cow: run.CowConversion | None = None,
) -> None:
    total = analysis.auctions
    print(f"\n=== leave one out: {analysis.solver} ===")
    print(f"window          {args.start} .. {args.end} on {args.network}")
    print(f"ranking mode    {analysis.mode}")
    print(f"outcome rule    {analysis.outcome_rule} — {counterfactual.OUTCOME_RULE[analysis.outcome_rule]}")

    missing = analysis.missing_data_auctions
    print(f"\nauctions in window            {total + len(missing)}")
    if missing:
        print(
            f"  missing order data, excluded {len(missing)} "
            f"({pct(len(missing), total + len(missing))})"
            "   <- an order in neither orders nor jit_orders: unsettled JIT, "
            "unrecoverable (D17)"
        )
        print(f"  analysed                    {total}")
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

    suspects = analysis.price_suspect_auctions
    if suspects:
        verb = "flagged but KEPT" if not analysis.exclude_price_suspect else "excluded"
        print(
            f"  price-suspect, {verb}     {len(suspects)} ({pct(len(suspects), total)})"
            "   <- a native price fails the 2x two-sided check"
        )
        for start in range(0, len(suspects), 6):
            print("      " + ", ".join(str(a) for a in suspects[start : start + 6]))
        if not analysis.exclude_price_suspect:
            print(
                "      WARNING: --include-price-suspects keeps fabricated prices in "
                "every number above and below."
            )

    print(f"\nuser surplus with solver      {eth(analysis.surplus_base)} ETH")
    print(f"user surplus without solver   {eth(analysis.surplus_loo)} ETH")
    print(
        f"delta surplus                 {eth(analysis.delta_surplus)} ETH"
        "   <- every delta is counterfactual minus actual"
    )

    if analysis.mode == "score":
        print(f"\nuncapped rewards with solver  {eth(analysis.rewards_base)} ETH")
        print(f"uncapped rewards without      {eth(analysis.rewards_loo)} ETH")
        delta_line = f"delta rewards                 {eth(analysis.delta_rewards)} ETH"
        if cow is not None and not cow.auctions_without_rate:
            delta_line += f"  = {cow_amount(cow.cow_wei)} COW"
        print(delta_line)
        print(
            f"  the solver's own reward     {eth(analysis.removed_reward_base)} ETH"
        )
        print(
            f"  rivals' rewards change      "
            f"{eth(analysis.delta_rewards + analysis.removed_reward_base)} ETH"
            "   <- positive means rivals earn more once the solver is gone"
        )
        print(f"  auctions where a reward moved {analysis.auctions_rewards_moved}")
        print(
            f"  negative uncapped rewards   "
            f"{analysis.negative_rewards_base} base ({eth(analysis.negative_reward_sum_base)} ETH) / "
            f"{analysis.negative_rewards_loo} loo ({eth(analysis.negative_reward_sum_loo)} ETH)"
        )
        print(
            "  NOTE: uncapped is the mechanism's accounting, not a payout — the real\n"
            "  payment clamps into the reward caps. The capped estimate below is the\n"
            "  payout-scale answer."
        )

        print(
            f"\ncapped rewards (estimate)     {eth(analysis.rewards_base_capped)} ETH with, "
            f"{eth(analysis.rewards_loo_capped)} ETH without"
        )
        capped_line = (
            f"delta rewards (capped)        {eth(analysis.delta_rewards_capped)} ETH"
        )
        if cow is not None and not cow.auctions_without_rate:
            capped_line += f"  = {cow_amount(cow.cow_wei_capped)} COW"
        print(capped_line)
        print(
            f"  over {analysis.auctions_capped} auctions"
            + (
                f"; {analysis.auctions_capped_skipped} skipped, no cap estimate"
                if analysis.auctions_capped_skipped
                else ""
            )
        )
        print(
            f"  a replacement inherits the displaced slot's cap (realised fees follow "
            f"the orders); double-inherited {analysis.cap_double_inherited}, "
            f"orphans {analysis.cap_orphans}"
        )
        if cow is not None and cow.auctions_without_rate:
            print(
                f"  COW conversion unavailable: {cow.auctions_without_rate} auctions "
                f"({eth(cow.native_without_rate)} ETH uncapped, "
                f"{eth(cow.capped_without_rate)} ETH capped) fall in an accounting "
                f"period with no snapshotted rate yet"
                + (
                    f"; the other {eth(cow.converted_native)} ETH converts to "
                    f"{cow_amount(cow.cow_wei)} COW"
                    if cow.converted_native
                    else ""
                )
            )
    else:
        print(
            "\nrewards not computed: the reward formula's quantities are scores, and "
            "this run ranked on user surplus"
        )

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
        "   <- the baseline's own batches that did not land in time"
    )
    print(
        f"    of which merely late     {analysis.orders_lost_to_lateness}"
        "   <- really filled; surplus discarded by choice"
    )
    print(f"JIT orders only with solver   {analysis.jit_orders_only_with_solver}")
    print(f"JIT orders only without       {analysis.jit_orders_only_without_solver}")

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
        "  <- where its removal moves a reward"
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
        + (
            f"delta_rewards={eth(result.delta_rewards)} ETH  "
            if result.baseline_rewards or result.loo_rewards
            else ""
        )
        + f"winners {sorted(result.baseline_winner_uids)} -> "
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


def report_summary(summary: validate.Summary, args: argparse.Namespace) -> None:
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
        print(f"\nUNEXPLAINED in {len(summary.unexplained)} auctions — the validation gate is NOT met:")
        reasons: dict[str, int] = {}
        for _, reason in summary.unexplained:
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in sorted(reasons.items()):
            examples = [a for a, r in summary.unexplained if r == reason][:5]
            print(f"    {reason}: {count}  e.g. {examples}")
    else:
        print("\nevery difference has a named cause — the validation gate is met.")

    if summary.mismatched:
        print(f"\n--- first {min(args.show, len(summary.mismatched))} ---")
        for report in summary.mismatched[: args.show]:
            print_report(report)


def print_report(report: validate.AuctionReport) -> None:
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


def write_json(
    path: str,
    summary: validate.Summary,
    surplus: validate.SurplusCrossCheck,
    missing_data: list[int] | None = None,
) -> None:
    payload = {
        "missing_data_auctions": missing_data or [],
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
