"""`analyse_window` orchestration: one extraction serves every solver and rule.

The DB is faked by monkeypatching the `extract` functions `run` consumes, so what is
pinned here is the orchestration contract — how often the expensive loaders run and
what the per-(solver, rule) runs receive — not the arithmetic, which the
counterfactual and extract tests own.
"""

from typing import cast

import pytest

from loo import extract, run
from loo.db import Connection
from loo.extract import AuctionBundle, Bid, Settlement, SolverMatch
from loo.valuation import Order

CONN = cast(Connection, None)
ONE = 10**18
WETH = "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
SOLVER, RIVAL, GHOST = "aa" * 20, "bb" * 20, "cc" * 20

ADDRESSES = {
    "TestSolver": SOLVER,
    "Rival": RIVAL,
    # Resolves (it bid somewhere in the window) but appears in no stubbed bundle —
    # the shape `--limit` produces.
    "Ghost": GHOST,
    # The same competitor under a second catalogue name.
    "AliasOfTestSolver": SOLVER,
}


def order(executed_buy: int) -> Order:
    return Order(
        uid="ab" * 56,
        sell_token=WETH,
        buy_token=USDC,
        sell_amount=1000,
        buy_amount=2000,
        executed_sell=1000,
        executed_buy=executed_buy,
        side="sell",
    )


def bundle(auction_id: int) -> AuctionBundle:
    """The removed solver wins with the better fill; the rival is the replacement."""
    winner, runner_up = order(2200), order(2100)
    return AuctionBundle(
        auction_id=auction_id,
        jit_owners=frozenset(),
        native_prices={USDC: ONE},
        bids=(
            Bid(
                auction_id=auction_id,
                uid=0,
                solver=SOLVER,
                score=200,
                is_winner=True,
                filtered_out=False,
                orders=(winner,),
                contributes={winner.uid: True},
            ),
            Bid(
                auction_id=auction_id,
                uid=1,
                solver=RIVAL,
                score=100,
                is_winner=False,
                filtered_out=False,
                orders=(runner_up,),
                contributes={runner_up.uid: True},
            ),
        ),
        reference_scores={},
        block_deadline=7,
    )


class Calls:
    """How often each expensive loader ran."""

    def __init__(self) -> None:
        self.extractions = 0
        self.settlements = 0
        self.rates = 0


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> Calls:
    calls = Calls()

    def resolve_solver(conn: Connection, solver: str, start: str, end: str):
        address = ADDRESSES[solver]
        return frozenset({address}), [
            SolverMatch(
                address=address,
                name=solver,
                environment="prod",
                active=True,
                solutions=2,
                auctions_bid=2,
                winning_solutions=2,
            )
        ]

    def load_auctions(conn: Connection, ids: list[int], missing_data: list[int]):
        calls.extractions += 1
        for auction_id in ids:
            yield bundle(auction_id)

    def load_settlement_outcomes(conn: Connection, ids: list[int]):
        calls.settlements += 1
        return {a: {0: Settlement(landed=True, in_time=True)} for a in ids}

    def load_conversion_rates(conn: Connection, blocks: list[int]):
        calls.rates += 1
        return dict.fromkeys(blocks)

    def auctions_in_window(conn: Connection, start: str, end: str) -> list[int]:
        return [1, 2]

    def load_solution_caps(
        conn: Connection, ids: list[int]
    ) -> dict[int, dict[int, extract.SolutionCap]]:
        return {}

    monkeypatch.setattr(extract, "resolve_solver", resolve_solver)
    monkeypatch.setattr(extract, "auctions_in_window", auctions_in_window)
    monkeypatch.setattr(extract, "load_auctions", load_auctions)
    monkeypatch.setattr(extract, "load_settlement_outcomes", load_settlement_outcomes)
    monkeypatch.setattr(extract, "load_solution_caps", load_solution_caps)
    monkeypatch.setattr(extract, "load_conversion_rates", load_conversion_rates)
    return calls


