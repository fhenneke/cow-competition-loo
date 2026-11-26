"""Hand-built worlds for shapes worth keeping exactly: real-data fixtures and the cases
that once exposed a defect. Built with the same recording path as generated worlds
(`world.record_auction`), so they run through every check a seed does."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .oracle import FeePolicy, Trade
from .world import (
    NATIVE,
    WRAPPED,
    Auction,
    Order,
    OrderQuote,
    Settlement,
    Solver,
    World,
    candidate,
    record_auction,
    record_mart,
    settle,
)

ONE = 10**18
DAI = "6b175474e89094c44da98b954eedeac495271d0f"
USDC = "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
TOKEN_X = "1f" * 20


def blank(network: str = "sepolia", max_winners: int = 20) -> World:
    weth = WRAPPED[network]
    return World(seed=0, network=network, start="2026-11-10", end="2026-11-11",
                 max_winners=max_winners,
                 tokens={weth: ONE, NATIVE: ONE, DAI: ONE, USDC: ONE, TOKEN_X: ONE},
                 solvers=[], orders={}, auctions=[])


def solver(world: World, name: str, address: str) -> str:
    world.solvers.append(Solver(name=name, addresses=[address]))
    return address


def order(world: World, uid: str, sell: str, buy: str, sell_amount: int, buy_amount: int,
          kind: str = "sell", **extra: object) -> Order:
    made = Order(uid=uid, sell_token=sell, buy_token=buy, sell_amount=sell_amount,
                 buy_amount=buy_amount, kind=kind, partially_fillable=False,
                 created=datetime(2026, 11, 10, 1), **extra)  # type: ignore[arg-type]
    world.orders[uid] = made
    return made


def fill(o: Order, executed_sell: int, executed_buy: int) -> Trade:
    return Trade(o.uid, o.sell_token, o.buy_token, o.sell_amount, o.buy_amount,
                 executed_sell, executed_buy, o.kind, o.partially_fillable)


def auction(
    world: World,
    auction_id: int,
    bids: list[tuple[str, list[Trade]]],
    *,
    settled: dict[int, Settlement] | None = None,
    penalties: dict[int, Decimal | None] | None = None,
    prices: dict[str, int] | None = None,
) -> Auction:
    """`bids` in tie-break order; `settled`/`penalties` keyed by position in `bids`."""
    made = Auction(auction_id, 23_500_000 + auction_id % 1000,
                   datetime(2026, 11, 10, 2, auction_id % 60),
                   dict(prices or world.tokens),
                   sorted({t.uid for _, trades in bids for t in trades}))
    candidates = [candidate(world, made, address, trades) for address, trades in bids]
    record_auction(world, made, candidates)
    position = {id(c[1]): i for i, c in enumerate(candidates)}
    for s in made.winners:
        index = position[id(s.trades)]
        settle(made, s, (settled or {}).get(index, Settlement(True, True)),
               (penalties or {}).get(index, Decimal(10**13)))
    record_mart(made)
    world.auctions.append(made)
    return made


def live_fee_fixture() -> World:
    """Auction 13509109 solution 0: a sell order carrying all three policy kinds —
    `priceimprovement(0.5, 0.0098)` and two volume fees, the last a partner's. The case
    that exposed the `mul_f64` rounding; its recorded score is 1330654764658941815 wei.
    A second, fee-free order on another pair makes the bid two-pair, so the per-pair
    score split is exercised on real fees."""
    world = blank()
    live = order(
        world,
        "e1e11c8242da276c0ccb8e72787c89cf697c329aa9b3c83ec286d690c1f3eec2e47afc755ab0ac66d7ede9b0d4e61cdd6d28934e6a710e4c",
        USDC, NATIVE, 120500000000000000000, 119140722704767911115,
        policies=(FeePolicy("priceimprovement", 0.5, 0.0098), FeePolicy("volume", 3e-05),
                  FeePolicy("volume", 0.003)),
        partners=1,
        quote=OrderQuote(solver="aa" * 20, sell_amount=120500000000000000000,
                         buy_amount=120466675793142735132, gas_amount=399631.0,
                         gas_price=317298105.0, sell_token_price=0.999861438619223,
                         verified=True, quote_id=None),
    )
    second = order(world, "f" * 112, WRAPPED["sepolia"], USDC, 1000, 2000)
    a = solver(world, "Baseline", "ba" * 20)
    b = solver(world, "Quasi", "9a" * 20)
    auction(world, 13509109, [
        (a, [fill(live, 120500000000000000000, 120103953122547755793), fill(second, 1000, 2100)]),
        (b, [fill(live, 120500000000000000000, 119240722704767911115)]),
    ])
    return world


def dark_pair() -> World:
    """Auction 13492893's shape: a three-pair bid whose DAI→USDC and X→DAI orders no
    kept bid trades, so their fees are unrecorded and, two of them, the residual cannot
    tell them apart: both pairs are dark. Its WETH→USDC pair is priced and fails the
    baseline, so the bid is unfair whatever the dark pairs are worth, and the verdict is
    a proof rather than a guess."""
    world = blank()
    weth = WRAPPED["sepolia"]
    o1 = order(world, "11" * 56, weth, USDC, 10**18, 10**18,
               policies=(FeePolicy("volume", 0.001),))
    o2 = order(world, "22" * 56, DAI, USDC, 10**18, 10**18,
               policies=(FeePolicy("volume", 0.001),))
    o3 = order(world, "23" * 56, TOKEN_X, DAI, 10**18, 10**18)
    a = solver(world, "Arc", "a1" * 20)
    b = solver(world, "Arctic", "a2" * 20)
    auction(world, 13492893, [
        (a, [fill(o1, 10**18, 10**18 + 10**15), fill(o2, 10**18, 10**18 + 9 * 10**16),
             fill(o3, 10**18, 10**18 + 10**16)]),
        (b, [fill(o1, 10**18, 10**18 + 5 * 10**16)]),
    ])
    return world


def ranked_order() -> World:
    """`ranked` is winners first: scores 100 (pairs P, R), 90 (P, Q) and 80 (Q). The 90
    loses to the 100 on P; the 80 wins Q. Re-picking without the 100's solver walks
    [80, 90], so its reference score is 80 — a score-sorted walk would say 90."""
    world = blank()
    weth = WRAPPED["sepolia"]
    p = order(world, "31" * 56, weth, USDC, 10**6, 10**6)
    r = order(world, "32" * 56, DAI, TOKEN_X, 10**6, 10**6)
    q = order(world, "33" * 56, TOKEN_X, DAI, 10**6, 10**6)
    q2 = order(world, "34" * 56, TOKEN_X, DAI, 10**6, 10**6)
    first = solver(world, "Sector", "c1" * 20)
    second = solver(world, "Horadrim", "c2" * 20)
    third = solver(world, "Quasi", "c3" * 20)
    # Every price is 1e18 and nothing carries a fee, so a score is its surplus in atoms.
    auction(world, 14000001, [
        (first, [fill(p, 10**6, 10**6 + 50), fill(r, 10**6, 10**6 + 50)]),
        (second, [fill(p, 10**6, 10**6 + 10), fill(q, 10**6, 10**6 + 80)]),
        (third, [fill(q2, 10**6, 10**6 + 80)]),
    ])
    return world


def blocked_batch() -> World:
    """Auction 13488369's shape, through the native-token asymmetry: a single-order bid
    on ETH→X outranks a two-order batch on WETH→X and Y. Baselines key on raw pairs, so
    the batch is fair; winners claim `as_erc20` pairs, so it is blocked. Remove the
    single order's solver and the batch wins both of its orders — one of which nobody
    else bid on: executed only without the removed solver."""
    world = blank()
    weth = WRAPPED["sepolia"]
    native_leg = order(world, "41" * 56, NATIVE, TOKEN_X, 10**9, 10**9)
    wrapped_leg = order(world, "42" * 56, weth, TOKEN_X, 10**9, 10**9)
    other = order(world, "43" * 56, DAI, USDC, 10**9, 10**9)
    single = solver(world, "Gnosis_0x", "d1" * 20)
    batch = solver(world, "ExtQuasimodo", "d2" * 20)
    auction(world, 13488369, [
        (single, [fill(native_leg, 10**9, 10**9 + 500)]),
        (batch, [fill(wrapped_leg, 10**9, 10**9 + 200), fill(other, 10**9, 10**9 + 100)]),
    ])
    return world


def penalty_caps() -> World:
    """Two winners that both reverted. One's penalty cap is 0 — a real
    cap, flooring its reward at 0 — the other's is missing, so it falls back to the flat
    floor. A zero-score bid that trades at exactly its limit is kept, and wins its pair."""
    world = blank()
    weth = WRAPPED["sepolia"]
    o1 = order(world, "51" * 56, weth, USDC, 10**9, 10**9)
    o2 = order(world, "52" * 56, DAI, TOKEN_X, 10**9, 10**9)
    o3 = order(world, "53" * 56, TOKEN_X, USDC, 10**9, 10**9)
    a = solver(world, "Baseline", "e1" * 20)
    b = solver(world, "Sector", "e2" * 20)
    c = solver(world, "Quasi", "e3" * 20)
    auction(world, 14000002, [
        (a, [fill(o1, 10**9, 10**9 + 300)]),
        (b, [fill(o2, 10**9, 10**9 + 200)]),
        (c, [fill(o3, 10**9, 10**9)]),
        (c, [fill(o1, 10**9, 10**9 + 100)]),
    ], settled={0: Settlement(False, False), 1: Settlement(True, False)},
        penalties={0: Decimal(0), 1: None})
    return world


def split_slot() -> World:
    """One recorded winner holding two pairs, and two rivals that each want one of them.
    Without the winner both rivals win, both inherit its caps, and the capped estimate
    counts them twice — which the result has to say (`cap_double_inherited`)."""
    world = blank()
    weth = WRAPPED["sepolia"]
    p = order(world, "61" * 56, weth, USDC, 10**9, 10**9)
    q = order(world, "62" * 56, DAI, TOKEN_X, 10**9, 10**9)
    a = solver(world, "Sector", "f1" * 20)
    b = solver(world, "Quasi", "f2" * 20)
    c = solver(world, "Baseline", "f3" * 20)
    auction(world, 14000003, [
        (a, [fill(p, 10**9, 10**9 + 100), fill(q, 10**9, 10**9 + 100)]),
        (b, [fill(p, 10**9, 10**9 + 90)]),
        (c, [fill(q, 10**9, 10**9 + 80)]),
    ])
    return world


def cancelled_price() -> World:
    """Polygon auction 41425733's shape: X priced 3,500x too high, and a CoW that buys X
    for one order and sells it for another, so its summed sides agree and only the
    orders show the wrong price; so do both orders' limits. A rival CoW fills the same
    two orders. The first auction is the shape of mainnet 14273947 and xdai 17840083:
    correct prices, and a balanced CoW that overpays one order 3x, its only fill, and
    charges the other, whose loose limit allows it and which a rival fills fairly. The
    overpaid order's limit is sane, so the imbalance is the bid's, not a price's, and
    the auction must stay in."""
    world = blank()
    x_buy = order(world, "71" * 56, USDC, TOKEN_X, ONE, ONE)
    x_sell = order(world, "72" * 56, TOKEN_X, DAI, ONE, ONE)
    over = order(world, "73" * 56, USDC, DAI, ONE, ONE)
    under = order(world, "74" * 56, DAI, USDC, 3 * ONE, ONE)
    a = solver(world, "Sector", "c7" * 20)
    b = solver(world, "Quasi", "d7" * 20)
    c = solver(world, "Baseline", "e7" * 20)
    wrong = dict(world.tokens)
    wrong[TOKEN_X] = 3_500 * ONE
    auction(world, 14273947, [
        (a, [fill(over, ONE, 3 * ONE), fill(under, 3 * ONE, ONE)]),
        (c, [fill(under, 3 * ONE, 3 * ONE + 10**16)]),
    ])
    auction(world, 41425733, [
        (a, [fill(x_buy, ONE, ONE + 10**16), fill(x_sell, ONE, ONE + 10**16)]),
        (b, [fill(x_buy, ONE, ONE + 5 * 10**15), fill(x_sell, ONE, ONE + 5 * 10**15)]),
    ], prices=wrong)
    return world


def runner_up_only() -> World:
    """Sector bids twice and never wins; both times its bid sets a winner's reference.
    Auction 14000011: Quasi wins X at 100 over Sector's 90 and Baseline's 50. Auction
    14000012: Quasi wins X at 100, Baseline wins Y at 80 over Sector's 70 and Horadrim's
    20. Without Sector the winners stay, Quasi's margin in the first rises from 10 to 50
    and Baseline's in the second from 10 to 60."""
    world = blank()
    weth = WRAPPED["sepolia"]
    x = order(world, "71" * 56, weth, USDC, 10**9, 10**9)
    y = order(world, "72" * 56, DAI, TOKEN_X, 10**9, 10**9)
    r = solver(world, "Sector", "a7" * 20)
    b = solver(world, "Quasi", "b7" * 20)
    c = solver(world, "Baseline", "c7" * 20)
    d = solver(world, "Horadrim", "d7" * 20)
    auction(world, 14000011, [
        (b, [fill(x, 10**9, 10**9 + 100)]),
        (r, [fill(x, 10**9, 10**9 + 90)]),
        (c, [fill(x, 10**9, 10**9 + 50)]),
    ])
    auction(world, 14000012, [
        (b, [fill(x, 10**9, 10**9 + 100)]),
        (c, [fill(y, 10**9, 10**9 + 80)]),
        (r, [fill(y, 10**9, 10**9 + 70)]),
        (d, [fill(y, 10**9, 10**9 + 20)]),
    ])
    return world


