"""The one-row-per-solver summary, derived a second time from the reports: the row order,
every cell in its unit, and what the CLI prints with and without USD."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from loo import aggregate
from loo.payouts import COMPONENTS

from . import oracle
from .harness import Sim

COLUMNS = ("won", "Δsurplus", "orders lost", "Δscore", "performance", "runner-up",
           "Δwinners' rewards", "consistency", "Δquote", "quote")


def share_text(deltas: Mapping[int, int | Decimal]) -> str:
    """The largest auction's part of the net total, none when nothing nets."""
    nonzero = [Decimal(d) for d in deltas.values() if d != 0]
    total = sum(nonzero, Decimal(0))
    if total == 0:
        return ""
    return f" ({float(max(abs(d) for d in nonzero) / abs(total)):.0%})"


def expected_rows(windows: Sequence[aggregate.SolverWindow],
                  rates: Mapping[int, Decimal]) -> list[tuple[str, tuple[str, ...]]]:
    """In USD when there are rates (one network per world), else native; most negative
    Δsurplus first, ties in report order."""
    fallback = median(rates.values()) if rates else Decimal(0)

    def value(total: int | Decimal, per_auction: Mapping[int, int | Decimal]) -> Decimal:
        if not rates:
            return Decimal(total)
        return sum((Decimal(v) * rates.get(a, fallback) for a, v in per_auction.items()),
                   Decimal(0)) / Decimal(oracle.ONE)

    def text(amount: Decimal, signed: bool) -> str:
        if not rates:
            return oracle.signed_eth_text(amount) if signed else oracle.eth_text(amount, 4)
        dollars = int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))
        sign = "-" if dollars < 0 else ("+" if signed and dollars > 0 else "")
        return f"{sign}${abs(dollars):,}"

    differ = len({w.headline.window for w in windows}) > 1
    keyed: list[tuple[Decimal, int, str, tuple[str, ...]]] = []
    for position, window in enumerate(windows):
        head = window.headline
        label = (f"{window.solver} {window.start}..{window.end} ({window.network}, "
                 f"{window.mode})" if differ else window.solver)
        surplus_moves = {m.auction_id: m.delta_surplus for m in head.moves}
        surplus = value(head.delta_surplus, surplus_moves)
        # Δscore's fee leg counts only auctions whose every fee is priced, as the fee row.
        score_moves = {m.auction_id: m.delta_surplus + m.delta_protocol_and_partner_fees
                       for m in head.moves}
        score = value(head.delta_surplus + head.delta_protocol_and_partner_fees, score_moves)
        analysed = head.analysed
        paid = head.paid
        assert paid is not None
        reward: dict[str, str] = {}
        for index, component in enumerate(COMPONENTS):
            missing = paid.unaccounted[component]
            if missing > 0 and missing >= paid.auctions:
                reward[component] = "pending"
                continue
            per = {a: parts[index] for a, parts in paid.per_auction.items()}
            amount = value(sum(per.values(), Decimal(0)), per)
            reward[component] = text(amount, False) + ("*" if missing else "")
        pressed = head.delta_pressed_rewards_capped
        if pressed is None:
            pressed_cell = "n/a"
        else:
            pressed_moves = {m.auction_id: m.delta_pressed_rewards_capped for m in head.moves
                             if m.delta_pressed_rewards_capped is not None}
            pressed_cell = text(value(pressed, pressed_moves), True)
        quote = head.quote
        if quote is None:
            quote_cell = "not run"
        else:
            # One amount for the whole window, at the window's median rate.
            native = quote.delta_volume_native
            quote_cell = text(native * fallback / Decimal(oracle.ONE) if rates else native, True)
        cells = [
            f"{head.auctions_solver_won / analysed:.1%}" if analysed else "n/a",
            text(surplus, True) + share_text(surplus_moves),
            f"{head.orders_only_with_solver:,}",
            text(score, True), reward["performance"],
            f"{head.auctions_runner_up:,}", pressed_cell, reward["consistency"],
            quote_cell, reward["quote"],
        ]
        keyed.append((surplus, position, label, tuple(cells)))
    keyed.sort(key=lambda k: (k[0], k[1]))
    return [(label, cells) for _, _, label, cells in keyed]


def check_table(table: aggregate.Table, windows: Sequence[aggregate.SolverWindow],
                rates: Mapping[int, Decimal]) -> None:
    assert table.columns == COLUMNS
    assert table.right_aligned
    assert list(table.rows) == expected_rows(windows, rates)
    unit = "USD" if rates else "ETH"
    assert f"Amounts in {unit}" in table.notes[0]
    shown = {label for label, _ in table.rows}
    for warning in table.warnings:
        # Only warnings about figures the summary shows: no cap orphans, COW or quotes.
        assert "cap orphans" not in warning and "COW" not in warning, warning
        assert any(warning.startswith(w.solver) for w in windows) or (
            warning.startswith("no USD rates")), (warning, shown)


def check_cli(sim: Sim, paths: Sequence[str], windows: Sequence[aggregate.SolverWindow],
              rates: Mapping[int, Decimal]) -> None:
    """`compare --summary`: native without a DB, USD per auction with one."""
    check_table(aggregate.summary(windows), windows, {})
    native = sim.cli("compare", *paths, "--summary", "--skip-usd")
    assert native.code == 0, native.err
    lines = native.out.splitlines()
    for label, cells in expected_rows(windows, {}):
        line = next(line for line in lines if line.startswith(label + " "))
        for cell in cells:
            assert cell in line, (cell, line)
    usd = sim.cli("compare", *paths, "--summary", "--markdown")
    assert usd.code == 0, usd.err
    assert "| --- |" + " ---: |" * len(COLUMNS) in usd.out
    for label, cells in expected_rows(windows, rates):
        assert "| " + label + " | " + " | ".join(cells) + " |" in usd.out
