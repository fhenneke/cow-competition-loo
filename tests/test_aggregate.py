"""Tests for report aggregation and the comparison table.

The numbers in a comparison are only re-arrangements of `analyse --out` reports, so
what is pinned here is the arithmetic of that re-arrangement: totals must be re-derived
from the per-auction moves and rejected on mismatch, medians are over non-zero auctions
only, USD conversion is per auction with a window-median fallback, and a table can
never lead with anything but the `inherited` rule.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from loo.aggregate import (
    CAVEATS,
    REPORT_FORMAT,
    Table,
    comparison,
    cow_amount,
    distribution,
    eth,
    group_reports,
    load_report,
    pct,
    render_markdown,
    render_text,
    signed_eth,
    usd_amount,
    usd_context,
    usd_total,
)
from loo.primitives import usd_per_native

ONE = 10**18


def move(
    auction_id: int, surplus: int, rewards: int = 0, capped: str | None = "0"
) -> dict[str, Any]:
    return {
        "auction_id": auction_id,
        "delta_surplus": surplus,
        "delta_rewards": rewards,
        "delta_rewards_capped": capped,
    }


def payload(
    solver: str = "Fractal",
    rule: str = "inherited",
    changed: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A minimal `analyse --out` payload, internally consistent by default.

    Only the keys `load_report` consumes; the writer/reader contract over a full
    payload is pinned by the round-trip test below.
    """
    changed = changed if changed is not None else [move(1, ONE, 2 * ONE, str(ONE))]
    base: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "sign_convention": "counterfactual-minus-actual",
        "solver": solver,
        "network": "mainnet",
        "start": "2026-08-01",
        "end": "2026-08-04",
        "mode": "score",
        "outcome_rule": rule,
        "exclude_price_suspect": True,
        "price_suspect_auctions": [900, 901],
        "missing_data_auctions": [],
        "auctions": 100,
        "auctions_with_solver": 75,
        "auctions_solver_won_baseline": 10,
        "auctions_winner_set_changed": 9,
        "auctions_filter_relaxed": 2,
        "auctions_newly_kept_won": 2,
        "delta_surplus": sum(m["delta_surplus"] for m in changed),
        "delta_rewards": sum(m["delta_rewards"] for m in changed),
        "capped_estimate": True,
        "delta_rewards_capped": str(
            sum(
                Decimal(m["delta_rewards_capped"])
                for m in changed
                if m["delta_rewards_capped"] is not None
            )
        ),
        "auctions_capped_skipped": 0,
        "cow": {
            "cow_wei": str(10 * ONE),
            "cow_wei_capped": str(ONE),
            "converted_native": 0,
            "auctions_without_rate": 0,
            "native_without_rate": 0,
            "capped_without_rate": "0",
        },
        "orders_compared": 500,
        "orders_only_with_solver": 40,
        "orders_only_without_solver": 3,
        "changed": changed,
    }
    base.update(overrides)
    return base


def write_report(tmp_path: Path, name: str, data: dict[str, Any]) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


