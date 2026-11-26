"""The simulation: one seeded world per test, the real pipeline over it, invariants and
the differential oracle on what comes back. See `tests/sim/__init__.py`."""

from __future__ import annotations

import json
import random
from decimal import Decimal
from pathlib import Path

from sim import aggregation, autopilot_checks, cli_checks, expect, oracle, quote_checks, runner_up
from sim.harness import Sim, simulate
from sim.oracle import Bid
from sim.world import World, generate

from loo import aggregate, extract, run, validate
from loo.counterfactual import (
    UNMOVED_SHARED,
    Analysis,
    AuctionCounterfactual,
    PreparedAuction,
    analyse_auction,
    leave_one_out,
)
from loo.extract import AuctionBundle
from loo.valuation import build_solutions
from loo.winner_selection import (
    Ranking,
    Solution,
    arbitrate,
    compute_reference_outcomes,
    filter_guess_could_matter,
)


def loaded(sim: Sim) -> tuple[list[AuctionBundle], list[int], list[int]]:
    conn = sim.conn()
    ids = extract.auctions_in_window(conn, sim.world.start, sim.world.end)
    fast: list[int] = []
    bundles = list(extract.load_auctions(conn, ids, fast_path=fast, chunk_size=3))
    return bundles, ids, fast


def as_bids(solutions: list[Solution]) -> list[Bid]:
    """`loo`'s solutions handed to the oracle: the filter compares a pair's score where
    it is known and its surplus where it is not."""
    return [
        Bid(s.solution_uid, s.solver, s.total,
            {p: s.pair_scores.get(p, v) for p, v in s.pair_values.items()},
            s.winner_pairs)
        for s in solutions
    ]


def check_against_oracle(ranking: Ranking, solutions: list[Solution], max_winners: int) -> None:
    """Differential: `loo.winner_selection` against the naive transcription."""
    truth = oracle.arbitrate(as_bids(solutions), max_winners)
    assert [s.solution_uid for s in ranking.ranked] == [b.uid for b in truth.ranked]
    assert [s.solution_uid for s in ranking.filtered_out] == [b.uid for b in truth.filtered]
    assert ranking.winner_uids == truth.winners
    assert ranking.baselines == truth.baselines
    ours = compute_reference_outcomes(ranking, max_winners)
    theirs = oracle.references(truth.ranked, truth.winners, max_winners)
    picks = oracle.reference_picks(truth.ranked, truth.winners, max_winners)
    assert {k: (v.score, v.setter_uids) for k, v in ours.items()} == {
        k: (score, picks[k]) for k, (score, _) in theirs.items()}
    # Proxied: a multi-pair solution with a pair that has no score and whose surplus
    # does not clear its baseline.
    proxied = {
        s.solution_uid
        for s in solutions
        if len(s.pair_values) > 1
        and any(p not in s.pair_scores and v < truth.baselines.get(p, 0)
                for p, v in s.pair_values.items())
    }
    assert ranking.proxied_uids == proxied


def check_pick_invariants(ranking: Ranking, max_winners: int) -> None:
    winners = ranking.winners
    assert len(winners) <= max_winners
    claimed: set[tuple[str, str]] = set()
    for w in winners:
        assert not (w.winner_pairs & claimed), "winners share a directed pair"
        claimed |= w.winner_pairs
    position = {s.solution_uid: i for i, s in enumerate(ranking.ranked)}
    by_total = sorted(ranking.ranked, key=lambda s: (-s.total, position[s.solution_uid]))
    for s in ranking.ranked:
        if s.solution_uid in ranking.winner_uids:
            continue
        earlier = [w for w in by_total[: by_total.index(s)] if w.solution_uid in ranking.winner_uids]
        assert len(earlier) >= max_winners or any(w.winner_pairs & s.winner_pairs for w in earlier)


