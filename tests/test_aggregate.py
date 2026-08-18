"""Tests for report aggregation and the comparison table.

The numbers in a comparison are only re-arrangements of `analyse --out` reports, so
what is pinned here is the arithmetic of that re-arrangement: totals must be re-derived
from the per-auction moves and rejected on mismatch, medians are over non-zero auctions
only, USD conversion is per auction with a window-median fallback, and a table can
never lead with anything but the `inherited` rule.
"""

from __future__ import annotations

import itertools
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from loo.aggregate import (
    CAVEATS,
    REPORT_FORMAT,
    PriceImpact,
    Table,
    bps,
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

DEFAULT_VOLUME = 100 * ONE
"""Received-leg volume an executed side gets unless a test says otherwise, so the
default moved order is ONE of delta over 100 ETH — exactly 100 bps."""


_uids = itertools.count()


def diff(
    surplus_base: int | None,
    surplus_loo: int | None,
    *,
    contributes: bool = True,
    volume_base: int | None = None,
    volume_loo: int | None = None,
    partially_fillable: bool = False,
    order_uid: str | None = None,
) -> dict[str, Any]:
    """One serialized order diff; a `None` surplus is an unexecuted side, which gets
    no volume either. An executed side defaults to `DEFAULT_VOLUME`; pass an explicit
    volume (0 included) to override. The uid defaults to a fresh one — pass the same
    uid twice to model a partially fillable order executing in several auctions."""
    if volume_base is None and surplus_base is not None:
        volume_base = DEFAULT_VOLUME
    if volume_loo is None and surplus_loo is not None:
        volume_loo = DEFAULT_VOLUME
    return {
        "order_uid": order_uid if order_uid is not None else f"uid{next(_uids)}",
        "contributes": contributes,
        "executed_base": surplus_base is not None,
        "executed_loo": surplus_loo is not None,
        "surplus_base": surplus_base,
        "surplus_loo": surplus_loo,
        "volume_base": volume_base,
        "volume_loo": volume_loo,
        "partially_fillable": partially_fillable,
    }


def move(
    auction_id: int,
    surplus: int,
    rewards: int = 0,
    capped: str | None = "0",
    order_diffs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if order_diffs is None:
        # Executed on both sides by default: the delta is price movement, so the
        # coverage slice stays zero unless a test asks for an only-with diff.
        order_diffs = [diff(max(-surplus, 0), max(surplus, 0))] if surplus else []
    return {
        "auction_id": auction_id,
        "delta_surplus": surplus,
        "delta_rewards": rewards,
        "delta_rewards_capped": capped,
        "order_diffs": order_diffs,
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
        "orders_only_with_solver": sum(
            1
            for m in changed
            for d in m["order_diffs"]
            if d["contributes"] and d["executed_base"] and not d["executed_loo"]
        ),
        "orders_only_without_solver": 3,
        "window_volume_base": 10_000 * ONE,
        "window_orders_base": 50_000,
        "window_order_executions_base": 60_000,
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

    def test_derives_only_with_surplus_from_the_order_diffs(self, tmp_path: Path):
        changed = [
            # One order lost outright, one re-executed at a worse price, and a JIT
            # (non-contributing) order lost alongside — only the first is coverage.
            move(
                7,
                -3 * ONE,
                order_diffs=[
                    diff(2 * ONE, None),
                    diff(2 * ONE, ONE),
                    diff(5 * ONE, None, contributes=False),
                ],
            ),
            move(8, ONE),
        ]
        path = write_report(tmp_path, "r.json", payload(changed=changed))

        report = load_report(path)

        assert report.orders_only_with_solver == 1
        assert report.delta_surplus_only_with == -2 * ONE
        assert report.moves[0].delta_surplus_only_with == -2 * ONE
        assert report.moves[1].delta_surplus_only_with == 0

    def test_rejects_an_only_with_count_the_diffs_do_not_hold(self, tmp_path: Path):
        """`delta_surplus_only_with` is complete only if every only-with order lives in
        a changed auction; a count disagreement means order diffs are missing."""
        data = payload()
        data["orders_only_with_solver"] += 1
        path = write_report(tmp_path, "r.json", data)

        with pytest.raises(ValueError, match="orders_only_with_solver"):
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


class TestCoverageShare:
    """The window-level readings of the only-with count (D24). `coverage_share`
    counts per auction-order execution on both sides, partially fillable included;
    `coverage_share_fok` is the distinct-order reading, which exists only for
    fill-or-kill because the window holds no distinct count of partially fillable
    orders — one of them re-executing across auctions is one order, many
    executions."""

    def test_executions_over_the_window_executions(self, tmp_path: Path):
        changed = [
            # Two fill-or-kill only-with orders, a partially fillable one executing
            # in both auctions (two executions, one distinct order), and a
            # still-traded order that is not coverage at all.
            move(
                7,
                -3 * ONE,
                order_diffs=[
                    diff(ONE, None),
                    diff(ONE, None, partially_fillable=True, order_uid="pf"),
                    diff(ONE, ONE),
                ],
            ),
            move(
                8,
                -2 * ONE,
                order_diffs=[
                    diff(ONE, None),
                    diff(ONE, None, partially_fillable=True, order_uid="pf"),
                ],
            ),
        ]
        path = write_report(
            tmp_path,
            "r.json",
            payload(
                changed=changed,
                window_orders_base=400,
                window_order_executions_base=800,
            ),
        )

        report = load_report(path)

        assert report.orders_only_with_solver == 4
        assert report.coverage_share == Decimal(4) / Decimal(800)
        assert report.orders_only_with_fok == 2
        assert report.orders_only_with_partial == 1
        assert report.coverage_share_fok == Decimal(2) / Decimal(400)

    def test_a_window_that_traded_nothing_has_no_share(self, tmp_path: Path):
        path = write_report(
            tmp_path,
            "r.json",
            payload(
                changed=[], window_orders_base=0, window_order_executions_base=0
            ),
        )
        report = load_report(path)
        assert report.coverage_share is None
        assert report.coverage_share_fok is None


class TestPriceImpact:
    """The relative reading of the price-movement slice, derived at load time from
    the changed auctions' order diffs — fill-or-kill orders trading on both sides."""

    def load(self, tmp_path: Path, changed: list[dict[str, Any]]) -> PriceImpact:
        return load_report(
            write_report(tmp_path, "r.json", payload(changed=changed))
        ).price_impact

    def test_weighted_and_median_over_moved_orders(self, tmp_path: Path):
        impact = self.load(
            tmp_path,
            [
                move(
                    7,
                    -2 * ONE,
                    order_diffs=[
                        diff(2 * ONE, ONE, volume_base=10_000 * ONE),  # -1 bps
                        diff(2 * ONE, ONE, volume_base=2_000 * ONE),  # -5 bps
                    ],
                )
            ],
        )
        assert impact.orders_still_traded == 2
        assert impact.orders_moved == 2
        assert impact.delta_surplus == -2 * ONE
        assert impact.volume_base == 12_000 * ONE
        # Volume-weighted, so the big order pulls the average toward its -1 bps.
        assert impact.weighted_bps == Decimal(-2 * ONE) * 10_000 / Decimal(12_000 * ONE)
        assert impact.median_bps == Decimal(-3)

    def test_partially_fillable_orders_are_excluded_and_counted(self, tmp_path: Path):
        impact = self.load(
            tmp_path,
            [
                move(
                    7,
                    -2 * ONE,
                    order_diffs=[
                        diff(2 * ONE, ONE),
                        diff(2 * ONE, ONE, partially_fillable=True),
                    ],
                )
            ],
        )
        assert impact.orders_partially_fillable == 1
        assert impact.orders_still_traded == 1
        assert impact.orders_moved == 1
        assert impact.delta_surplus == -ONE

    def test_orders_not_trading_on_both_sides_stay_out(self, tmp_path: Path):
        """The coverage slice is an absolute story — losing a fill is not a worse
        price — and so is its mirror image."""
        impact = self.load(
            tmp_path,
            [move(7, -ONE, order_diffs=[diff(2 * ONE, None), diff(None, ONE)])],
        )
        assert impact.orders_still_traded == 0
        assert impact.weighted_bps is None
        assert impact.median_bps is None

    def test_a_zero_delta_still_traded_order_is_not_moved(self, tmp_path: Path):
        """Re-executed identically: counted as still trading, but it carries no price
        information and must not dilute the bps figures toward zero."""
        impact = self.load(tmp_path, [move(7, 0, order_diffs=[diff(ONE, ONE)])])
        assert impact.orders_still_traded == 1
        assert impact.orders_moved == 0
        assert impact.weighted_bps is None

    def test_a_moved_order_with_zero_volume_has_no_denominator(self, tmp_path: Path):
        """A dust fill whose native volume floors to 0 wei cannot be expressed in bps."""
        impact = self.load(
            tmp_path, [move(7, -ONE, order_diffs=[diff(2 * ONE, ONE, volume_base=0)])]
        )
        assert impact.orders_still_traded == 1
        assert impact.orders_moved == 0

    def test_non_contributing_orders_stay_out(self, tmp_path: Path):
        impact = self.load(
            tmp_path, [move(7, 0, order_diffs=[diff(2 * ONE, ONE, contributes=False)])]
        )
        assert impact.orders_still_traded == 0

    def test_overall_spreads_the_moved_delta_over_the_window_volume(
        self, tmp_path: Path
    ):
        """The unconditioned reading: unmoved and no-longer-trading orders count as
        zero price change, so the same Δsurplus lands on the whole window's volume."""
        changed = [move(7, -ONE, order_diffs=[diff(2 * ONE, ONE)])]
        report = load_report(
            write_report(tmp_path, "r.json", payload(changed=changed))
        )
        # -1 ETH over the fixture's 10,000 ETH window -> -1 bps exactly.
        assert report.overall_bps == Decimal(-1)

    def test_no_window_volume_means_no_overall_figure(self, tmp_path: Path):
        report = load_report(
            write_report(
                tmp_path, "r.json", payload(changed=[], window_volume_base=0)
            )
        )
        assert report.overall_bps is None


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
                    payload(
                        changed=[
                            move(1, ONE, 2 * ONE, str(ONE)),
                            # Auction 2's loss is an order that only trades with the
                            # solver, so it is coverage rather than a worse price.
                            move(2, -ONE // 2, order_diffs=[diff(ONE // 2, None)]),
                        ]
                    ),
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
            "1 (0.2% of 500"
        )
        assert rows["  Δsurplus from those orders"][fractal] == "-0.5000 ETH"
        assert rows["  Δsurplus from those orders"][sector] == "0.0000 ETH"

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
        # The coverage slice converts at its own auction's rate too.
        assert rows["  Δsurplus from those orders"][fractal] == "-0.5000 ETH (-$500.00)"
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

    def test_relative_price_rows(self, tmp_path: Path):
        """Fractal's one moved order: +ONE of delta over the default 100 ETH volume,
        so exactly +100 bps; the coverage order in auction 2 stays out."""
        table = self.build(tmp_path)

        rows = dict(table.rows)
        fractal = list(table.columns).index("Fractal")
        assert rows["relative price change, still-traded orders"][fractal] == (
            "+100.00 bps volume-weighted over 100.00 ETH (1 of 1 orders moved)"
        )
        assert rows["  median moved order"][fractal] == "+100.00 bps over 1 orders"
        # The same +1 ETH spread over the fixture's 10,000 ETH window.
        assert rows["  averaged over all traded volume"][fractal] == (
            "+1.000 bps of 10000.00 ETH traded (50,000 orders)"
        )

    def test_no_moved_order_reads_as_such(self, tmp_path: Path):
        changed = [
            move(
                2,
                -ONE // 2,
                order_diffs=[
                    diff(ONE // 2, None),
                    diff(ONE, ONE, partially_fillable=True),
                ],
            )
        ]
        report = load_report(write_report(tmp_path, "n.json", payload(changed=changed)))
        table = comparison(group_reports([report]))

        rows = dict(table.rows)
        assert rows["relative price change, still-traded orders"][0] == (
            "no still-traded order moved; 1 partially fillable excluded"
        )
        assert rows["  median moved order"][0] == "n/a"

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

    def test_bps_marks_positive_like_signed_eth(self):
        assert bps(Decimal(2)) == "+2.00 bps"
        assert bps(Decimal("-1.5")) == "-1.50 bps"
        assert bps(Decimal(0)) == "0.00 bps"


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
                    volume_base=600_000,
                    volume_loo=590_000,
                ),
                OrderDiff(
                    order_uid="cd",
                    contributes=True,
                    executed_base=True,
                    executed_loo=False,
                    surplus_base=40,
                    surplus_loo=None,
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
            executed_volume_base=600_000,
            executed_orders_base=1,
            executed_orders_all_base=2,
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
        assert report.delta_surplus == -100
        assert report.delta_rewards == -5
        assert report.delta_rewards_capped == Decimal(-3)
        assert report.delta_rewards_cow == Decimal(1)
        assert report.delta_rewards_capped_cow == Decimal(2)
        assert [m.auction_id for m in report.moves] == [7]
        assert report.moves[0].delta_rewards_capped == Decimal(-3)
        assert report.orders_only_with_solver == 1
        assert report.orders_only_with_fok == 1
        assert report.orders_only_with_partial == 0
        assert report.window_volume == 600_000
        assert (report.window_orders, report.window_executions) == (1, 2)
        assert report.coverage_share == Decimal(1) / Decimal(2)
        assert report.delta_surplus_only_with == -40
        assert report.moves[0].delta_surplus_only_with == -40
        # The relative reading survives the round trip: -60 over 600,000 is -1 bps.
        assert report.price_impact.orders_moved == 1
        assert report.price_impact.weighted_bps == Decimal(-1)
        assert report.price_impact.median_bps == Decimal(-1)


def test_caveats_cover_the_plan_list():
    """The plan names six caveats, plus the USD one, the missing-data one, the
    coverage-denominator one and the relative-price one; a comparison must carry them
    all, so their disappearance should fail loudly here."""
    assert len(CAVEATS) == 10
    themes = (
        "behavioural", "Settlement", "proxy", "Quote", "reward", "Price",
        "neither order table", "two denominators", "relative price", "USD",
    )
    for theme, caveat in zip(themes, CAVEATS, strict=True):
        assert theme.lower() in caveat.lower()
