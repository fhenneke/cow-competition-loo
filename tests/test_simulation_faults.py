"""The simulation with something broken: the connection, the ClickHouse endpoint, a table
that lost rows, or the arguments. Each fault must surface as the error or the exit code
`loo` promises for it — never as a clean run over silently missing data."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import psycopg2
import pytest
from sim import expect
from sim.harness import Run, Sim, simulate
from sim.simch import SimClickHouse
from sim.simdb import Faults
from sim.world import Knobs, World, auction_times, generate, sample_knobs

from loo import aggregate, ch, extract, payouts


def analyse_args(world: World, *solvers: str) -> list[str]:
    args = ["analyse", "--network", world.network, "--start", world.start, "--end",
            world.end, "--max-winners", str(world.max_winners)]
    for solver in solvers:
        args += ["--solver", solver]
    return args


def some_label(world: World, rng: random.Random) -> str:
    return rng.choice(sorted({world.solver_of(a).label for a in world.bidding_addresses()}))


def test_connection_faults(seed: int) -> None:
    """A connection lost or a statement timed out at any query is exit 6 with the cause
    named; any other database error propagates as itself."""
    world = generate(seed)
    rng = random.Random(seed)
    label = some_label(world, rng)
    with simulate(world) as sim:
        clean = sim.cli(*analyse_args(world, label))
        queries = len(sim.server.queries)
    assert clean.code in (0, 4), clean.err
    at = rng.randint(1, queries)
    with simulate(world, Faults(lose_connection_at=at)) as sim:
        lost = sim.cli(*analyse_args(world, label))
    assert lost.code == 6
    assert f"prod_{world.network}" in lost.err and "the run can be repeated" in lost.err
    with simulate(world, Faults(time_out_at=at)) as sim:
        slow = sim.cli(*analyse_args(world, label))
    assert slow.code == 6 and "statement_timeout" in slow.err
    with simulate(world, Faults(fail_other_at=at)) as sim, pytest.raises(psycopg2.Error):
        sim.cli(*analyse_args(world, label))


def test_missing_data_is_loud(seed: int) -> None:
    """Rows missing from a table the pipeline relies on raise by name or set an exit
    code; they never turn into a smaller, clean-looking result."""
    world = generate(seed)
    rng = random.Random(seed)
    label = some_label(world, rng)
    live = [a for a in world.auctions if not a.fast_path]
    if not live:
        return
    target = rng.choice(live)
    order = rng.choice([t.uid for s in target.solutions for t in s.trades])
    with simulate(world, Faults(drop_orders=frozenset({order}))) as sim, pytest.raises(
        extract.MissingOrderError
    ):
        sim.cli(*analyse_args(world, label))
    with simulate(world, Faults(drop_contexts=frozenset({target.auction_id}))) as sim, \
            pytest.raises(extract.MissingAuctionContextError):
        sim.cli(*analyse_args(world, label))
    with simulate(world, Faults(unpriced=frozenset({target.auction_id}))) as sim, \
            pytest.raises(extract.MissingPricesError, match=str(target.auction_id)):
        sim.cli(*analyse_args(world, label))
    # Settlements: only the inherited rule needs them, and it needs them everywhere.
    if target.winners:
        faults = Faults(drop_settlements=frozenset({target.auction_id}))
        with simulate(world, faults) as sim:
            assert sim.cli(*analyse_args(world, label)).code == 5
            assert sim.cli(*analyse_args(world, label), "--outcome-rule",
                           "assume-settled").code in (0, 4)
    # A buy token's price gone: that bid cannot be valued, the auction is abandoned and
    # counted, and the run says so with its exit code.
    bid = rng.choice(target.solutions)
    token = rng.choice(bid.trades).buy_token
    faults = Faults(drop_prices=frozenset({(target.auction_id, token)}))
    with simulate(world, faults) as sim:
        conn = sim.conn()
        bundle = next(b for b in extract.load_auctions(conn, [target.auction_id]))
        from loo.counterfactual import price_suspects

        suspect = bool(price_suspects(bundle.bids, bundle.native_prices))
        result = sim.cli(*analyse_args(world, label))
        assert result.code == (4 if result.code == 4 else (0 if suspect else 2)), result.err
        checked = sim.cli("validate", "--network", world.network, "--start", world.start,
                          "--end", world.end, "--max-winners", str(world.max_winners))
        assert checked.code == 2
        assert "valuation-failure" in checked.out


def test_uncovered_window(seed: int, tmp_path: Path) -> None:
    """Block times or the model of auctions with a bid that stop before the window's end
    are exit 5 from every command, naming the table and where it stops: past them the
    window query finds nothing, so the run would silently come out shorter. `analyse`
    needs the same up to the end of each paid accounting period the window touches, whose
    full-week totals share out its rewards. A model whose last auction falls before an
    end, with staging holding no later bid before it (a quiet chain), is complete and runs
    as if nothing were cut. A refused run keeps nothing, so the same window reruns whole
    once the tables catch up."""
    world = generate(seed)
    rng = random.Random(seed * 7_907 + 41)  # its own stream: the world stays the seed's
    label = some_label(world, random.Random(seed))
    start, end = datetime.fromisoformat(world.start), datetime.fromisoformat(world.end)
    periods = payouts.accounting_periods(world.start, world.end)
    paid = [finish for begin, finish in periods
            if world.consistency_mart.get(payouts.period_label(begin, finish))
            or world.quote_mart.get(payouts.period_label(begin, finish))]
    times = auction_times(world)
    blocks_end = rng.choice((None, start + (end - start) * rng.random(),
                             end + (periods[-1][1] - end) * rng.random()))
    model_end = rng.choice((None, rng.choice(sorted(times))))
    later = sorted(a for a in times if model_end is not None and a > model_end)

    def short(ends: list[datetime]) -> tuple[str, datetime] | None:
        """The table that stops before the first of `ends` it does not reach, and where."""
        for at in ends:
            if blocks_end is not None and blocks_end < at:
                return "stg_rpc_data__block_timestamp", blocks_end
            if model_end is not None and later and times[later[0]] < at:
                return "pre_stg__orders_per_auction_with_at_least_one_bid", times[model_end]
        return None

    window = ("--network", world.network, "--start", world.start, "--end", world.end)
    commands = {
        "analyse": [*analyse_args(world, label), "--out", str(tmp_path / "{rule}.json")],
        "all-solvers": ["analyse", *window, "--all-solvers", "--limit", "2",
                        "--max-winners", str(world.max_winners)],
        "validate": ["validate", *window, "--max-winners", str(world.max_winners)],
        "validate-rewards": ["validate-rewards", *window],
    }
    expected = {name: short([end, *paid] if name in ("analyse", "all-solvers") else [end])
                for name in commands}

    def outcomes(sim: Sim) -> dict[str, tuple[Run, str]]:
        """Each command's run, and the report it wrote."""
        seen: dict[str, tuple[Run, str]] = {}
        for name, argv in commands.items():
            run = sim.cli(*argv)
            report = tmp_path / "inherited.json"
            seen[name] = (run, report.read_text() if report.exists() else "")
            report.unlink(missing_ok=True)
        return seen

    def same(a: tuple[Run, str], b: tuple[Run, str]) -> bool:
        return (a[0].code, a[0].out, a[1]) == (b[0].code, b[0].out, b[1])

    with simulate(world) as sim:
        clean = outcomes(sim)
    with simulate(world, Faults(blocks_end=blocks_end, model_end=model_end)) as sim:
        cut = outcomes(sim)
        for name, refusal in expected.items():
            run, report = cut[name]
            if refusal is None:
                assert same(cut[name], clean[name]), (name, run.err)
                continue
            table, stop = refusal
            assert run.code == 5, (name, run.err)
            assert table in run.err and world.network in run.err, run.err
            assert f"{stop:%Y-%m-%d %H:%M:%S} UTC" in run.err, run.err
            assert not run.out and not report, name
        sim.server.faults = Faults()  # the tables catch up
        caught_up = outcomes(sim)
        assert all(same(caught_up[name], clean[name]) for name in commands)


