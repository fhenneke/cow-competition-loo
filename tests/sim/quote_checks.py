"""The quote side, end to end: the id link against what the world made linkable, the
pick and the stream replay against the oracle, both price guards against a naive
recomputation, every leaf of every diff, and the replacement quotes' way into the fee
path."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from loo import aggregate, ch, extract, quotes
from loo.counterfactual import AuctionCounterfactual, analyse_auction
from loo.extract import AuctionBundle
from loo.fees import Quote
from loo.quotes import MatchedQuote, QuoteAnalysis, QuoteWindow
from loo.valuation import build_solutions

from . import expect, oracle, summary_checks
from .harness import Sim
from .oracle import Answer
from .world import NATIVE, World

THRESHOLD = 2.0


def in_window(world: World) -> list[str]:
    from datetime import datetime

    start, end = datetime.fromisoformat(world.start), datetime.fromisoformat(world.end)
    return [u for u, o in world.orders.items() if o.quote is not None and start <= o.created < end]


def landed_trades(world: World) -> dict[str, list[tuple[int, int, int]]]:
    trades: dict[str, list[tuple[int, int, int]]] = {}
    for a in world.auctions:
        for s in a.winners:
            if a.settlements[s.uid].landed:
                for t in s.trades:
                    trades.setdefault(t.uid, []).append((a.auction_id, t.executed_sell,
                                                         t.executed_buy))
    return trades


def expected_suspects(world: World, auction_ids: list[int]) -> tuple[set[str], set[str]]:
    """(one-sided, two-sided), recomputed naively."""
    prices = {a.auction_id: a.prices for a in world.auctions}
    window = [a for a in world.auctions if a.auction_id in set(auction_ids)]
    trades = landed_trades(world)

    def priced(token: str) -> str:
        return world.weth if token == NATIVE else token

    one: set[str] = set()
    two: set[str] = set()
    for uid in in_window(world):
        order = world.orders[uid]
        assert order.quote is not None
        sell, buy = priced(order.sell_token), priced(order.buy_token)
        weighted = [(prices[a][sell], sold) for a, sold, _ in trades.get(uid, [])
                    if sell in prices[a] and sold > 0]
        if weighted:
            reference: float | None = (
                sum(p * q for p, q in weighted) / sum(q for _, q in weighted))
        else:
            seen = [a.prices[order.sell_token] for a in window if order.sell_token in a.prices]
            reference = oracle.window_median(seen) if seen else None
        api = order.quote.sell_token_price
        if reference and api > 0:
            ratio = (reference / 1e18) / api
            if ratio > THRESHOLD or ratio < 1 / THRESHOLD:
                one.add(uid)
        # Exact, 1e18-scaled: a side worth under one wei is still worth something.
        sold_value = bought_value = 0
        for a, sold, bought in trades.get(uid, []):
            if sell in prices[a] and buy in prices[a]:
                sold_value += prices[a][sell] * sold
                bought_value += prices[a][buy] * bought
        if sold_value and bought_value and (
            sold_value > THRESHOLD * bought_value or bought_value > THRESHOLD * sold_value
        ):
            two.add(uid)
    return one, two


def answers(match: MatchedQuote) -> list[Answer]:
    return [Answer(e.estimator, int(e.out_amount), int(e.gas), e.verified)
            for e in match.request.estimates]


def check_window(sim: Sim, world: World, auction_ids: list[int]) -> QuoteWindow:
    conn = sim.conn()
    window = quotes.load_quote_window(ch.Client.from_env(), conn, world.network, world.start,
                                      world.end, auction_ids=auction_ids)
    created = in_window(world)
    assert sorted(o.order_uid for o in window.orders) == sorted(created)
    assert window.orders_dropped == 0
    assert window.trivial_orders == sum(world.linkable.get(u) == "trivial" for u in created)
    hows = [world.linkable[u] for u in created if world.linkable[u] != "trivial"]
    linked = {"id", "stream", "requote"}
    assert window.link.orders == len(hows)
    assert window.link.linked == sum(h in linked for h in hows)
    assert window.link.requoted == hows.count("requote")
    assert window.link.streamed == hows.count("stream")
    expected_unlinked = {h: hows.count(h) for h in set(hows) - linked}
    assert dict(window.link.unlinked) == expected_unlinked
    placed = {m.order.order_uid: m for m in window.matches}
    assert set(placed) == {u for u in created if world.linkable[u] in linked}
    for uid, match in placed.items():
        order = world.orders[uid]
        assert order.quote is not None
        assert match.order.quote_solver == order.quote.solver
        assert (match.stream is not None) == (world.linkable[uid] == "stream")
        winner = oracle.quote_winner(answers(match), match.order.is_sell,
                                     match.request.in_amount, order.quote.gas_price,
                                     order.quote.sell_token_price)
        if match.stream is None:
            # The stored quote is the competition's winner, to the atom.
            assert winner is not None and winner.estimator == match.request.winner
            assert winner.out_amount == match.order.out_atoms
        else:
            sent = oracle.stream_forwards(answers(match), match.order.is_sell,
                                          match.request.in_amount, order.quote.gas_price,
                                          order.quote.sell_token_price)
            names = tuple(a.estimator for a in sent)
            assert names[: len(match.stream.forwarded)] == match.stream.forwarded
            assert match.stream.taken in match.stream.forwarded
            assert match.stream.complete == bool(match.request.winner)
            order_of = [e.estimator for e in match.request.estimates]
            position = order_of.index(match.stream.taken)
            later = [order_of.index(f) for f in match.stream.forwarded
                     if order_of.index(f) > position]
            if later:
                horizon = min(later)
            else:
                arrived = {e.answer.estimator: e.timestamp for r in world.requests
                           if r.request_id == match.request.request_id for e in r.estimates}
                horizon = position + 1
                while horizon < len(order_of) and arrived[order_of[horizon]] < order.created:
                    horizon += 1
            assert (match.stream.position, match.stream.horizon) == (position, horizon)
    for estimator, address in window.estimator_map.items():
        solver = next(s for s in world.solvers if s.estimator == estimator)
        assert address == solver.addresses[0]
    one, two = expected_suspects(world, auction_ids)
    assert window.price_suspects == frozenset(one | two)
    assert (window.one_sided_suspects, window.two_sided_suspects) == (len(one), len(two))
    trades = landed_trades(world)
    # From the world's truth, not a recomputation that can share `loo`'s arithmetic: an
    # order of a token both feeds overprice is a suspect wherever it traded.
    misfed = {u for u in created if world.orders[u].sell_token in world.misfed and u in trades}
    assert misfed <= window.price_suspects
    clean = [u for u in created if u not in one | two]
    volume = sum(round(sum(t[1] for t in trades.get(u, [])) *
                       world.orders[u].quote.sell_token_price)  # type: ignore[union-attr]
                 for u in clean)
    assert window.window_volume_native == volume
    v = window.validation
    checked = [m for m in window.matches if m.order.order_uid not in window.price_suspects]
    assert v.price_suspect == len(window.matches) - len(checked)
    assert v.requests == len(checked)
    assert v.reproduced == v.with_recorded_winner
    assert v.disagreements == [] and v.tie_mismatches == 0
    assert v.streams_reproduced == v.streams
    with_winner = tied = mixed = mixed_right = 0
    for m in checked:
        usable = [e for e in m.request.estimates if e.out_amount > 0 and e.gas > 0]
        named = [e for e in usable if e.is_winner]
        if len(named) != 1:
            continue
        with_winner += 1
        winner = named[0]
        if any(e.verified for e in usable) and not all(e.verified for e in usable):
            mixed += 1
            mixed_right += winner.verified
        pricing = m.order.pricing
        keys = {e.estimator: oracle.quote_key(
            Answer(e.estimator, int(e.out_amount), int(e.gas), e.verified), m.order.is_sell,
            m.request.in_amount, pricing.gas_price, pricing.sell_token_price) for e in usable}
        pool = [e for e in usable if e.verified] if winner.verified else usable
        tied += any(e.estimator != winner.estimator and keys[e.estimator] == keys[winner.estimator]
                    for e in pool)
    assert (v.with_recorded_winner, v.tied, v.mixed_class, v.mixed_class_correct) == (
        with_winner, tied, mixed, mixed_right)
    assert v.rate == (1.0 if with_winner else None)
    assert v.streams == len({m.request.request_id for m in checked
                             if m.stream and len(m.stream.forwarded) >= 2})
    return window


def expected_leaf(match: MatchedQuote, removed: frozenset[str], suspects: frozenset[str],
                  gas_price: float, price: float) -> tuple[str, str | None]:
    """(leaf, replacement) for a quote placed by its competition's winner."""
    order, request = match.order, match.request
    taken = request.winner
    if taken and taken not in removed:
        return "unaffected", None
    usable = [e for e in request.estimates if e.out_amount > 0 and e.gas > 0]
    if not any(e.estimator in removed for e in usable):
        return "unaffected", None
    if order.order_uid in suspects:
        return "price_suspect", None
    rest = [Answer(e.estimator, int(e.out_amount), int(e.gas), e.verified)
            for e in request.estimates if e.estimator not in removed]
    pick = oracle.quote_winner(rest, order.is_sell, request.in_amount, gas_price, price)
    if pick is None:
        return "no_quote", None
    cost = oracle.gas_in_sell(pick.gas, gas_price, price)
    quote = order.quote
    if order.is_sell:
        base = (quote.sell_amount - quote.fee) / quote.sell_amount * quote.buy_amount if (
            quote.sell_amount > 0) else 0.0
        loo = (request.in_amount - cost) / request.in_amount * pick.out_amount
    else:
        base = float(quote.sell_amount + quote.fee)
        loo = pick.out_amount + cost
    if base <= 0 or loo <= 0:
        return "nonpositive", pick.estimator
    ratio = loo / base if order.is_sell else base / loo
    if ratio > THRESHOLD or ratio < 1 / THRESHOLD:
        return "implausible", pick.estimator
    recorded = next((e for e in usable if e.estimator == taken and e.is_winner), None)
    flip = bool(recorded and recorded.verified and not pick.verified)
    return ("class_flip" if flip else "degraded"), pick.estimator


