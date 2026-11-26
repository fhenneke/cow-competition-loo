"""Seed selection for the simulation.

    uv run pytest                          # the default seeds plus every pinned one
    LOO_SIM_SEEDS=500 uv run pytest        # seeds 0..499 (plus the pinned ones)
    LOO_SIM_SEEDS=1000-1999 uv run pytest  # a range
    LOO_SIM_SEED=4242 uv run pytest        # replay exactly one seed

A failing seed is the whole reproduction: the world, the fault and the scenarios are all
derived from it. Once understood, add it to `PINNED` (with what it found) so it runs on
every default run from then on; a seed exposing a defect in `loo` that is not fixed here
goes in `XFAIL` instead, strict, so the fix shows up as an unexpected pass.
"""

from __future__ import annotations

import os

import pytest

DEFAULT_SEEDS = 120

PINNED: dict[int, str] = {
    1: "an auction with an unpriced fee carried a partial fee delta into the USD fee cells",
    6: "block times ending inside the window cut it short, and the run exited 0",
    13: "block times ending inside a paid week after the window overstated its reward share",
    80: "a replaced quote's limit shift would move the counterfactual winners",
    100: "a token both feeds overprice, sold for a sub-wei side the floor read as unpriced",
    107: "a replaced quote's limit shift would move the counterfactual winners",
    138: "a streamed order whose replacement depends on when its user committed",
    181: "a model whose last bid falls before the window's end, none after it: not refused",
    271: "our baseline differs from the recorded winners: a guessed filter verdict",
    291: "our baseline differs from the recorded winners: a guessed filter verdict",
    372: "a dropped stream's replacement answer was never recorded",
    481: "a dropped stream's replacement answer was never recorded",
}
"""Seeds that reach a rare region or once found something, with what. Run always."""

XFAIL: dict[tuple[str, int], str] = {}
"""(test name, seed) pairs that expose an unfixed defect in `loo`, with the defect."""


def seeds() -> list[int]:
    one = os.environ.get("LOO_SIM_SEED")
    if one:
        return [int(one)]
    spec = os.environ.get("LOO_SIM_SEEDS")
    if spec and "-" in spec:
        low, high = (int(x) for x in spec.split("-"))
        chosen = list(range(low, high + 1))
    else:
        chosen = list(range(int(spec) if spec else DEFAULT_SEEDS))
    return sorted(set(chosen) | set(PINNED) | {seed for _, seed in XFAIL})


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "seed" not in metafunc.fixturenames:
        return
    name = metafunc.function.__name__
    params = [
        pytest.param(s, marks=pytest.mark.xfail(reason=XFAIL[(name, s)], strict=True))
        if (name, s) in XFAIL
        else pytest.param(s)
        for s in seeds()
    ]
    metafunc.parametrize("seed", params, ids=[f"seed{s}" for s in seeds()])