def test_reward_gate_faults(seed: int, tmp_path: Path) -> None:
    """The rewards gate: a winner without a reference score, or an auction the mart has
    not reached, is exit 3 — data, not formula."""
    world = generate(seed)
    with_winners = [a for a in world.auctions if a.winners and a.references]
    if not with_winners:
        return
    target = random.Random(seed).choice(with_winners)
    target.references = {}
    with simulate(world) as sim:
        assert sim.cli("validate-rewards", "--network", world.network, "--start",
                       world.start, "--end", world.end).code == 3
    world = generate(seed)
    target = next(a for a in world.auctions if a.auction_id == target.auction_id)
    target.mart = {}
    world.mart_off = set()
    with simulate(world) as sim:
        result = sim.cli("validate-rewards", "--network", world.network, "--start",
                         world.start, "--end", world.end, "--out", str(tmp_path / "r.out"))
    assert result.code == 3
    assert json.loads((tmp_path / "r.out").read_text())["auctions_missing_from_fct"] == [
        target.auction_id]


def test_clickhouse_faults(seed: int) -> None:
    """A refused query and a response cut off mid-stream both surface as
    `ClickHouseError` carrying the server's own message."""
    world = generate(seed)
    if world.chain_id is None:
        return
    label = some_label(world, random.Random(seed))
    house = SimClickHouse(world)
    with simulate(world, clickhouse=house) as sim:
        sim.cli(*analyse_args(world, label), "--quotes")
        sent = len(house.queries)
    if not sent:
        return
    at = random.Random(seed).randint(1, sent)
    for broken, message in ((SimClickHouse(world, refuse_at=at), "Syntax error"),
                            (SimClickHouse(world, truncate_at=at), "Code: 241")):
        with simulate(world, clickhouse=broken) as sim, pytest.raises(
            ch.ClickHouseError, match=message
        ):
            sim.cli(*analyse_args(world, label), "--quotes")


