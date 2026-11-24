"""The mechanism's rules, driven directly from seeded inputs rather than through the DB.

Two places where the DB-shaped world is too polite to reach every corner: the arbitration
(ties on every key, dark pairs on purpose, `max_winners` of one) and the quote ranking
(answers under water, truncation ties, both rules). Both are compared with `oracle` on
every generated case, and the claims the code makes about itself — "False is a proof",
"surplus is a lower bound" — are checked by brute force.
"""

from __future__ import annotations

import itertools
import math
import random
from decimal import Decimal
from fractions import Fraction

import pytest
from sim import oracle
from sim.oracle import Answer

from loo import aggregate, quotes
from loo.counterfactual import leave_one_out, un_filtered
from loo.fees import Quote
from loo.quotes import Estimate, Pricing, QuoteRequest, StreamTake, UnrecordedStreamError
from loo.valuation import Order
from loo.winner_selection import (
    Solution,
    arbitrate,
    compute_reference_scores,
    filter_guess_could_matter,
    filter_scale,
    is_fair,
    pick_winners,
)

WETH = "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
TOKENS = (WETH, "ee" * 20, "a0" * 20, "b1" * 20, "c2" * 20)


def random_solutions(rng: random.Random) -> list[Solution]:
    """Bids in `uid` order, with small value ranges so that ties are everywhere."""
    solvers = [f"{i:02x}" * 20 for i in range(rng.randint(1, 5))]
    out: list[Solution] = []
    for uid in range(rng.randint(1, 12)):
        pairs: dict[tuple[str, str], int] = {}
        for _ in range(rng.choice((1, 1, 2, 3))):
            sell, buy = rng.sample(TOKENS, 2)
            pairs[(sell, buy)] = rng.choice((0, 5, 10, 10, 20, 40))
        scores = {
            p: v + rng.choice((0, 0, 3, 10)) for p, v in pairs.items() if rng.random() < 0.7
        }
        dark = sum(v for p, v in pairs.items() if p not in scores)
        total = sum(scores.values()) + dark + rng.choice((0, 0, 5))
        claims = frozenset(
            (WETH if s == "ee" * 20 else s, WETH if b == "ee" * 20 else b) for s, b in pairs
        )
        out.append(Solution(solver=rng.choice(solvers), solution_uid=uid, total=total,
                            pair_values=pairs, pair_scores=scores, winner_pairs=claims))
    return out


def bids_of(solutions: list[Solution]) -> list[oracle.Bid]:
    return [
        oracle.Bid(s.solution_uid, s.solver, s.total,
                   {p: s.pair_scores.get(p, v) for p, v in s.pair_values.items()},
                   s.winner_pairs)
        for s in solutions
    ]


PROTOCOL_MAX_WINNERS = 20
"""`max_winners` in services `crates/configs/src/autopilot/run_loop.rs`. A literal, not
`loo.primitives.MAX_WINNERS`, so a drifted constant fails here."""


def test_default_max_winners_is_the_protocols(seed: int) -> None:
    """Generated worlds never hold 20 disjoint winners, so the default needs its own case."""
    rng = random.Random(seed)
    tokens = [f"{i:02x}" * 20 for i in range(rng.randint(8, 12))]
    pairs = rng.sample(list(itertools.permutations(tokens, 2)), rng.randint(21, 40))
    solutions = [
        Solution(solver=f"{uid % 7:02x}" * 20, solution_uid=uid, total=v,
                 pair_values={p: v}, pair_scores={p: v}, winner_pairs=frozenset({p}))
        for uid, (p, v) in enumerate((p, rng.randint(1, 10**6)) for p in pairs)
    ]
    ranking = arbitrate(solutions)
    truth = oracle.arbitrate(bids_of(solutions), PROTOCOL_MAX_WINNERS)
    assert ranking.winner_uids == truth.winners
    assert len(ranking.winner_uids) == PROTOCOL_MAX_WINNERS