class TestLoadReport:
    def test_parses_totals_and_moves(self, tmp_path: Path):
        changed = [move(7, 3 * ONE, -ONE, "5"), move(8, -ONE, 0, None)]
        path = write_report(tmp_path, "r.json", payload(changed=changed))

        report = load_report(path)

        assert report.solver == "Fractal"
        assert report.outcome_rule == "inherited"
        assert report.delta_surplus == 2 * ONE
        assert report.delta_rewards == -ONE
        assert report.delta_rewards_capped == Decimal(5)
        assert report.price_suspects == 2
        assert report.analysed == 98
        assert [m.auction_id for m in report.moves] == [7, 8]
        assert report.moves[1].delta_rewards_capped is None

    def test_rejects_a_report_without_the_format_marker(self, tmp_path: Path):
        """A pre-format-2 file (the hand-written `*_wei` shape) must fail with
        "re-run analyse", not with a KeyError deep inside the comparison."""
        data = payload()
        del data["format"]
        with pytest.raises(ValueError, match="re-run analyse"):
            load_report(write_report(tmp_path, "old.json", data))

    def test_missing_data_is_counted_but_stays_out_of_analysed(self, tmp_path: Path):
        data = payload(missing_data_auctions=[11, 12, 13])

        report = load_report(write_report(tmp_path, "r.json", data))

        assert report.missing_data == 3
        # unlike price suspects these never entered `auctions`, so `analysed` is unmoved
        assert report.analysed == 98

    def test_surplus_mode_has_no_rewards(self, tmp_path: Path):
        data = payload(
            mode="surplus",
            capped_estimate=False,
            cow=None,
            changed=[move(1, ONE)],
        )
        report = load_report(write_report(tmp_path, "r.json", data))

        assert report.delta_rewards is None
        assert report.delta_rewards_capped is None
        assert report.delta_rewards_cow is None
        assert report.cow_auctions_without_rate == 0

    def test_suspects_kept_do_not_shrink_the_analysed_set(self, tmp_path: Path):
        data = payload(exclude_price_suspect=False)
        report = load_report(write_report(tmp_path, "r.json", data))
        assert report.analysed == report.auctions

    def test_capped_sums_survive_more_than_28_significant_digits(self, tmp_path: Path):
        # Real capped rewards carry ~40 significant digits; under Python's default
        # 28-digit Decimal context their sum rounds and the consistency check misfires
        # as "file corrupted". Regression for the context set at module import.
        changed = [
            move(1, 0, 0, "87307890745180353.8"),
            move(2, 0, 0, "0.0061377571809297225"),
        ]
        data = payload(
            changed=changed,
            delta_rewards_capped="87307890745180353.8061377571809297225",
        )
        report = load_report(write_report(tmp_path, "r.json", data))
        assert report.delta_rewards_capped == Decimal("87307890745180353.8061377571809297225")

    def test_rejects_totals_that_disagree_with_the_moves(self, tmp_path: Path):
        data = payload()
        data["delta_surplus"] += 1
        path = write_report(tmp_path, "r.json", data)

        with pytest.raises(ValueError, match="delta_surplus"):
            load_report(path)

    def test_rejects_a_report_from_the_old_sign_convention(self, tmp_path: Path):
        """Both sides of the totals check flip together when the convention changes,
        so only an explicit marker can catch a stale report — silently tabulating one
        would invert every sign it contributes."""
        data = payload()
        del data["sign_convention"]
        with pytest.raises(ValueError, match="sign convention"):
            load_report(write_report(tmp_path, "old.json", data))

    def test_rejects_capped_totals_that_disagree(self, tmp_path: Path):
        data = payload()
        data["delta_rewards_capped"] = "123456"
        path = write_report(tmp_path, "r.json", data)

        with pytest.raises(ValueError, match="delta_rewards_capped"):
            load_report(path)


class TestDistribution:
    def test_zero_deltas_are_not_auctions_that_moved(self):
        result = distribution({1: 5, 2: -1, 3: 0, 4: 96})

        assert result.total == 100
        assert result.n_nonzero == 3
        assert result.n_positive == 2
        assert result.n_negative == 1
        assert result.sum_positive == 101
        assert result.sum_negative == -1
        assert result.median_abs == 5
        assert result.largest == 96
        assert result.largest_auction_id == 4
        assert result.largest_share == pytest.approx(0.96)

    def test_empty(self):
        result = distribution({1: 0})
        assert result.n_nonzero == 0
        assert result.median_abs is None
        assert result.largest is None
        assert result.largest_share is None

    def test_offsetting_whale_share_can_exceed_one(self):
        # A whale nearly cancelled by the rest: the share says "one auction dominates",
        # not "one auction is most of a big number".
        result = distribution({1: 100, 2: -99})
        assert result.total == 1
        assert result.largest_share == pytest.approx(100.0)