class TestAnalyseWindow:
    def test_both_rules_share_one_extraction(self, stubbed: Calls):
        window = run.analyse_window(
            CONN,
            ["TestSolver"],
            "2026-07-01",
            "2026-08-01",
            outcome_rules=("inherited", "assume-settled"),
        )

        assert [r.analysis.outcome_rule for r in window.runs] == [
            "inherited",
            "assume-settled",
        ]
        assert stubbed.extractions == 1
        assert stubbed.settlements == 1
        assert stubbed.rates == 1  # one rate query for all runs together
        for solver_run in window.runs:
            assert solver_run.analysis.auctions == 2
            assert solver_run.analysis.auctions_with_solver == 2
            # The winner's fill was 100 atoms better in each auction.
            assert solver_run.analysis.delta_surplus == -200
            assert solver_run.cow is not None

    def test_single_rule_default_is_inherited(self, stubbed: Calls):
        window = run.analyse_window(CONN, ["TestSolver"], "2026-07-01", "2026-08-01")
        assert [r.analysis.outcome_rule for r in window.runs] == ["inherited"]

    def test_assume_settled_alone_skips_the_settlement_source(self, stubbed: Calls):
        run.analyse_window(
            CONN,
            ["TestSolver"],
            "2026-07-01",
            "2026-08-01",
            outcome_rules=("assume-settled",),
        )
        assert stubbed.settlements == 0

    def test_duplicate_rules_are_rejected(self, stubbed: Calls):
        with pytest.raises(ValueError, match="duplicate"):
            run.analyse_window(
                CONN,
                ["TestSolver"],
                "2026-07-01",
                "2026-08-01",
                outcome_rules=("inherited", "inherited"),
            )

    def test_no_rules_is_rejected(self, stubbed: Calls):
        with pytest.raises(ValueError, match="at least one"):
            run.analyse_window(
                CONN, ["TestSolver"], "2026-07-01", "2026-08-01", outcome_rules=()
            )


class TestRemovalGroups:
    """A comma-separated `--solver` entry removes its members together — one joint
    counterfactual, never a per-member attribution."""

    def test_a_group_removes_all_members_jointly(self, stubbed: Calls):
        window = run.analyse_window(
            CONN, ["TestSolver,Rival"], "2026-07-01", "2026-08-01"
        )
        (solver_run,) = window.runs
        assert solver_run.solver == "TestSolver,Rival"
        assert solver_run.addresses == frozenset({SOLVER, RIVAL})
        # With both bidders gone nothing executes: each auction loses the winner's
        # full 200 atoms of surplus, not the 100-atom downgrade to the rival.
        assert solver_run.analysis.delta_surplus == -400
        assert solver_run.analysis.addresses_seen == frozenset({SOLVER, RIVAL})
        assert run.absent_members(solver_run) == []

    def test_a_group_may_overlap_a_single_scenario(self, stubbed: Calls):
        """Removing A alone and {A,B} together are different scenarios of one window —
        exactly the comparison joint removal exists for — and they share the one
        extraction pass like any other pair of runs."""
        window = run.analyse_window(
            CONN, ["TestSolver", "TestSolver,Rival"], "2026-07-01", "2026-08-01"
        )
        assert [r.analysis.delta_surplus for r in window.runs] == [-200, -400]
        assert stubbed.extractions == 1

    def test_the_label_is_normalised(self, stubbed: Calls):
        window = run.analyse_window(
            CONN, ["TestSolver, Rival"], "2026-07-01", "2026-08-01"
        )
        assert window.runs[0].solver == "TestSolver,Rival"

    def test_a_competitor_named_twice_in_one_group_is_rejected(self, stubbed: Calls):
        with pytest.raises(extract.SolverResolutionError, match="once"):
            run.analyse_window(
                CONN, ["TestSolver,AliasOfTestSolver"], "2026-07-01", "2026-08-01"
            )

    def test_identical_scenarios_are_rejected(self, stubbed: Calls):
        with pytest.raises(extract.SolverResolutionError, match="same removal"):
            run.analyse_window(
                CONN,
                ["TestSolver,Rival", "Rival, TestSolver"],
                "2026-07-01",
                "2026-08-01",
            )

    def test_the_same_competitor_under_two_names_is_still_rejected(self, stubbed: Calls):
        with pytest.raises(extract.SolverResolutionError, match="same removal"):
            run.analyse_window(
                CONN, ["TestSolver", "AliasOfTestSolver"], "2026-07-01", "2026-08-01"
            )

    def test_an_empty_group_is_rejected(self, stubbed: Calls):
        with pytest.raises(extract.SolverResolutionError, match="names no solver"):
            run.analyse_window(CONN, [" , "], "2026-07-01", "2026-08-01")

    def test_a_member_absent_from_every_analysed_auction_is_reported(self, stubbed: Calls):
        """The group's numbers are then the smaller group's numbers wearing the full
        label — `absent_members` is what lets the CLI refuse that."""
        window = run.analyse_window(
            CONN, ["TestSolver,Ghost"], "2026-07-01", "2026-08-01"
        )
        (solver_run,) = window.runs
        assert run.absent_members(solver_run) == ["Ghost"]
        assert solver_run.analysis.delta_surplus == -200  # the single-solver result