def test_arguments(seed: int, tmp_path: Path) -> None:
    """Resolution and argument errors: exact, case-insensitive names; rotated addresses
    removed together; a catalogued solver that never bid, the catalogue's own bucket
    name, an unknown name and a duplicate scenario all refused; placeholders required."""
    world = generate(seed)
    rng = random.Random(seed)
    names = sorted({s.name for s in world.solvers if s.bids and s.name != "Uncatalogued"
                    and any(a in world.bidding_addresses() for a in s.addresses)})
    with simulate(world) as sim:
        for refused in ("Nobody", "Dormant", "Uncatalogued"):
            assert sim.cli(*analyse_args(world, refused)).code == 4
        if names:
            name = rng.choice(names)
            out = tmp_path / "{solver}.json"
            result = sim.cli(*analyse_args(world, name.lower()), "--out", str(out))
            assert result.code in (0, 4), result.err
            written = next(tmp_path.glob("*.json"))
            payload = json.loads(written.read_text())
            assert payload["addresses"] == sorted(expect.label_addresses(world, name))
            assert sim.cli(*analyse_args(world, name, name.upper())).code == 4
            assert sim.cli(*analyse_args(world, f"{name},{name.swapcase()}")).code == 4
            assert sim.cli(*analyse_args(world, name, name), "--out", str(out)).code == 4
            assert sim.cli(*analyse_args(world, name), "--outcome-rule", "inherited",
                           "--outcome-rule", "assume-settled", "--out", str(out)).code == 4
        assert sim.cli(*analyse_args(world, " , ")).code == 4
        assert sim.cli(*analyse_args(world)).code == 4
        assert sim.cli(*analyse_args(world, "Nobody"), "--all-solvers").code == 4
        labels = {world.solver_of(a).label for a in world.bidding_addresses()}
        if len(labels) > 1:
            assert sim.cli(*analyse_args(world), "--all-solvers", "--out",
                           str(tmp_path / "fixed.json")).code == 4
        early = sim.cli("analyse", "--network", world.network, "--start", "2026-10-20",
                        "--end", world.end, "--solver", names[0] if names else "Nobody")
        assert early.code == 4
        assert sim.cli("validate", "--network", world.network, "--start", "2026-10-20",
                       "--end", world.end).code == 4
        empty = ("--network", world.network, "--start", "2027-01-01", "--end", "2027-01-02")
        assert sim.cli("validate", *empty).code == 1
        assert sim.cli("validate-rewards", *empty).code == 1
        if names:
            assert sim.cli("analyse", *empty, "--solver", names[0]).code == 4