class TestUsd:
    def test_rate_is_per_auction_with_median_fallback(self):
        context = usd_context({1: Decimal(2000), 2: Decimal(1000), 3: Decimal(3000)})

        assert context is not None
        assert context.fallback == Decimal(2000)
        assert context.rate(2) == Decimal(1000)
        assert context.rate(99) == Decimal(2000)

    def test_no_rates_means_no_usd(self):
        assert usd_context({}) is None

    def test_total_converts_each_auction_at_its_own_rate(self):
        context = usd_context({1: Decimal(2000), 2: Decimal(1000)})
        assert context is not None
        total = usd_total({1: ONE, 3: ONE}, context)
        # Auction 1 at its own 2000, auction 3 at the median fallback 1500.
        assert total == Decimal(3500)

    def test_usd_per_native_matches_the_hand_derivation(self):
        # 1 USDC (6 decimals) at price p is worth 1e6 * p / 1e18 wei, so USD/native
        # is 1e30 / p; checked against the measured mainnet value ~1915.
        price = 522217609822219700000000000
        rate = usd_per_native(price, 6)
        assert rate == Decimal(10) ** 30 / Decimal(price)
        assert 1900 < rate < 1930


class TestGrouping:
    def test_groups_rules_of_one_solver_window(self, tmp_path: Path):
        reports = [
            load_report(write_report(tmp_path, "a.json", payload(rule="inherited"))),
            load_report(
                write_report(tmp_path, "b.json", payload(rule="assume-settled"))
            ),
            load_report(
                write_report(tmp_path, "c.json", payload(solver="Sector"))
            ),
        ]

        windows = group_reports(reports)

        assert {w.solver for w in windows} == {"Fractal", "Sector"}
        fractal = next(w for w in windows if w.solver == "Fractal")
        assert set(fractal.by_rule) == {"inherited", "assume-settled"}
        assert fractal.headline.outcome_rule == "inherited"

    def test_headline_rule_is_required(self, tmp_path: Path):
        report = load_report(
            write_report(tmp_path, "a.json", payload(rule="assume-settled"))
        )
        with pytest.raises(ValueError, match="inherited"):
            group_reports([report])

    def test_duplicate_rule_is_rejected(self, tmp_path: Path):
        reports = [
            load_report(write_report(tmp_path, "a.json", payload())),
            load_report(write_report(tmp_path, "b.json", payload())),
        ]
        with pytest.raises(ValueError, match="two reports"):
            group_reports(reports)