def check_analysis(world: World, window: QuoteWindow, label: str,
                   addresses: frozenset[str]) -> QuoteAnalysis:
    analysis = window.analyse(label, addresses)
    removed = frozenset(e for e, a in window.estimator_map.items() if a in addresses)
    assert analysis.window_orders == len(window.orders) - len(window.price_suspects)
    if not removed:
        assert analysis.matched == 0 and analysis.diffs == []
        return analysis
    assert analysis.matched == len(window.matches)
    leaves = {d.order_uid: d for d in analysis.diffs}
    for match in window.matches:
        uid = match.order.order_uid
        pricing = match.order.pricing
        diff = leaves.get(uid)
        if match.stream is not None:
            if diff is not None:
                assert diff.streamed
                assert match.stream.taken in removed or diff.leaf != "unaffected"
            else:
                assert match.stream.taken not in removed or not any(
                    e.estimator in removed and e.out_amount > 0 and e.gas > 0
                    for e in match.request.estimates)
            continue
        leaf, replacement = expected_leaf(match, removed, window.price_suspects,
                                          pricing.gas_price, pricing.sell_token_price)
        if leaf == "unaffected":
            assert diff is None
            continue
        assert diff is not None
        assert (diff.leaf, diff.replacement) == (leaf, replacement)
        if leaf in ("degraded", "class_flip"):
            assert diff.quote_loo is not None
            pick = next(e for e in match.request.estimates if e.estimator == replacement)
            fee = round(oracle.gas_in_sell(pick.gas, pricing.gas_price,
                                           pricing.sell_token_price))
            amount = round(pick.out_amount)
            given = round(match.request.in_amount)
            want = (Quote(given, amount, fee) if match.order.is_sell
                    else Quote(amount, given, fee))
            assert diff.quote_loo == want
        else:
            assert diff.quote_loo is None
    counts = {leaf: sum(d.leaf == leaf for d in analysis.diffs) for leaf in (
        "no_quote", "class_flip", "degraded", "unpriced", "nonpositive", "price_suspect",
        "implausible", "unrecorded")}
    assert analysis.quote_wins == len(analysis.diffs) == sum(counts.values())
    assert (analysis.no_quote, analysis.class_flips, analysis.degraded, analysis.nonpositive,
            analysis.price_suspect, analysis.implausible, analysis.unrecorded) == (
        counts["no_quote"], counts["class_flip"], counts["degraded"], counts["nonpositive"],
        counts["price_suspect"], counts["implausible"], counts["unrecorded"])
    assert analysis.stream_wins == sum(d.streamed for d in analysis.diffs)
    degraded = [d for d in analysis.diffs if d.leaf == "degraded"]
    numerator = 0.0
    for d in degraded:
        ratio = (d.effective_loo / d.effective_base if d.is_sell
                 else d.effective_base / d.effective_loo)
        numerator += (ratio - 1.0) * d.volume_native
        assert abs(d.delta_bps - (ratio - 1.0) * 10_000) <= 1e-6 * max(1.0, abs(d.delta_bps))
    assert abs(analysis.delta_volume_native - numerator) <= 1e-6 * max(1.0, abs(numerator))
    assert analysis.degraded_volume_native == sum(d.volume_native for d in degraded)
    if analysis.window_volume_native:
        assert analysis.window_bps is not None
    assert analysis.counterfactual_quotes == {
        d.order_uid: d.quote_loo for d in analysis.diffs if d.quote_loo is not None}
    return analysis


