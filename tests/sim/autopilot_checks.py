"""The quote side under its cross-check ranking, the rule from before 2026-09-02."""

from __future__ import annotations

from statistics import median

from loo import ch, quotes

from .harness import Sim
from .world import World


def check_autopilot(sim: Sim, world: World, auction_ids: list[int],
                    removals: dict[str, frozenset[str]]) -> None:
    """Buy-token prices are the window's median auction price; every win lands in
    exactly one leaf; the pick is validated against the same record."""
    window = quotes.load_quote_window(ch.Client.from_env(), sim.conn(), world.network,
                                      world.start, world.end, ranking="autopilot",
                                      auction_ids=auction_ids)
    ids = set(auction_ids)
    for token, price in window.buy_token_prices.items():
        seen = [a.prices[token] for a in world.auctions
                if a.auction_id in ids and token in a.prices]
        assert price == float(median(seen)) / 1e18
    v = window.validation
    assert v.ranking == "autopilot"
    assert v.reproduced + v.tie_mismatches + len(v.disagreements) + v.unpriced <= v.requests
    for label, addresses in removals.items():
        analysis = window.analyse(label, addresses)
        assert analysis.ranking == "autopilot"
        assert analysis.quote_wins == len(analysis.diffs) == (
            analysis.no_quote + analysis.class_flips + analysis.degraded + analysis.unpriced
            + analysis.nonpositive + analysis.price_suspect + analysis.implausible
            + analysis.unrecorded)
