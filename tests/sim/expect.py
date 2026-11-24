"""What the pipeline should report, derived from the world's truth with `oracle`.

The counterfactual here is the naive one, written from the outcome-rule description in
`loo.counterfactual.OUTCOME_RULE` and docs/rewards.md: arbitrate twice, give each winner
an outcome (its own record if it won for real, else the slots it displaced), sum surplus
and fees over the executed orders, and run the mart formula on each side. It never calls
`loo`; it is compared with `loo` only where both see the same filter inputs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from . import oracle
from .oracle import Bid, Pair, Refused
from .world import Auction, Cap, Solution, World

PRICE_THRESHOLD = 2


def suspect_uids(auction: Auction) -> set[int]:
    """Solutions whose sold and bought value disagree by more than 2x at the auction's
    own prices, orders missing a price on either side skipped."""
    flagged: set[int] = set()
    for s in auction.solutions:
        sold = bought = 0
        for t in s.trades:
            ps, pb = auction.prices.get(t.sell_token), auction.prices.get(t.buy_token)
            if ps is None or pb is None:
                continue
            sold += t.executed_sell * ps // oracle.ONE
            bought += t.executed_buy * pb // oracle.ONE
        if sold and bought and (sold > PRICE_THRESHOLD * bought or bought > PRICE_THRESHOLD * sold):
            flagged.add(s.uid)
    return flagged


def surplus_pairs(world: World, auction: Auction, s: Solution) -> dict[Pair, int]:
    pairs: dict[Pair, int] = {}
    for t in s.trades:
        pair = (t.sell_token, t.buy_token)
        pairs[pair] = pairs.get(pair, 0) + s.order_scores[t.uid].surplus_native
    return pairs


def bids(world: World, auction: Auction, mode: str) -> list[Bid]:
    """The auction's solutions as the arbitration sees them under perfect information:
    true per-pair scores in score mode, surplus in surplus mode."""
    out: list[Bid] = []
    for s in auction.solutions:
        if mode == "score":
            out.append(Bid(s.uid, s.solver, s.score, s.pair_scores, s.claims))
        else:
            values = surplus_pairs(world, auction, s)
            out.append(Bid(s.uid, s.solver, sum(values.values()), values, s.claims))
    return out


@dataclass
class Side:
    winners: set[int]
    executed: dict[int, bool]
    references: dict[str, int]
    rewards: dict[str, oracle.RewardRow] = field(default_factory=dict[str, oracle.RewardRow])


@dataclass
class Counterfactual:
    base: Side
    loo: Side
    kept_base: set[int]
    kept_loo: set[int]
    surplus_base: int
    surplus_loo: int
    fees_base: int | None
    fees_loo: int | None
    partner_base: int | None
    partner_loo: int | None
    only_with: int
    only_without: int
    orders_compared: int

    @property
    def delta_surplus(self) -> int:
        return self.surplus_loo - self.surplus_base

    @property
    def rewards_base(self) -> int:
        return sum(r.uncapped for r in self.base.rewards.values())

    @property
    def rewards_loo(self) -> int:
        return sum(r.uncapped for r in self.loo.rewards.values())

    def capped(self, side: Side) -> Decimal | None:
        total = Decimal(0)
        for r in side.rewards.values():
            if r.capped is None:
                return None
            total += r.capped
        return total


def counterfactual(
    world: World,
    auction: Auction,
    removed: frozenset[str],
    *,
    rule: str,
    mode: str = "score",
    known: set[str] | None = None,
) -> Counterfactual:
    """`known`: orders whose fee policies are recorded (else every order is)."""
    all_bids = bids(world, auction, mode)
    by_uid = {s.uid: s for s in auction.solutions}
    base = oracle.arbitrate(all_bids, world.max_winners)
    loo = oracle.arbitrate([b for b in all_bids if b.solver not in removed], world.max_winners)
    recorded = {s.uid for s in auction.winners}
    inherit: dict[Pair, bool] = {}
    caps_by_pair: dict[Pair, tuple[int, Cap]] = {}
    for uid in sorted(recorded):
        for pair in by_uid[uid].claims:
            inherit[pair] = auction.settlements[uid].in_time
            caps_by_pair[pair] = (uid, auction.caps[uid])

    def executed(uid: int) -> bool:
        if rule == "assume-settled":
            return True
        if uid in recorded:
            return auction.settlements[uid].in_time
        held = [inherit[p] for p in by_uid[uid].claims if p in inherit]
        return all(held)

    def side(outcome: oracle.Outcome) -> Side:
        refs = oracle.references(outcome.ranked, outcome.winners, world.max_winners)
        result = Side(set(outcome.winners), {u: executed(u) for u in outcome.winners},
                      {solver: score for solver, (score, _) in refs.items()})
        if mode != "score":
            return result
        flat = auction.flat_lower if recorded else None
        wins: list[oracle.WinRecord] = []
        for bid in outcome.ranked:
            if bid.uid not in outcome.winners:
                continue
            if bid.uid in recorded:
                cap = auction.caps[bid.uid]
                upper, penalty = cap.upper, cap.penalty
            else:
                displaced = {caps_by_pair[p][0]: caps_by_pair[p][1]
                             for p in by_uid[bid.uid].claims if p in caps_by_pair}
                if displaced:
                    upper = sum((c.upper for c in displaced.values()), Decimal(0))
                    penalties = [c.penalty for c in displaced.values()]
                    penalty = (None if any(p is None for p in penalties)
                               else sum((p for p in penalties if p is not None), Decimal(0)))
                else:
                    upper, penalty = None, None
            wins.append(oracle.WinRecord(bid.solver, bid.total, result.executed[bid.uid],
                                         upper, penalty))
        result.rewards = oracle.rewards(wins, result.references, flat,
                                        auction.excluded if recorded else False)
        return result

    b, lo = side(base), side(loo)

    def orders(s: Side) -> dict[str, tuple[bool, Solution]]:
        return {t.uid: (s.executed[u], by_uid[u]) for u in s.winners for t in by_uid[u].trades}

    ob, ol = orders(b), orders(lo)
    surplus_base = surplus_loo = 0
    fees: dict[str, int | None] = {"base": 0, "loo": 0}
    partners: dict[str, int | None] = {"base": 0, "loo": 0}
    only_with = only_without = 0
    for uid in set(ob) | set(ol):
        eb = ob.get(uid, (False, None))[0]
        el = ol.get(uid, (False, None))[0]
        only_with += eb and not el
        only_without += el and not eb
        for name, entry in (("base", ob.get(uid)), ("loo", ol.get(uid))):
            if entry is None or not entry[0]:
                continue
            score = entry[1].order_scores[uid]
            if name == "base":
                surplus_base += score.surplus_native
            else:
                surplus_loo += score.surplus_native
            fee_known = (known is None or uid in known) and score.partner_native is not None
            if not fee_known:
                fees[name] = None
                partners[name] = None
            else:
                current_fee, current_partner = fees[name], partners[name]
                if current_fee is not None and current_partner is not None:
                    fees[name] = current_fee + score.fee_native
                    partners[name] = current_partner + (score.partner_native or 0)
    return Counterfactual(
        base=b, loo=lo,
        kept_base={x.uid for x in base.ranked}, kept_loo={x.uid for x in loo.ranked},
        surplus_base=surplus_base, surplus_loo=surplus_loo,
        fees_base=fees["base"], fees_loo=fees["loo"],
        partner_base=partners["base"], partner_loo=partners["loo"],
        only_with=only_with, only_without=only_without,
        orders_compared=len(set(ob) | set(ol)),
    )


def covered(auction: Auction) -> set[str]:
    return {t.uid for s in auction.solutions if not s.filtered_out for t in s.trades}


def executed_volume(world: World, auction: Auction, rule: str) -> tuple[int, int, int]:
    """(fill-or-kill volume, fill-or-kill orders, all orders) the recorded winners
    executed under the rule."""
    volume = fok = every = 0
    for s in auction.winners:
        if rule == "inherited" and not auction.settlements[s.uid].in_time:
            continue
        for t in s.trades:
            every += 1
            if t.partially_fillable:
                continue
            volume += t.executed_buy * auction.prices[t.buy_token] // oracle.ONE
            fok += 1
    return volume, fok, every


def label_addresses(world: World, label: str) -> frozenset[str]:
    """What `--solver <label>` removes: every address of the catalogue name that bid in
    the window, or the one address given."""
    bidding = world.bidding_addresses()
    for solver in world.solvers:
        if solver.name.lower() == label.lower() and solver.name != "Uncatalogued":
            return frozenset(a for a in solver.addresses if a in bidding)
    raw = label.lower().removeprefix("0x")
    return frozenset({raw}) if raw in bidding else frozenset()


def fee_known_everywhere(scores: Iterable[oracle.OrderScore]) -> bool:
    return all(s.partner_native is not None for s in scores)


__all__ = ["Refused"]