def runner_up_elsewhere() -> World:
    """Sector wins X; Quasi wins Y, and its reference holds Sector's winning 100.
    Auction 14000021: without Sector, Horadrim takes X at 60, and the winning score and
    Quasi's reference both fall by 40, so Quasi's margin stays 50: Sector sets a
    reference but presses nobody. Auction 14000022: Sector also bids Y at 70, losing to
    Quasi's 80 but filling Quasi's reference pick: runner-up on Y while it wins X, so
    Quasi's margin rises from 10 to 50."""
    world = blank()
    weth = WRAPPED["sepolia"]
    x = order(world, "81" * 56, weth, USDC, 10**9, 10**9)
    y = order(world, "82" * 56, DAI, TOKEN_X, 10**9, 10**9)
    r = solver(world, "Sector", "a8" * 20)
    b = solver(world, "Quasi", "b8" * 20)
    c = solver(world, "Baseline", "c8" * 20)
    d = solver(world, "Horadrim", "d8" * 20)
    for auction_id, sector_on_y in ((14000021, False), (14000022, True)):
        bids = [
            (r, [fill(x, 10**9, 10**9 + 100)]),
            (b, [fill(y, 10**9, 10**9 + 80)]),
            (d, [fill(x, 10**9, 10**9 + 60)]),
            (c, [fill(y, 10**9, 10**9 + 30)]),
        ]
        if sector_on_y:
            bids.append((r, [fill(y, 10**9, 10**9 + 70)]))
        auction(world, auction_id, bids)
    return world