def check_bundle_matches_world(world: World, bundle: AuctionBundle) -> None:
    auction = next(a for a in world.auctions if a.auction_id == bundle.auction_id)
    assert [b.uid for b in bundle.bids] == sorted(s.uid for s in auction.solutions)
    assert bundle.block_deadline == auction.block
    assert bundle.reference_scores == auction.references
    covered = expect.covered(auction)
    assert bundle.fee_context.known == frozenset(covered)
    for bid in bundle.bids:
        solution = next(s for s in auction.solutions if s.uid == bid.uid)
        assert (bid.solver, bid.score, bid.is_winner, bid.filtered_out) == (
            solution.solver, solution.score, solution.is_winner, solution.filtered_out)
        assert sorted(o.uid for o in bid.orders) == sorted(t.uid for t in solution.trades)
        for order in bid.orders:
            trade = next(t for t in solution.trades if t.uid == order.uid)
            assert (order.executed_sell, order.executed_buy, order.sell_amount,
                    order.buy_amount, order.side, order.partially_fillable) == (
                trade.executed_sell, trade.executed_buy, trade.sell_amount,
                trade.buy_amount, trade.kind, trade.partially_fillable)
            assert bundle.native_prices[order.buy_token] == auction.prices[order.buy_token]
            assert bundle.native_prices[order.sell_token] == auction.prices[order.sell_token]
    for uid in covered:
        policies = bundle.fee_context.policies.get(uid, ())
        assert len(policies) == len(world.orders[uid].policies)
        for got, want in zip(policies, world.orders[uid].policies, strict=True):
            assert got.kind == want.kind
        assert bundle.fee_context.partner_counts.get(uid, 0) == world.orders[uid].partners


