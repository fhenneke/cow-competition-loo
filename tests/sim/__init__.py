"""Deterministic simulation of the `loo` pipeline.

A seeded generator (`world.generate`) builds a synthetic chain window — auctions,
orders, solvers, bids, fee policies, settlements, caps and the quote competition — and
records the competition's outcome with an independent, deliberately naive transcription
of the mechanism (`oracle`). The real pipeline then reads that world through a simulated
analytics DB (`simdb`) and a simulated ClickHouse endpoint (`simch`), end to end through
`loo.cli.main`, and `checks` asserts what comes back. Every failure reproduces from its
seed: `LOO_SIM_SEED=<n> uv run pytest tests/test_simulation.py`.
"""