def margin_falls() -> World:
    """Two ways removing a solver lowers another winner's margin, every bid fixed.

    Auction 14000031, un-filtering: Quasi's batch wins X and Y at 100 (50 + 50).
    Sector's single X bid at 40 sets the X baseline, which filters Arc's batch (X 30,
    Y 45). Quasi's reference is Sector's 40 plus Baseline's 20 on Y: margin 40. Without
    Sector, Arc's batch is kept and alone makes the reference, 75: margin 25.

    Auction 14000032, a winner's claim: Sector's batch wins X and Z at 50, Quasi's wins
    Y and W at 40. Arctic's batch on Z and Y (30) is kept out of Quasi's reference pick
    by Sector's claim on Z, so the reference is Sector's 50 plus Baseline's 5: margin 35.
    Without Sector, Horadrim takes X alone at 20, Arctic's batch joins Quasi's reference
    pick (20 + 30), and the margin falls to 10."""
    world = blank()
    weth = WRAPPED["sepolia"]
    x = order(world, "91" * 56, weth, USDC, 10**9, 10**9)
    y = order(world, "92" * 56, DAI, TOKEN_X, 10**9, 10**9)
    z = order(world, "93" * 56, TOKEN_X, USDC, 10**9, 10**9)
    w = order(world, "94" * 56, USDC, DAI, 10**9, 10**9)
    r = solver(world, "Sector", "a9" * 20)
    b = solver(world, "Quasi", "b9" * 20)
    c = solver(world, "Baseline", "c9" * 20)
    d = solver(world, "Horadrim", "d9" * 20)
    u = solver(world, "Arc", "e9" * 20)
    n = solver(world, "Arctic", "f9" * 20)
    auction(world, 14000031, [
        (b, [fill(x, 10**9, 10**9 + 50), fill(y, 10**9, 10**9 + 50)]),
        (r, [fill(x, 10**9, 10**9 + 40)]),
        (u, [fill(x, 10**9, 10**9 + 30), fill(y, 10**9, 10**9 + 45)]),
        (c, [fill(y, 10**9, 10**9 + 20)]),
    ])
    auction(world, 14000032, [
        (r, [fill(x, 10**9, 10**9 + 25), fill(z, 10**9, 10**9 + 25)]),
        (b, [fill(y, 10**9, 10**9 + 20), fill(w, 10**9, 10**9 + 20)]),
        (n, [fill(z, 10**9, 10**9 + 15), fill(y, 10**9, 10**9 + 15)]),
        (d, [fill(x, 10**9, 10**9 + 20)]),
        (c, [fill(y, 10**9, 10**9 + 5)]),
    ])
    return world


PINNED_WORLDS = {
    "live-fee-fixture": live_fee_fixture,
    "dark-pair": dark_pair,
    "ranked-order": ranked_order,
    "blocked-batch": blocked_batch,
    "penalty-caps": penalty_caps,
    "cancelled-price": cancelled_price,
    "runner-up-only": runner_up_only,
    "runner-up-elsewhere": runner_up_elsewhere,
    "margin-falls": margin_falls,
}