def test_all_solvers(seed: int, tmp_path: Path) -> None:
    """`--all-solvers` derives one scenario per competitor from the window — names for
    catalogued solvers, addresses for the rest — and reports, rather than fails on, a
    competitor the analysed auctions never saw."""
    world = generate(seed)
    labels = sorted({world.solver_of(a).label for a in world.bidding_addresses()})
    with simulate(world) as sim:
        result = sim.cli("analyse", "--network", world.network, "--start", world.start,
                         "--end", world.end, "--all-solvers", "--max-winners",
                         str(world.max_winners), "--limit", "2",
                         "--out", str(tmp_path / "{solver}.json"))
    assert result.code in (0, 2), result.err
    written = sorted(json.loads(p.read_text())["solver"] for p in tmp_path.glob("*.json"))
    assert written == sorted(labels)
    for path in tmp_path.glob("*.json"):
        payload = json.loads(path.read_text())
        assert payload["auctions"] <= 2


def test_networks_without_quote_data(seed: int) -> None:
    """A network with no quote data refuses `--quotes` by name rather than reporting an
    empty quote side; its solve side still runs."""
    knobs = replace(sample_knobs(random.Random(seed)), network="sepolia")
    world = generate(seed, knobs)
    label = some_label(world, random.Random(seed))
    with simulate(world) as sim:
        refused = sim.cli(*analyse_args(world, label), "--quotes")
        assert refused.code == 4 and "sepolia" in refused.err
        assert sim.cli(*analyse_args(world, label)).code in (0, 4)


def test_modes(seed: int, tmp_path: Path) -> None:
    """Surplus mode ranks by user surplus with complete information, so the counterfactual
    equals the oracle's everywhere and carries no rewards; keeping price suspects puts
    them back into every number."""
    world = generate(seed)
    label = some_label(world, random.Random(seed))
    with simulate(world) as sim:
        result = sim.cli(*analyse_args(world, label), "--mode", "surplus",
                         "--include-price-suspects", "--out", str(tmp_path / "s.json"))
    assert result.code in (0, 4), result.err
    payload = json.loads((tmp_path / "s.json").read_text())
    assert payload["mode"] == "surplus" and payload["cow"] is None
    assert payload["rewards_base"] == payload["rewards_loo"] == 0
    addresses = expect.label_addresses(world, label)
    total = 0
    with_solver = 0
    for auction in world.auctions:
        if auction.fast_path or not any(s.solver in addresses for s in auction.solutions):
            continue
        with_solver += 1
        truth = expect.counterfactual(world, auction, addresses, rule="inherited",
                                      mode="surplus")
        total += truth.delta_surplus
    assert payload["auctions_with_solver"] == with_solver
    assert payload["delta_surplus"] == total
    # Beside the score-mode run of the same scenario, the two are different windows and
    # the table's columns say which is which; a kept suspect is warned about.
    with simulate(world) as sim:
        sim.cli(*analyse_args(world, label), "--out", str(tmp_path / "t.json"))
    reports = [aggregate.load_report(str(tmp_path / n)) for n in ("s.json", "t.json")]
    assert reports[0].delta_rewards is None and reports[0].delta_rewards_capped is None
    table = aggregate.comparison(aggregate.group_reports(reports))
    assert table.columns == tuple(
        f"{r.solver} {r.start}..{r.end} ({r.network}, {r.mode})" for r in reports)
    cells = dict(table.rows)
    assert cells["Δrewards uncapped"][0] == "not computed (surplus mode)"
    assert cells["Δrewards capped (estimate)"][0] == "not computed"
    assert cells["net change (Δsurplus − capped Δrewards)"][0] == "n/a without a capped estimate"
    assert cells["  filter verdicts resting on surplus, in those auctions"][0] == (
        "n/a (surplus mode)")
    assert cells["  assume-settled"] == ("not run", "not run")
    kept = bool(payload["price_suspect_auctions"])
    assert any("--include-price-suspects" in w for w in table.warnings) == kept
    assert reports[0].analysed == reports[0].auctions
    with pytest.raises(ValueError, match="two reports"):
        aggregate.group_reports([reports[1], reports[1]])
    alt = aggregate.load_report(str(tmp_path / "t.json"))
    with pytest.raises(ValueError, match="no 'inherited' report"):
        aggregate.group_reports([replace(alt, outcome_rule="assume-settled")])


__all__ = ["Knobs"]