def fee_known(world: World, auction: Any, uid: str, quote: oracle.QuoteTerms | None,
              covered: set[str]) -> bool:
    """Can the fee of `uid` be computed in this auction with `quote` as its quote?"""
    order = world.orders[uid]
    if uid not in covered:
        return False
    trades = [t for s in auction.solutions for t in s.trades if t.uid == uid]
    for trade in trades:
        try:
            fees = oracle.protocol_fees(trade, order.policies, quote)
        except oracle.Refused:
            continue
        if oracle.partner_part(fees, order.partners) is not None:
            return True
    return False


def unusable(world: World, auction: Any, replacements: dict[str, Quote],
             covered: set[str]) -> set[str]:
    """Replacements that would turn a fee the record prices into one it cannot."""
    out: set[str] = set()
    for uid, q in replacements.items():
        if not any(t.uid == uid for s in auction.solutions for t in s.trades):
            continue
        terms = oracle.QuoteTerms(q.sell_amount, q.buy_amount, q.fee)
        recorded = world.orders[uid].quote_terms()
        if fee_known(world, auction, uid, recorded, covered) and not fee_known(
            world, auction, uid, terms, covered
        ):
            out.add(uid)
    return out


def check_limits(world: World, bundle: AuctionBundle, removed: frozenset[str],
                 replacements: dict[str, Quote], result: AuctionCounterfactual) -> None:
    """The two checks behind holding executions fixed under a replaced quote: which
    recharged executions would not clear the shifted limit, and whether shifting every
    score by its limit change would move the winners — recomputed with the oracle's
    arbitration."""
    valued, _ = build_solutions(bundle.bids, bundle.native_prices, world.weth,
                                fee_context=bundle.fee_context)
    held_by = {o.uid: b.uid for b in bundle.bids if b.uid in result.loo_winner_uids
               for o in b.orders}
    shifts: dict[int, dict[tuple[str, str], int]] = {}
    unclear: set[str] = set()
    for entry in valued:
        for order in entry.bid.orders:
            if order.uid not in result.quote_carried_uids:
                continue
            recorded = bundle.fee_context.quotes.get(order.uid)
            if recorded is None:
                continue
            shift = quotes.shifted_limit(order, recorded, replacements[order.uid])
            if shift is None:
                continue
            if not shift.clears and held_by.get(order.uid) == entry.bid.uid:
                unclear.add(order.uid)
            token = order.buy_token if order.side == "sell" else order.sell_token
            price = bundle.native_prices.get(token)
            atoms = shift.surplus_shift_atoms
            if price is None or not atoms:
                continue
            value = atoms * price // oracle.ONE if atoms > 0 else -((-atoms) * price // oracle.ONE)
            pair = (order.sell_token, order.buy_token)
            by_pair = shifts.setdefault(entry.bid.uid, {})
            by_pair[pair] = by_pair.get(pair, 0) + value
    assert result.quote_limit_unclearable_uids == frozenset(unclear) & result.quote_substituted_uids
    bids: list[oracle.Bid] = []
    for entry in valued:
        s = entry.solution
        if s.solver in removed:
            continue
        delta = shifts.get(s.solution_uid, {})
        values = {p: s.pair_scores.get(p, v) + delta.get(p, 0) for p, v in s.pair_values.items()}
        bids.append(oracle.Bid(s.solution_uid, s.solver, s.total + sum(delta.values()), values,
                               s.winner_pairs))
    moved = bool(shifts) and oracle.arbitrate(bids, world.max_winners).winners != set(
        result.loo_winner_uids)
    assert result.quote_shift_winner_set_changed == moved


