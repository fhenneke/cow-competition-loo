"""What the CLI says about a Solana window: native amounts in SOL on a 1e9 unit, the
recorded rewards and the COW conversion left unknown rather than zero, the questions
Solana has no table for refused, solvers named by base58 address, and the comparison
converting lamports to USD at 1e9 to the SOL."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from statistics import median

from loo import aggregate, extract
from loo.counterfactual import Analysis

from . import oracle, report_checks, runner_up
from .cli_checks import usd_rates, usd_text
from .harness import Run, Sim
from .simdb import base58
from .world import World


def check_cli(
    sim: Sim, world: World, tmp_path: Path, entries: list[str],
    direct: dict[tuple[str, str], Analysis], analyse: Run,
) -> None:
    assert world.solana
    sol = world.decimals

    # Neither paid rewards nor COW: Solana's weekly accounting is not read, and an
    # unknown amount is never a zero.
    assert "recorded rewards and COW conversion: not available on solana" in analyse.err
    paths = sorted(str(p) for p in tmp_path.glob("*.*.json"))
    for path in paths:
        payload = json.loads(Path(path).read_text())
        assert payload["network"] == "solana"
        assert payload["paid"] is None and payload["cow"] is None
        report = aggregate.load_report(path)
        analysis = direct[(report.solver, report.outcome_rule)]
        report_checks.check_report_properties(report, payload, analysis)
        runner_up.check_report(report, payload, analysis)

    # The printout: every native amount in SOL, nine decimals to the token.
    assert " ETH" not in analyse.out
    for analysis in direct.values():
        assert (f"user surplus with solver      {oracle.eth_text(analysis.surplus_base, 6, sol)}"
                f" SOL") in analyse.out
        assert (f"delta surplus                 {oracle.eth_text(analysis.delta_surplus, 6, sol)}"
                f" SOL") in analyse.out

    # The comparison, in SOL and, from the auctions' own stablecoins, in USD.
    windows = aggregate.group_reports([aggregate.load_report(p) for p in paths])
    rows = dict(aggregate.comparison(windows).rows)
    for column, window in enumerate(windows):
        head = window.headline
        assert rows["Δsurplus (inherited)"][column] == (
            oracle.signed_eth_text(head.delta_surplus, 4, sol) + " SOL")
        assert rows["rewards paid to the solver (recorded)"][column] == "not recorded"
    usd_run = sim.cli("compare", *paths)
    assert usd_run.code == 0, usd_run.err
    moved = {m.auction_id for w in windows for m in w.headline.moves}
    rates = usd_rates(world, moved)
    for window in windows:
        head = window.headline
        if not rates or not head.moves:
            continue
        fallback = median(rates.values())
        usd = sum((Decimal(m.delta_surplus) * rates.get(m.auction_id, fallback)
                   for m in head.moves), Decimal(0)) / Decimal(10**sol)
        cell = oracle.signed_eth_text(head.delta_surplus, 4, sol) + " SOL (" + usd_text(usd) + ")"
        assert cell in usd_run.out

    # What Solana has no table for is refused, not answered with nothing.
    cross = sim.cli("validate", *sim.window_args(), "--max-winners", str(world.max_winners),
                    "--cross-check-surplus")
    assert cross.code == 3 and "solana does not have" in cross.err
    quoted = sim.cli("analyse", *sim.window_args(), "--solver", entries[0], "--quotes")
    assert quoted.code == 4 and "no quote data" in quoted.err

    out = tmp_path / "validate.out"
    checked = sim.cli("validate", *sim.window_args(), "--max-winners", str(world.max_winners),
                      "--out", str(out))
    assert checked.code == 0, checked.err
    payload = json.loads(out.read_text())
    assert payload["unexplained"] == [] and payload["valuation_failures"] == 0
    assert payload["auctions"] == len(world.auctions)

    # Solvers: the catalogue speaks base58, the bids hex, and either names one.
    conn = sim.conn()
    bidding = world.bidding_addresses()
    labels = {
        world.solver_of(a).name if world.solver_of(a).name != "Uncatalogued" else base58(a)
        for a in bidding
    }
    assert extract.all_solvers(conn, world.start, world.end) == sorted(labels)
    address = sorted(bidding)[0]
    addresses, _ = extract.resolve_solver(conn, base58(address), world.start, world.end)
    assert addresses == {address}
    addresses, _ = extract.resolve_solver(conn, "0x" + address, world.start, world.end)
    assert addresses == {address}