class TestQuoteSide:
    """The quote pass is one load for every scenario and rule, and a network without
    quote data is refused by type rather than by a stray `KeyError`."""

    def test_a_network_without_quote_data_is_refused(self, stubbed: Calls):
        from loo import ch

        with pytest.raises(ch.NoQuoteDataError, match="xdai"):
            run.analyse_window(
                CONN,
                ["TestSolver"],
                "2026-07-01",
                "2026-08-01",
                network="xdai",
                with_quotes=True,
            )

    def test_the_quote_side_is_loaded_once_and_feeds_every_run(
        self, stubbed: Calls, monkeypatch: pytest.MonkeyPatch
    ):
        from loo import ch, quotes
        from loo.fees import Quote
        from loo.quotes import QuoteAnalysis, QuoteDiff, QuoteWindow

        uid = "ab" * 56
        loads: list[tuple[str, str, str, str, list[int]]] = []

        class FakeWindow:
            def analyse(self, label: str, addresses: frozenset[str]) -> QuoteAnalysis:
                analysis = QuoteAnalysis(
                    solver=label,
                    ranking="effective-sell",
                    window_orders=1,
                    window_volume_native=1,
                )
                analysis.add(
                    QuoteDiff(
                        order_uid=uid,
                        leaf="degraded",
                        is_sell=True,
                        tier="corroborated",
                        winner="w",
                        replacement="s",
                        effective_base=2050.0,
                        effective_loo=2020.0,
                        volume_native=1,
                        partially_fillable=False,
                        quote_loo=Quote(sell_amount=1000, buy_amount=2020, fee=0),
                    )
                )
                return analysis

        def load_quote_window(
            client: ch.Client,
            conn: Connection,
            network: str,
            start: str,
            end: str,
            *,
            ranking: str,
            auction_ids: list[int],
            log: object = None,
        ) -> QuoteWindow:
            loads.append((network, start, end, ranking, list(auction_ids)))
            return cast(QuoteWindow, FakeWindow())

        def from_env(cls: type[ch.Client]) -> ch.Client:
            return cast(ch.Client, object())

        monkeypatch.setattr(quotes, "load_quote_window", load_quote_window)
        monkeypatch.setattr(ch.Client, "from_env", classmethod(from_env))

        window = run.analyse_window(
            CONN,
            ["TestSolver"],
            "2026-07-01",
            "2026-08-01",
            outcome_rules=("inherited", "assume-settled"),
            with_quotes=True,
        )
        assert loads == [("mainnet", "2026-07-01", "2026-08-01", "effective-sell", [1, 2])]
        assert stubbed.extractions == 1
        for solver_run in window.runs:
            assert solver_run.quote is not None
            assert solver_run.quote.quote_wins == 1
            assert list(solver_run.quote_substitutions) == [uid]
            # The stubbed bundles carry no fee policies, so the replacement quote is
            # carried into both auctions and recharges nothing — the distinction the
            # report draws between "touched" and "recharged".
            assert solver_run.analysis.auctions_quote_checked == 2
            assert solver_run.analysis.quote_substituted_orders == 0