def check_fee_path(world: World, bundle: AuctionBundle, removed: frozenset[str],
                   replacements: dict[str, Quote]) -> AuctionCounterfactual:
    """A replacement quote recharges the price-improvement fee of the orders the
    counterfactual executes, by exactly what the oracle's fee says, and moves nothing
    else; the recorded quote handed back as its own replacement moves nothing at all."""
    auction = next(a for a in world.auctions if a.auction_id == bundle.auction_id)
    settled = {u: extract.Settlement(s.landed, s.in_time) for u, s in auction.settlements.items()}
    plain = analyse_auction(bundle, world.weth, removed, max_winners=world.max_winners,
                            settled=settled)
    result = analyse_auction(bundle, world.weth, removed, max_winners=world.max_winners,
                             settled=settled, counterfactual_quotes=replacements)
    carried = {o.uid for b in bundle.bids for o in b.orders if o.uid in replacements}
    if not carried:
        assert not result.quote_carried_uids
        return result
    assert result.order_diffs == plain.order_diffs or not plain.solver_present
    assert result.delta_surplus == (plain.delta_surplus if plain.solver_present else 0)
    covered = expect.covered(auction)
    assert result.quote_carried_uids == carried - unusable(world, auction, replacements, covered)
    check_limits(world, bundle, removed, replacements, result)
    # One replacement the fee path cannot charge against is dropped on its own; the
    # others still go in.
    priced = sorted(u for u in result.quote_carried_uids
                    if any(p.kind == "priceimprovement" for p in world.orders[u].policies))
    if priced:
        victim = priced[0]
        bad = (Quote(1, 1, 10) if world.orders[victim].kind == "sell" else Quote(1, 0, 0))
        spoiled = analyse_auction(bundle, world.weth, removed, max_winners=world.max_winners,
                                  settled=settled,
                                  counterfactual_quotes={**replacements, victim: bad})
        known = fee_known(world, auction, victim, world.orders[victim].quote_terms(), covered)
        assert spoiled.quote_carried_uids == (
            result.quote_carried_uids - {victim} if known else result.quote_carried_uids)
    for uid, recharge in result.quote_fee_recharges.items():
        order = world.orders[uid]
        diff = next(d for d in result.order_diffs if d.order_uid == uid)
        assert diff.executed_loo
        assert any(p.kind == "priceimprovement" for p in order.policies)
        assert uid in covered
        winner = next(s for s in auction.solutions if s.uid in result.loo_winner_uids
                      and any(t.uid == uid for t in s.trades))
        trade = next(t for t in winner.trades if t.uid == uid)
        q = replacements[uid]
        before = oracle.order_score(trade, order.policies, order.quote_terms(), order.partners,
                                    auction.prices)
        after = oracle.order_score(trade, order.policies,
                                   oracle.QuoteTerms(q.sell_amount, q.buy_amount, q.fee),
                                   order.partners, auction.prices)
        assert recharge == after.fee_native - before.fee_native
        if before.partner_native is not None and after.partner_native is not None:
            assert result.quote_partner_fee_recharges[uid] == (
                after.partner_native - before.partner_native)
    assert result.quote_substituted_uids == frozenset(result.quote_fee_recharges) | (
        result.quote_substituted_uids - frozenset(result.quote_fee_recharges))
    assert result.quote_limit_unclearable_uids <= result.quote_substituted_uids
    # Identity: the recorded quotes as their own replacements change nothing.
    own = {uid: Quote(world.orders[uid].quote_terms().sell_amount,  # type: ignore[union-attr]
                      world.orders[uid].quote_terms().buy_amount,  # type: ignore[union-attr]
                      world.orders[uid].quote_terms().fee)  # type: ignore[union-attr]
           for uid in carried if world.orders[uid].quote_terms() is not None}
    ghost = frozenset({"ab" * 20})
    same = analyse_auction(bundle, world.weth, ghost, max_winners=world.max_winners,
                           settled=settled, counterfactual_quotes=own)
    if own:
        assert same.baseline_winner_uids == same.loo_winner_uids
        assert same.delta_surplus == 0 and same.delta_rewards == 0
        assert same.delta_protocol_and_partner_fees == 0
        assert all(v == 0 for v in same.quote_fee_recharges.values())
        assert not same.quote_limit_unclearable_uids
        assert not same.quote_shift_winner_set_changed
        assert not same.un_filtered_uids
    return result