def test_arbitration_differential(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(25):
        solutions = random_solutions(rng)
        max_winners = rng.choice((1, 2, 3, 20))
        ranking = arbitrate(solutions, max_winners)
        truth = oracle.arbitrate(bids_of(solutions), max_winners)
        assert [s.solution_uid for s in ranking.ranked] == [b.uid for b in truth.ranked]
        assert [s.solution_uid for s in ranking.filtered_out] == [b.uid for b in truth.filtered]
        assert ranking.winner_uids == truth.winners
        assert ranking.baselines == truth.baselines
        assert ranking.winning_score == sum(b.total for b in truth.ranked if b.uid in truth.winners)
        assert compute_reference_scores(ranking, max_winners) == {
            k: v for k, (v, _) in oracle.references(truth.ranked, truth.winners,
                                                     max_winners).items()}
        for s in solutions:
            bid = next(b for b in bids_of([s]))
            fair = len(bid.values) == 1 or all(
                v >= truth.baselines.get(p, 0) for p, v in bid.values.items())
            assert is_fair(s, truth.baselines) == fair
            guessed = any(
                p not in s.pair_scores and v < truth.baselines.get(p, 0)
                for p, v in s.pair_values.items())
            assert filter_scale(s, truth.baselines) == ("surplus" if guessed else "score")
        ordered = sorted(ranking.ranked, key=lambda s: -s.total)
        picked = pick_winners(ordered, max_winners)
        assert {ordered[i].solution_uid for i in picked} == {
            b.uid for b in oracle._pick(bids_of(ordered), max_winners)}  # pyright: ignore[reportPrivateUsage]
        guesses = {
            s.solution_uid for s in solutions if len(s.pair_values) > 1 and any(
                p not in s.pair_scores and v < truth.baselines.get(p, 0)
                for p, v in s.pair_values.items())}
        assert ranking.proxied_uids == guesses
        # Every guess restored at once, then re-picked in uid tie-break order.
        restored = sorted(
            (b for b in truth.ranked + truth.filtered
             if b in truth.ranked or b.uid in guesses),
            key=lambda b: (-b.total, b.uid))
        could = bool(guesses) and {b.uid for b in oracle._pick(  # pyright: ignore[reportPrivateUsage]
            restored, max_winners)} != truth.winners
        assert filter_guess_could_matter(ranking, max_winners) == could
        # "False is a proof": no subset of the guessed verdicts, flipped, moves a winner.
        if not filter_guess_could_matter(ranking, max_winners):
            for size in range(len(ranking.proxied_uids) + 1):
                for flipped in itertools.combinations(sorted(ranking.proxied_uids), size):
                    kept = [s for s in ranking.ranked + ranking.filtered_out
                            if s in ranking.ranked or s.solution_uid in flipped]
                    kept.sort(key=lambda s: (-s.total, s.solution_uid))
                    again = {kept[i].solution_uid for i in pick_winners(kept, max_winners)}
                    assert again == ranking.winner_uids
        # Removing a solver: a full re-arbitration that only ever relaxes the filter.
        solver = rng.choice([s.solver for s in solutions])
        removed = frozenset({solver})
        loo = leave_one_out(solutions, removed, max_winners)
        rest = [s for s in solutions if s.solver != solver]
        assert loo.winner_uids == arbitrate(rest, max_winners).winner_uids
        relaxed = un_filtered(ranking, loo, removed)
        assert relaxed == {s.solution_uid for s in loo.ranked} - {
            s.solution_uid for s in ranking.ranked}
        assert leave_one_out(solutions, frozenset(), max_winners).winner_uids == (
            ranking.winner_uids)
        # The surplus fallback can only make the filter stricter than the truth.
        lit = [Solution(s.solver, s.solution_uid, s.total, s.pair_values,
                        {p: s.pair_scores.get(p, v + 10) for p, v in s.pair_values.items()},
                        winner_pairs=s.winner_pairs) for s in solutions]
        lit_kept = {s.solution_uid for s in arbitrate(lit, max_winners).ranked}
        assert {s.solution_uid for s in ranking.ranked} <= lit_kept


# --- the quote competition ------------------------------------------------------------


def random_ladder(rng: random.Random) -> tuple[list[Answer], bool, float, float, float]:
    is_sell = rng.random() < 0.6
    in_amount = float(rng.choice((10, 35, 10**6, 2 * 10**14, 10**18)))
    gas_price = float(rng.choice((1, 55_000_000, 2 * 10**9)))
    price = rng.choice((1.0, 1e-6, 3.3e-10))
    base = rng.randint(1, 10**6)
    answers: list[Answer] = []
    for i in range(rng.randint(0, 6)):
        out = rng.choice((base, base, base + rng.randint(-5, 5), 0, base * 10))
        gas = rng.choice((0, 100, 229_190, 286_090, rng.randint(1, 400_000)))
        answers.append(Answer(f"e{i}", max(out, 0), gas, rng.random() < 0.6))
    return answers, is_sell, in_amount, gas_price, price


def estimates(answers: list[Answer], winner: str = "") -> tuple[Estimate, ...]:
    return tuple(Estimate(a.estimator, a.out_amount, a.gas, a.verified, a.estimator == winner)
                 for a in answers)


def autopilot_key(a: Answer, is_sell: bool, gas_price: float, sell: float,
                  buy: float | None) -> float:
    price = sell if not is_sell else buy
    if not price:
        raise quotes.UnpricedRequestError("unpriced")
    out_wei, gas_wei = a.out_amount * price, a.gas * gas_price
    if is_sell:
        value = out_wei - gas_wei
        return float(math.trunc(value)) if value > 0 else 0.0
    return -float(math.trunc(out_wei + gas_wei))


def stream_replacement_oracle(
    answers: list[Answer], removed: set[str], is_sell: bool, in_amount: float, gas_price: float,
    price: float, position: int, horizon: int, complete: bool,
) -> tuple[str | None, bool] | str:
    """The best remaining answer that had arrived by the commit, else the next remaining
    sendable one; "unrecorded" where the stream was dropped before it."""
    def fits(a: Answer) -> bool:
        return not is_sell or oracle.gas_in_sell(a.gas, gas_price, price) < in_amount

    def at(upto: int) -> Answer | str | None:
        best = oracle.quote_winner([a for a in answers[:upto] if a.estimator not in removed],
                                   is_sell, in_amount, gas_price, price)
        if best is not None:
            return best
        later = [a for a in answers[upto:] if a.estimator not in removed
                 and oracle.answer_usable(a) and fits(a)]
        if later:
            return later[0]
        return None if complete else "unrecorded"

    if not any(oracle.answer_usable(a) for a in answers if a.estimator not in removed):
        return "unrecorded" if not complete else (None, False)
    late = at(horizon)
    if isinstance(late, str):
        return late
    early = at(position + 1)
    early_name = None if isinstance(early, str) or early is None else early.estimator
    late_name = late.estimator if late else None
    return late_name, late_name != early_name


def test_quote_rules_differential(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(40):
        answers, is_sell, in_amount, gas_price, price = random_ladder(rng)
        pricing = Pricing(gas_price, price, rng.choice((None, 2e-6)))
        ladder = estimates(answers)
        ours = quotes.pick(ladder, is_sell=is_sell, in_amount=in_amount, pricing=pricing)
        truth = oracle.quote_winner(answers, is_sell, in_amount, gas_price, price)
        assert (ours.estimator if ours else None) == (truth.estimator if truth else None)
        sent = quotes.replay_stream(ladder, is_sell=is_sell, in_amount=in_amount, pricing=pricing)
        assert sent == [a.estimator for a in oracle.stream_forwards(
            answers, is_sell, in_amount, gas_price, price)]
        for a, e in zip(answers, ladder, strict=True):
            if not oracle.answer_usable(a):
                assert not e.reasonable
                continue
            assert quotes.rank_key(e, is_sell=is_sell, in_amount=in_amount, pricing=pricing,
                                   ranking="effective-sell") == oracle.quote_key(
                a, is_sell, in_amount, gas_price, price)
            try:
                want: float | None = autopilot_key(a, is_sell, gas_price, price,
                                                   pricing.buy_token_price)
            except quotes.UnpricedRequestError:
                want = None
            if want is None:
                with pytest.raises(quotes.UnpricedRequestError):
                    quotes.rank_key(e, is_sell=is_sell, in_amount=in_amount, pricing=pricing,
                                    ranking="autopilot")
            else:
                assert quotes.rank_key(e, is_sell=is_sell, in_amount=in_amount,
                                       pricing=pricing, ranking="autopilot") == want
            q = quotes.counterfactual_quote(e, is_sell=is_sell, in_amount=in_amount,
                                            pricing=pricing)
            fee = round(oracle.gas_in_sell(a.gas, gas_price, price))
            assert q == (Quote(round(in_amount), a.out_amount, fee) if is_sell
                         else Quote(a.out_amount, round(in_amount), fee))
        if not answers:
            continue
        removed = set(rng.sample([a.estimator for a in answers], rng.randint(0, len(answers))))
        request = QuoteRequest("r", "sell" if is_sell else "buy", "aa", "bb", in_amount,
                               truth.estimator if truth else "", ladder)
        left = quotes.leave_one_out(request, frozenset(removed), pricing=pricing)
        expected = oracle.quote_winner([a for a in answers if a.estimator not in removed],
                                       is_sell, in_amount, gas_price, price)
        assert (left.estimator if left else None) == (expected.estimator if expected else None)
        position = rng.randrange(len(answers))
        horizon = rng.randint(position + 1, len(answers))
        complete = rng.random() < 0.6
        take = StreamTake(answers[position].estimator, position, horizon, complete)
        want_stream = stream_replacement_oracle(answers, removed, is_sell, in_amount,
                                                gas_price, price, position, horizon, complete)
        if want_stream == "unrecorded":
            with pytest.raises(UnrecordedStreamError):
                quotes.stream_replacement(request, take, frozenset(removed), pricing=pricing)
        else:
            got, moved = quotes.stream_replacement(request, take, frozenset(removed),
                                                   pricing=pricing)
            assert ((got.estimator if got else None), moved) == want_stream


def effective_rate(q: Quote, is_sell: bool) -> Fraction:
    if is_sell:
        return Fraction(q.buy_amount * (q.sell_amount - q.fee), q.sell_amount**2) if (
            q.sell_amount > 0) else Fraction(0)
    cost = q.sell_amount + q.fee
    return Fraction(q.buy_amount, cost) if cost > 0 else Fraction(0)


def test_shifted_limit(seed: int) -> None:
    """The limit a replacement quote implies: the user's wedge over the quote, applied to
    the replacement — clears whenever the replacement is no better, and the measured
    surplus moves by the limit's own move."""
    rng = random.Random(seed)
    for _ in range(40):
        is_sell = rng.random() < 0.5
        sell, buy = rng.randint(1, 10**9), rng.randint(1, 10**9)
        recorded = Quote(sell, buy, rng.randint(0, sell // 10))
        replacement = Quote(max(1, sell + rng.randint(-sell // 5, sell // 5)),
                            max(1, buy + rng.randint(-buy // 5, buy // 5)),
                            rng.randint(0, sell // 10))
        limit_sell, limit_buy = sell, max(1, buy * rng.randint(90, 100) // 100)
        if is_sell:
            executed_sell = rng.randint(1, limit_sell)
            executed_buy = math.ceil(Fraction(executed_sell * limit_buy, limit_sell)) + rng.randint(0, 50)
        else:
            executed_buy = rng.randint(1, limit_buy)
            executed_sell = max(1, executed_buy * limit_sell // limit_buy - rng.randint(0, 50))
        order = Order("u", "aa", "bb", limit_sell, limit_buy, executed_sell, executed_buy,
                      "sell" if is_sell else "buy")
        shift = quotes.shifted_limit(order, recorded, replacement)
        before, after = effective_rate(recorded, is_sell), effective_rate(replacement, is_sell)
        if before <= 0 or after <= 0:
            assert shift is None
            continue
        assert shift is not None
        ratio = after / before
        limit_rate = Fraction(limit_buy, limit_sell) * ratio
        assert shift.clears == (Fraction(executed_buy, executed_sell) >= limit_rate)
        if is_sell:
            required = Fraction(limit_buy * executed_sell, limit_sell)
            want = required - required * ratio
        else:
            allowed = Fraction(limit_sell * executed_buy, limit_buy)
            want = allowed / ratio - allowed
        assert shift.surplus_shift_atoms == int(want)
        if after <= before and Fraction(executed_buy, executed_sell) >= Fraction(limit_buy, limit_sell):
            assert shift.clears and shift.surplus_shift_atoms >= 0
        same = quotes.shifted_limit(order, recorded, recorded)
        assert same is not None and same.surplus_shift_atoms == 0


def test_formatting_differential(seed: int) -> None:
    """The table's number formats, against `Decimal` quantize: wei as ETH rounded half
    away from zero, signs only on positives, basis points, COW, dollars, percentages."""
    rng = random.Random(seed)
    for _ in range(40):
        wei = rng.choice((0, 1, -1, 5 * 10**11, -5 * 10**11, rng.randint(-10**22, 10**22),
                          rng.randint(-10**13, 10**13)))
        places = rng.choice((2, 4, 6))
        assert aggregate.eth(wei, places) == oracle.eth_text(wei, places)
        assert aggregate.signed_eth(wei) == oracle.signed_eth_text(wei)
        value = Decimal(rng.randint(-20_000, 20_000)) / Decimal(rng.choice((1, 100, 10_000)))
        assert aggregate.bps(value) == ("+" if value > 0 else "") + f"{value:.2f} bps"
        cow = Decimal(rng.randint(-10**24, 10**24))
        assert aggregate.cow_amount(cow) == str((cow / Decimal(10**18)).quantize(Decimal("0.01")))
        usd = Decimal(rng.randint(-10**8, 10**8)) / Decimal(rng.choice((1, 10**4, 10**8)))
        places_usd = 2 if abs(usd) >= Decimal("0.01") or not usd else 4
        assert aggregate.usd_amount(usd) == (
            ("-" if usd < 0 else "") + "$" + f"{abs(usd):,.{places_usd}f}")
        part, whole = rng.randint(0, 50), rng.choice((0, 7, 100))
        assert aggregate.pct(part, whole) == (f"{part / whole:.1%}" if whole else "n/a")