class TestComparison:
    def build(self, tmp_path: Path, **kwargs: Any) -> Table:
        reports = [
            load_report(
                write_report(
                    tmp_path,
                    "fi.json",
                    payload(changed=[move(1, ONE, 2 * ONE, str(ONE)), move(2, -ONE // 2)]),
                )
            ),
            load_report(
                write_report(
                    tmp_path,
                    "fa.json",
                    payload(rule="assume-settled", changed=[move(1, 3 * ONE)]),
                )
            ),
            load_report(write_report(tmp_path, "si.json", payload(solver="Sector"))),
        ]
        return comparison(group_reports(reports), **kwargs)

    def test_columns_and_headline_rows(self, tmp_path: Path):
        table = self.build(tmp_path)

        assert set(table.columns) == {"Fractal", "Sector"}
        rows = dict(table.rows)
        fractal = list(table.columns).index("Fractal")
        sector = list(table.columns).index("Sector")
        assert rows["auctions analysed"][fractal] == "98 of 100 (2 price-suspect excluded)"
        assert rows["Δsurplus (inherited)"][fractal] == "+0.5000 ETH"
        assert rows["  assume-settled"][fractal] == "+3.0000 ETH"
        assert rows["  assume-settled"][sector] == "not run"
        assert rows["net change (Δsurplus − capped Δrewards)"][fractal] == "-0.5000 ETH"
        assert "over 2 auctions" in rows["  median non-zero auction"][fractal]
        assert "auction 1" in rows["  largest single auction"][fractal]
        assert rows["orders executed only with the solver"][fractal].startswith(
            "40 (8.0% of 500"
        )

    def test_missing_data_exclusions_widen_the_window_total(self, tmp_path: Path):
        reports = [
            load_report(
                write_report(
                    tmp_path, "a.json", payload(missing_data_auctions=[11, 12, 13])
                )
            )
        ]

        table = comparison(group_reports(reports))

        rows = dict(table.rows)
        assert rows["auctions analysed"][0] == (
            "98 of 103 (2 price-suspect, 3 missing-data excluded)"
        )

    def test_sign_split_keeps_direction_visible(self, tmp_path: Path):
        table = self.build(tmp_path)

        rows = dict(table.rows)
        fractal = list(table.columns).index("Fractal")
        assert rows["  auctions moved + / −"][fractal] == (
            "1 (+1.0000 ETH) / 1 (-0.5000 ETH)"
        )

    def test_usd_columns_convert_per_auction(self, tmp_path: Path):
        usd = usd_context({1: Decimal(2000), 2: Decimal(1000)})
        assert usd is not None
        table = self.build(tmp_path, usd_by_network={"mainnet": usd})

        rows = dict(table.rows)
        fractal = list(table.columns).index("Fractal")
        # +1 ETH at 2000 and −0.5 ETH at 1000 → $1,500.
        assert rows["Δsurplus (inherited)"][fractal] == "+0.5000 ETH ($1,500.00)"
        # Sector's single move is auction 1 at its own rate.
        sector = list(table.columns).index("Sector")
        assert rows["Δsurplus (inherited)"][sector] == "+1.0000 ETH ($2,000.00)"

    def test_kept_suspects_and_missing_rates_warn(self, tmp_path: Path):
        reports = [
            load_report(
                write_report(
                    tmp_path,
                    "a.json",
                    payload(exclude_price_suspect=False, changed=[move(5, ONE)]),
                )
            ),
        ]
        usd = usd_context({1: Decimal(2000)})
        assert usd is not None
        table = comparison(group_reports(reports), usd_by_network={"mainnet": usd})

        assert any("--include-price-suspects" in w for w in table.warnings)
        assert any("window-median rate" in w for w in table.warnings)

    def test_surplus_mode_has_no_reward_rows(self, tmp_path: Path):
        data = payload(
            mode="surplus",
            capped_estimate=False,
            cow=None,
            changed=[move(1, ONE)],
        )
        report = load_report(write_report(tmp_path, "s.json", data))
        table = comparison(group_reports([report]))

        rows = dict(table.rows)
        assert rows["Δrewards uncapped"][0] == "not computed (surplus mode)"
        assert rows["net change (Δsurplus − capped Δrewards)"][0] == (
            "n/a without a capped estimate"
        )

    def test_renderers_carry_every_row_warning_and_the_convention(self, tmp_path: Path):
        table = self.build(tmp_path)

        text = render_text(table)
        markdown = render_markdown(table)
        for label, _ in table.rows:
            assert label.strip() in text
            assert label.strip() in markdown
        assert markdown.splitlines()[0].startswith("| |")
        # The sign convention is part of the output, not something readers infer.
        assert "counterfactual minus actual" in text
        assert "counterfactual minus actual" in markdown

    def test_window_shown_only_when_windows_differ(self, tmp_path: Path):
        reports = [
            load_report(write_report(tmp_path, "a.json", payload())),
            load_report(
                write_report(
                    tmp_path, "b.json", payload(solver="Sector", start="2026-08-05")
                )
            ),
        ]
        table = comparison(group_reports(reports))
        assert any("2026-08-05" in c for c in table.columns)
        assert any("Fractal 2026-08-01" in c for c in table.columns)


class TestFormatters:
    def test_eth_rounds_half_away_from_zero_symmetrically(self):
        assert eth(1, 6) == "0.000000"
        assert eth(-1, 6) == "-0.000000"
        assert eth(int(1.5 * ONE), 4) == "1.5000"
        assert eth(Decimal("-1500000000000000000.9"), 4) == "-1.5000"
        # A 4-place quote agrees with the 6-place one — PLAN §6.1 quotes 0.263076
        # rounded to +0.2631, and a truncating display would print 0.2630.
        assert eth(263076 * 10**12, 4) == "0.2631"
        assert eth(-3078272 * 10**12, 4) == "-3.0783"

    def test_signed_eth_marks_positive(self):
        assert signed_eth(ONE) == "+1.0000"
        assert signed_eth(-ONE) == "-1.0000"
        assert signed_eth(0) == "0.0000"

    def test_usd_amount_gives_small_values_more_places(self):
        assert usd_amount(Decimal("1234.5")) == "$1,234.50"
        assert usd_amount(Decimal("-1234.5")) == "-$1,234.50"
        assert usd_amount(Decimal("0.0042")) == "$0.0042"
        assert usd_amount(Decimal(0)) == "$0.00"

    def test_pct_and_cow(self):
        assert pct(1, 8) == "12.5%"
        assert pct(1, 0) == "n/a"
        assert cow_amount(Decimal(3 * ONE) / 2) == "1.50"


class TestReportRoundTrip:
    """`run.write_report` and `load_report` are the two ends of one contract; a full
    payload written by the one must load through the other with nothing lost."""

    def test_written_report_loads_back(self, tmp_path: Path):
        from loo import run as run_module
        from loo.counterfactual import Analysis, AuctionCounterfactual, OrderDiff
        from loo.rewards import SolverReward

        result = AuctionCounterfactual(
            auction_id=7,
            n_solutions=3,
            solver_present=True,
            baseline_winner_uids=frozenset({0}),
            loo_winner_uids=frozenset({1}),
            order_diffs=(
                OrderDiff(
                    order_uid="ab",
                    contributes=True,
                    executed_base=True,
                    executed_loo=True,
                    surplus_base=100,
                    surplus_loo=40,
                ),
            ),
            baseline_rewards={
                "s1": SolverReward(
                    solver="s1",
                    competition_score=10,
                    observed_score=10,
                    reference_score=5,
                    uncapped_reward=5,
                    upper_cap=Decimal(3),
                    capped_reward=Decimal(3),
                )
            },
            block_deadline=123,
        )
        analysis = Analysis(
            solver="TestSolver", addresses=frozenset({"aa"}), capped_estimate=True
        )
        analysis.add(result)
        window = run_module.WindowAnalysis(
            network="mainnet",
            start="2026-08-01",
            end="2026-08-02",
            auction_ids=[7],
            missing_data=[],
            runs=[],
        )
        solver_run = run_module.SolverRun(
            solver="TestSolver",
            addresses=frozenset({"aa"}),
            matches=[],
            analysis=analysis,
            cow=run_module.CowConversion(
                Decimal(1), Decimal(2), 5, 0, 0, Decimal(0)
            ),
        )
        path = str(tmp_path / "roundtrip.json")

        run_module.write_report(path, window, solver_run)
        report = load_report(path)

        assert report.solver == "TestSolver"
        assert report.outcome_rule == "inherited"
        assert report.delta_surplus == -60
        assert report.delta_rewards == -5
        assert report.delta_rewards_capped == Decimal(-3)
        assert report.delta_rewards_cow == Decimal(1)
        assert report.delta_rewards_capped_cow == Decimal(2)
        assert [m.auction_id for m in report.moves] == [7]
        assert report.moves[0].delta_rewards_capped == Decimal(-3)


def test_caveats_cover_the_plan_list():
    """The plan names six caveats, plus the USD one and the missing-data one; a
    comparison must carry them all, so their disappearance should fail loudly here."""
    assert len(CAVEATS) == 8
    themes = (
        "behavioural", "Settlement", "proxy", "Quote", "reward", "Price",
        "neither order table", "USD",
    )
    for theme, caveat in zip(themes, CAVEATS, strict=True):
        assert theme.lower() in caveat.lower()