def check_valuation(world: World, bundle: AuctionBundle) -> None:
    """Per order: surplus and fee against the oracle, and the decomposition is exactly
    additive against the recorded score."""
    auction = next(a for a in world.auctions if a.auction_id == bundle.auction_id)
    valued, failures = build_solutions(bundle.bids, bundle.native_prices, world.weth,
                                       fee_context=bundle.fee_context)
    assert failures == {}
    covered = expect.covered(auction)
    for entry in valued:
        solution = next(s for s in auction.solutions if s.uid == entry.bid.uid)
        v = entry.valuation
        assert not entry.solution.score_anchor_failed
        assert entry.solution.total == solution.score
        for trade in solution.trades:
            truth = solution.order_scores[trade.uid]
            assert v.order_surplus_native[trade.uid] == truth.surplus_native
            assert v.order_volume_native[trade.uid] == (
                trade.executed_buy * auction.prices[trade.buy_token] // oracle.ONE)
            known = trade.uid in covered and truth.partner_native is not None
            assert (trade.uid not in v.fees_unknown) == known
            if known:
                assert v.order_fee_native[trade.uid] == truth.fee_native
                assert v.order_surplus_native[trade.uid] + v.order_fee_native[trade.uid] == (
                    truth.score_native)
                assert v.order_partner_fee_native[trade.uid] == truth.partner_native
                assert 0 <= v.order_partner_fee_native[trade.uid] <= v.order_fee_native[trade.uid]
        for pair, score in entry.solution.pair_scores.items():
            assert score == solution.pair_scores[pair], "a known pair score must be exact"
        if not v.pairs_fees_unknown or len(v.pairs_fees_unknown) == 1:
            assert sum(entry.solution.pair_scores.values()) == solution.score
        assert entry.solution.winner_pairs == solution.claims
        assert v.total == sum(t.surplus_native for t in solution.order_scores.values())
        assert v.fees_complete == all(
            t.uid in covered and solution.order_scores[t.uid].partner_native is not None
            for t in solution.trades)
        assert v.fee_native == sum(v.order_fee_native.values())
        assert v.partner_fee_native == sum(v.order_partner_fee_native.values())


def check_record_reproduced(world: World, bundle: AuctionBundle) -> Ranking:
    """The recorded competition, replayed: where no filter verdict was a guess, our
    arbitration is the record; where it was, `validate` must name the cause."""
    auction = next(a for a in world.auctions if a.auction_id == bundle.auction_id)
    valued, _ = build_solutions(bundle.bids, bundle.native_prices, world.weth,
                                fee_context=bundle.fee_context)
    solutions = [v.solution for v in valued]
    ranking = arbitrate(solutions, world.max_winners)
    check_against_oracle(ranking, solutions, world.max_winners)
    check_pick_invariants(ranking, world.max_winners)
    # Removing nobody is the baseline, exactly.
    nobody = leave_one_out(solutions, frozenset(), world.max_winners)
    assert nobody.winner_uids == ranking.winner_uids
    assert [s.solution_uid for s in nobody.ranked] == [s.solution_uid for s in ranking.ranked]
    report = validate.check_auction(bundle, world.weth, max_winners=world.max_winners)
    assert report.unexplained is None, report.unexplained
    assert report.pick_matches_observed and report.reference_matches_observed
    if not ranking.proxied_uids:
        assert ranking.winner_uids == {s.uid for s in auction.winners}
        assert {s.solution_uid for s in ranking.filtered_out} == {
            s.uid for s in auction.solutions if s.filtered_out}
        assert report.winners_match and report.filter_matches
        assert report.reference_matches_recomputed
        assert report.disagreement == "none"
    # Ties: shuffling the bids among *distinct* totals cannot move anything.
    totals = [s.total for s in solutions]
    if len(set(totals)) == len(totals):
        shuffled = list(solutions)
        random.Random(bundle.auction_id).shuffle(shuffled)
        again = arbitrate(shuffled, world.max_winners)
        assert again.winner_uids == ranking.winner_uids
        assert [s.solution_uid for s in again.ranked] == [s.solution_uid for s in ranking.ranked]
    return ranking


def check_counterfactual(
    world: World, bundle: AuctionBundle, removed: frozenset[str], rule: str,
    shared: dict[int, PreparedAuction],
) -> AuctionCounterfactual:
    auction = next(a for a in world.auctions if a.auction_id == bundle.auction_id)
    settled = {u: extract.Settlement(s.landed, s.in_time) for u, s in auction.settlements.items()}
    caps = {
        u: extract.SolutionCap(upper=c.upper, lower=auction.flat_lower,
                               excluded=auction.excluded, penalty=c.penalty)
        for u, c in auction.caps.items()
    }
    result = analyse_auction(bundle, world.weth, removed, max_winners=world.max_winners,
                             outcome_rule=rule,  # type: ignore[arg-type]
                             settled=settled, solution_caps=caps)
    # `analyse_window` shares one preparation across every scenario and rule of an
    # auction, and copies one unmoved result, with what it derived, into the next.
    if bundle.auction_id not in shared:
        shared[bundle.auction_id] = PreparedAuction(
            bundle, world.weth, max_winners=world.max_winners, settled=settled,
            solution_caps=caps)
    again = shared[bundle.auction_id].analyse(removed, outcome_rule=rule)  # type: ignore[arg-type]
    assert again == result
    for name in UNMOVED_SHARED:
        assert getattr(again, name) == getattr(result, name), name
    present = any(s.solver in removed for s in auction.solutions)
    assert result.solver_present == present
    assert result.n_solutions == len(auction.solutions)
    assert result.solver_won_db == any(s.solver in removed for s in auction.winners)
    volume = expect.executed_volume(world, auction, rule)
    assert (result.executed_volume_base, result.executed_orders_base,
            result.executed_orders_all_base) == volume
    assert result.price_suspect_uids == expect.suspect_uids(auction)
    if not present:
        assert result.order_diffs == () and not result.anything_moved
        return result
    truth = expect.counterfactual(world, auction, removed, rule=rule, known=expect.covered(auction))
    valued, _ = build_solutions(bundle.bids, bundle.native_prices, world.weth,
                                fee_context=bundle.fee_context)
    solutions = [v.solution for v in valued]
    runner_up.check(result, auction, as_bids(solutions), removed, world.max_winners)
    ours_base = arbitrate(solutions, world.max_winners)
    ours_loo = arbitrate([s for s in solutions if s.solver not in removed], world.max_winners)
    # Monotone: nobody kept with the removed solvers present is filtered without them.
    kept_base = {s.solution_uid for s in ours_base.ranked}
    kept_loo = {s.solution_uid for s in ours_loo.ranked}
    survivors = {s.solution_uid for s in solutions if s.solver not in removed}
    assert kept_base & survivors <= kept_loo
    assert result.un_filtered_uids == kept_loo - kept_base
    assert result.baseline_winner_uids == ours_base.winner_uids
    assert result.loo_winner_uids == ours_loo.winner_uids
    assert result.baseline_matches_db == (ours_base.winner_uids == {s.uid for s in auction.winners})
    assert result.solver_won_baseline == any(s.solver in removed for s in ours_base.winners)
    assert result.multi_pair_filter_proxied == len(ours_base.proxied_uids)
    assert result.multi_pair_solutions == sum(
        1 for s in ours_base.ranked + ours_base.filtered_out if len(s.pair_values) > 1)
    # Who is a replacement, and what the slot rule gave it.
    recorded = {s.uid for s in auction.winners}
    claims = {s.uid: s.claims for s in auction.solutions}
    inherit = {p: auction.settlements[u].in_time for u in recorded for p in claims[u]}
    for side, winners, reps, orphans in (
        ("base", ours_base.winner_uids, result.replacements_base, result.orphans_base),
        ("loo", ours_loo.winner_uids, result.replacements_loo, result.orphans_loo),
    ):
        assert reps == winners - recorded, side
        held = {u: [inherit[p] for p in claims[u] if p in inherit] for u in reps}
        if rule == "inherited":
            assert orphans == {u for u in reps if not held[u]}
            if side == "loo":
                assert result.inherited_reverts_loo == {
                    u for u in reps if held[u] and not all(held[u])}
        else:
            assert not orphans and not result.inherited_reverts_loo
    for d in result.order_diffs:
        assert d.late_base == (d.observed_base and rule == "inherited" and any(
            auction.settlements[u].landed and not auction.settlements[u].in_time
            for u in ours_base.winner_uids & recorded
            if any(t.uid == d.order_uid for t in next(
                s for s in auction.solutions if s.uid == u).trades)))
    assert result.filter_guess_could_matter == (
        filter_guess_could_matter(ours_base, world.max_winners)
        or filter_guess_could_matter(ours_loo, world.max_winners))
    # Caps: a replacement takes the summed caps of the recorded slots it displaced.
    holders = {p: u for u in recorded for p in claims[u]}
    taken: dict[int, int] = {}
    for u in ours_base.winner_uids - recorded:
        for slot in {holders[p] for p in claims[u] if p in holders}:
            taken[slot] = taken.get(slot, 0) + 1
    double = sum(1 for n in taken.values() if n > 1)
    taken = {}
    orphans_loo: set[int] = set()
    for u in ours_loo.winner_uids - recorded:
        slots = {holders[p] for p in claims[u] if p in holders}
        if not slots:
            orphans_loo.add(u)
        for slot in slots:
            taken[slot] = taken.get(slot, 0) + 1
    double += sum(1 for n in taken.values() if n > 1)
    assert result.cap_double_inherited == double
    assert result.cap_orphans_loo == orphans_loo
    if not result.filter_guess_could_matter:
        assert ours_base.winner_uids == truth.base.winners
        assert ours_loo.winner_uids == truth.loo.winners
    exact = kept_base == truth.kept_base and kept_loo == truth.kept_loo
    if exact:
        assert result.baseline_reference_scores == truth.base.references
        assert result.loo_reference_scores == truth.loo.references
        assert result.surplus_base == truth.surplus_base
        assert result.surplus_loo == truth.surplus_loo
        assert result.delta_surplus == truth.delta_surplus
        assert sum(d.only_with_solver for d in result.order_diffs) == truth.only_with
        assert sum(d.only_without_solver for d in result.order_diffs) == truth.only_without
        assert len(result.order_diffs) == truth.orders_compared
        complete = truth.fees_base is not None and truth.fees_loo is not None
        assert result.fees_complete == complete
        if complete:
            assert truth.fees_base is not None and truth.fees_loo is not None
            assert truth.partner_base is not None and truth.partner_loo is not None
            assert result.delta_protocol_and_partner_fees == truth.fees_loo - truth.fees_base
            assert result.delta_partner_fees == truth.partner_loo - truth.partner_base
            assert result.delta_protocol_fees == (
                truth.fees_loo - truth.partner_loo - truth.fees_base + truth.partner_base)
            assert result.protocol_fees_base == truth.fees_base - truth.partner_base
            assert result.protocol_and_partner_fees_base == truth.fees_base
            assert result.protocol_and_partner_fees_loo == truth.fees_loo
            assert result.partner_fees_base == truth.partner_base
            assert result.partner_fees_loo == truth.partner_loo
        assert {k: v.uncapped_reward for k, v in result.baseline_rewards.items()} == {
            k: v.uncapped for k, v in truth.base.rewards.items()}
        assert {k: v.uncapped_reward for k, v in result.loo_rewards.items()} == {
            k: v.uncapped for k, v in truth.loo.rewards.items()}
        for side, rows in ((result.baseline_rewards, truth.base.rewards),
                           (result.loo_rewards, truth.loo.rewards)):
            for solver, row in rows.items():
                got = side[solver]
                assert (got.upper_cap, got.lower_cap, got.capped_reward) == (
                    row.upper, row.lower, row.capped)
                assert (got.competition_score, got.observed_score, got.reference_score) == (
                    row.competition, row.observed, row.reference)
        assert result.rewards_base_capped == truth.capped(truth.base)
        assert result.rewards_loo_capped == truth.capped(truth.loo)
        budgets: list[Decimal | None] = []
        for side in (truth.base, truth.loo):
            rows = list(side.rewards.values())
            budgets.append(
                None if any(r.upper is None or r.capped is None for r in rows)
                else sum((r.upper - r.capped for r in rows
                          if r.upper is not None and r.capped is not None), Decimal(0)))
        assert (result.consistency_budget_base, result.consistency_budget_loo) == tuple(budgets)
        runner_up.check_above_cap(result, truth.base.rewards, truth.loo.rewards,
                                  auction.excluded)
    # The consistency budget is what the caps leave: with the capped rewards it sums to the
    # upper caps, auction by auction, and exists exactly where the capped delta does.
    if result.delta_rewards_capped is None:
        assert result.delta_consistency_budget is None
    else:
        budget = result.delta_consistency_budget
        assert budget is not None
        uppers = [sum((r.upper_cap for r in side.values() if r.upper_cap is not None),
                      Decimal(0))
                  for side in (result.baseline_rewards, result.loo_rewards)]
        assert result.delta_rewards_capped + budget == uppers[1] - uppers[0]
    # Rewards stay inside their caps.
    for reward in [*result.baseline_rewards.values(), *result.loo_rewards.values()]:
        if reward.capped_reward is None:
            continue
        if auction.excluded:
            assert reward.capped_reward == 0
        else:
            assert reward.lower_cap is not None and reward.upper_cap is not None
            low, high = reward.lower_cap, reward.upper_cap
            assert reward.capped_reward == min(max(Decimal(reward.uncapped_reward), low), high)
    return result


def check_rules_agree(
    world: World, inherited: list[AuctionCounterfactual], settled: list[AuctionCounterfactual]
) -> None:
    """Where every recorded winner settled in time the two outcome rules cannot differ: a
    replacement inherits nothing but successes, and nobody reverts."""
    for a, b in zip(inherited, settled, strict=True):
        auction = next(x for x in world.auctions if x.auction_id == a.auction_id)
        if not all(s.in_time for s in auction.settlements.values()):
            continue
        assert (a.delta_surplus, a.delta_rewards, a.delta_rewards_capped) == (
            b.delta_surplus, b.delta_rewards, b.delta_rewards_capped)
        assert a.delta_protocol_fees == b.delta_protocol_fees
        assert not a.inherited_reverts_loo


def scenarios(world: World, rng: random.Random) -> list[str]:
    """`--solver` arguments: a name, a joint group, a raw address — each resolvable."""
    labels = sorted({world.solver_of(a).label for a in world.bidding_addresses()})
    chosen = [rng.choice(labels)]
    if len(labels) >= 2:
        group = rng.sample(labels, 2)
        if set(group) != set(chosen):
            chosen.append(",".join(group))
    address = rng.choice(sorted(world.bidding_addresses()))
    spelled = rng.choice((address, "0x" + address, "0X" + address.upper()))
    if world.solver_of(address).name == "Uncatalogued" and frozenset({address}) not in [
        expect.label_addresses(world, c) for c in chosen
    ]:
        chosen.append(spelled)
    return chosen


def group_addresses(world: World, entry: str) -> frozenset[str]:
    out: frozenset[str] = frozenset()
    for name in entry.split(","):
        out |= expect.label_addresses(world, name.strip())
    return out


def check_report_is_direct(
    world: World, tmp_path: Path, ids: list[int], payload: dict[str, object], direct: Analysis
) -> None:
    """The CLI shares each auction's valuation, arbitration and unmoved results across its
    scenarios; the report must be what one fresh `analyse_auction` per auction gives,
    in every analysis field and every changed auction."""
    solver_run = run.SolverRun(solver=direct.solver, addresses=direct.addresses, matches=[],
                               members=(), analysis=direct)
    window = run.WindowAnalysis(world.network, world.start, world.end, ids, [solver_run])
    path = tmp_path / "direct" / "report.json"
    path.parent.mkdir(exist_ok=True)
    run.write_report(str(path), window, solver_run, None)
    fresh = json.loads(path.read_text())
    for key in fresh.keys() - {"cow", "paid", "quote", "fast_path_auctions"}:
        assert payload[key] == fresh[key], key


def test_world(seed: int, tmp_path: Path) -> None:
    exercise(generate(seed), tmp_path, random.Random(seed ^ 0x5EED))


def exercise(
    world: World, tmp_path: Path, rng: random.Random, entries: list[str] | None = None
) -> dict[tuple[str, str], Analysis]:
    """Every layer over one world: extraction, valuation, the record, the counterfactual
    per scenario and rule, the CLI and its files, both gates, and the quote side."""
    with simulate(world) as sim:
        bundles, ids, fast = loaded(sim)
        assert ids == sorted({a.auction_id for a in world.auctions} | set(world.extra_window_ids))
        assert fast == [a.auction_id for a in world.auctions if a.fast_path]
        assert [b.auction_id for b in bundles] == [
            a.auction_id for a in world.auctions if not a.fast_path]
        for bundle in bundles:
            check_bundle_matches_world(world, bundle)
            check_valuation(world, bundle)
            check_record_reproduced(world, bundle)

        entries = entries or scenarios(world, rng)
        removals = {e: group_addresses(world, e) for e in entries}
        shared: dict[int, PreparedAuction] = {}
        direct: dict[tuple[str, str], Analysis] = {}
        for entry, addresses in removals.items():
            by_rule: dict[str, list[AuctionCounterfactual]] = {}
            for rule in ("inherited", "assume-settled"):
                results = [check_counterfactual(world, b, addresses, rule, shared)
                           for b in bundles]
                by_rule[rule] = results
                for exclude in (False, True):
                    analysis = Analysis(solver=entry, addresses=addresses,
                                        outcome_rule=rule,  # type: ignore[arg-type]
                                        capped_estimate=True, exclude_price_suspect=exclude)
                    for r in results:
                        analysis.add(r)
                    aggregation.compare(analysis, aggregation.expected(
                        results, addresses, exclude_suspects=exclude, capped_estimate=True))
                    direct[(entry, rule)] = analysis
            check_rules_agree(world, by_rule["inherited"], by_rule["assume-settled"])

        args = ["analyse", *sim.window_args(), "--max-winners", str(world.max_winners),
                "--outcome-rule", "inherited", "--outcome-rule", "assume-settled",
                "--out", str(tmp_path / "{solver}.{rule}.json")]
        for entry in entries:
            args += ["--solver", entry]
        result = sim.cli(*args)
        assert result.code in (0, 4), result.err
        assert f"{len(ids)} auctions in [{world.start}, {world.end}) on {world.network}" in (
            result.err)
        assert "settlement outcomes for" in result.err and "reward caps for" in result.err
        reports = sorted(tmp_path.glob("*.json"))
        assert len(reports) == 2 * len(entries)
        for path in reports:
            report = aggregate.load_report(str(path))
            payload = json.loads(path.read_text())
            entry = report.solver
            analysis = direct[(entry, report.outcome_rule)]
            assert payload["addresses"] == sorted(removals[entry])
            assert report.delta_surplus == analysis.delta_surplus
            assert report.delta_rewards == analysis.delta_rewards
            assert report.delta_rewards_capped == analysis.delta_rewards_capped
            assert report.delta_protocol_fees == analysis.delta_protocol_fees
            assert report.auctions == analysis.auctions == len(bundles)
            assert report.fast_path == len(fast)
            assert report.orders_only_with_solver == analysis.orders_only_with_solver
            assert report.window_volume == analysis.window_volume_base
            assert payload["price_suspect_auctions"] == analysis.price_suspect_auctions
            assert payload["auctions_with_solver"] == analysis.auctions_with_solver
            check_report_is_direct(world, tmp_path, ids, payload, analysis)
        cli_checks.check_reports_and_compare(sim, world, tmp_path, entries, direct, result.code)
        cli_checks.check_validate(sim, world, tmp_path, bundles)
        cli_checks.check_validate_rewards(sim, world, tmp_path)
        if world.chain_id is None:
            return direct
        window = quote_checks.check_window(sim, world, ids)
        autopilot_checks.check_autopilot(sim, world, ids, removals)
        for entry, addresses in removals.items():
            analysis_q = quote_checks.check_analysis(world, window, entry, addresses)
            results = [
                quote_checks.check_fee_path(world, bundle, addresses,
                                            analysis_q.counterfactual_quotes)
                for bundle in bundles
            ]
            together = Analysis(solver=entry, addresses=addresses)
            for r in results:
                together.add(r)
            aggregation.compare(together, aggregation.expected(
                results, addresses, exclude_suspects=True, capped_estimate=False))
            if entry == entries[0]:
                quote_checks.check_cli(sim, world, tmp_path, entry, analysis_q, window)
    return direct