def check_cli(sim: Sim, world: World, tmp_path: Path, label: str,
              analysis: QuoteAnalysis, window: QuoteWindow) -> None:
    out = tmp_path / "quotes"
    out.mkdir()
    run = sim.cli("analyse", *sim.window_args(), "--solver", label, "--quotes",
                  "--max-winners", str(world.max_winners), "--out", str(out / "q.json"))
    assert run.code in (0, 4), run.err
    payload = json.loads((out / "q.json").read_text())
    quote = payload["quote"]
    for name in ("quote_wins", "no_quote", "degraded", "class_flips", "matched",
                 "window_orders", "nonpositive", "implausible", "unrecorded"):
        assert quote[name] == getattr(analysis, name), name
    assert payload["quote_window"]["link"]["linked"] == window.link.linked
    assert payload["quote_window"]["price_suspects"] == len(window.price_suspects)
    assert payload["quote_fee_recharge_protocol"] == (
        payload["quote_fee_recharge"] - payload["quote_partner_fee_recharge"])
    report = aggregate.load_report(str(out / "q.json"))
    assert report.quote is not None
    assert report.quote.no_quote == analysis.no_quote
    if analysis.no_quote_share is not None:
        assert report.quote.no_quote_share == Decimal(str(analysis.no_quote_share))
    assert report.quote.matched == analysis.matched
    assert report.quote.link.linked == window.link.linked
    assert report.quote.link.orders == window.link.orders
    assert report.quote.trivial_orders == window.trivial_orders
    assert report.quote.price_suspects == len(window.price_suspects)
    assert report.quote.degraded == analysis.degraded
    if analysis.window_bps is not None:
        assert report.quote.window_bps == Decimal(str(analysis.window_bps))
    degraded = [d for d in analysis.diffs if d.leaf == "degraded"]
    if degraded:
        assert report.quote.median_bps == median(Decimal(str(d.delta_bps)) for d in degraded)
    else:
        assert report.quote.median_bps is None
    table = aggregate.comparison(aggregate.group_reports([report]))
    cell = {label: cells[0] for label, cells in table.rows}
    expected_share = (analysis.no_quote / analysis.window_orders
                      if analysis.window_orders else None)
    assert analysis.no_quote_share == expected_share
    share = (f" ({Decimal(str(expected_share)):.3%} of {analysis.window_orders:,})"
             if expected_share is not None else "")
    assert cell["orders that would not have been quoted"] == f"{analysis.no_quote:,}{share}"
    assert cell["  quote wins in the window"].startswith(
        f"{analysis.quote_wins:,} in {analysis.matched:,} matched orders")
    if window.link.orders:
        rate = Decimal(window.link.linked) / window.link.orders
        assert cell["  order↔competition link"].startswith(
            f"{window.link.linked:,} of {window.link.orders:,} ({rate:.1%})")
    else:
        assert cell["  order↔competition link"] == (
            f"{window.trivial_orders:,} trivial unwraps, no competition"
            if window.trivial_orders else "no orders")
    link = report.quote.link
    assert link.rate == (Decimal(link.linked) / link.orders if link.orders else None)
    # The window's Δquote as an amount (checked against the oracle's numerator above),
    # and the summary's quote group, native without a USD rate.
    assert report.quote.delta_volume_native == Decimal(str(analysis.delta_volume_native))
    windows = aggregate.group_reports([report])
    summary_checks.check_table(aggregate.summary(windows), windows, {})
    v = window.validation
    if v.rate is not None:
        pick = cell["  quote pick reproduced the recorded winner"]
        assert pick.startswith(
            f"{Decimal(str(v.rate)):.3%} of {v.with_recorded_winner:,} requests "
            f"(effective-sell; {v.tied:,} ties decided by arrival order")
        assert ("stream replay" in pick) == bool(v.streams)
    else:
        assert cell["  quote pick reproduced the recorded winner"] == "n/a"
    unlinked = analysis.matched < len(window.orders) - window.trivial_orders
    assert any("could be linked" in w for w in table.warnings) == unlinked
    rerun = sim.cli("analyse", *sim.window_args(), "--solver", label, "--quotes",
                    "--quote-ranking", "autopilot", "--max-winners", str(world.max_winners))
    assert rerun.code in (0, 4), rerun.err


__all__ = ["replace"]
