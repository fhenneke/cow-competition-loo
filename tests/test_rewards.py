"""Tests for M3: uncapped rewards.

The transcription itself is validated against the DB row-by-row by `validate-rewards`;
what is pinned here is the formula's shape and its integration into the counterfactual —
in particular that the reward side consumes the *same* per-solution settlement decision
as the surplus side, so the two can never disagree about which winners delivered (D5).
"""

from __future__ import annotations

import pytest

from loo.counterfactual import Analysis, analyse_auction
from loo.extract import AuctionBundle, Bid, Settlement
from loo.rewards import (
    MissingReferenceScoreError,
    RewardValidation,
    SolverReward,
    Win,
    uncapped_rewards,
)
from loo.valuation import Order

ONE = 10**18
WETH = "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DAI = "6b175474e89094c44da98b954eedeac495271d0f"

SETTLED = Settlement(landed=True, in_time=True)
NOT_SETTLED = Settlement(landed=False, in_time=False)
LANDED_LATE = Settlement(landed=True, in_time=False)

A = (WETH, USDC)
B = (DAI, USDC)


def sell_order(uid: str, pair: tuple[str, str] = A, executed_buy: int = 2100) -> Order:
    return Order(
        uid=uid,
        sell_token=pair[0],
        buy_token=pair[1],
        sell_amount=1000,
        buy_amount=2000,
        executed_sell=1000,
        executed_buy=executed_buy,
        side="sell",
    )


def bid(
    uid: int,
    solver: str,
    score: int,
    orders: list[Order],
    *,
    is_winner: bool = False,
) -> Bid:
    return Bid(
        auction_id=1,
        uid=uid,
        solver=solver,
        score=score,
        is_winner=is_winner,
        filtered_out=False,
        orders=tuple(orders),
        contributes={order.uid: True for order in orders},
    )


def bundle(bids: list[Bid]) -> AuctionBundle:
    return AuctionBundle(
        auction_id=1,
        jit_owners=frozenset(),
        native_prices={USDC: ONE},
        bids=tuple(bids),
        reference_scores={},
    )


class TestUncappedRewards:
    def test_a_settled_sole_winner_earns_the_marginal_value(self):
        """With everything settled the formula reduces to
        `winning_score - min(winning_score, reference_score)`."""
        rewards = uncapped_rewards([Win("a", 100, True)], {"a": 60})
        assert rewards == {
            "a": SolverReward(
                solver="a",
                competition_score=100,
                observed_score=100,
                reference_score=60,
                uncapped_reward=40,
            )
        }

    def test_the_reference_score_is_clamped_to_the_winning_score(self):
        rewards = uncapped_rewards([Win("a", 100, True)], {"a": 150})
        assert rewards["a"].uncapped_reward == 0

    def test_a_failed_settlement_is_a_penalty(self):
        """`observed_score` drops to 0, so a sole winner's reward is
        `-reference_score` — the quantity the real payout clamps at -0.01 ETH,
        which is why stopping at uncapped rewards must be said out loud."""
        rewards = uncapped_rewards([Win("a", 100, False)], {"a": 60})
        assert rewards["a"].observed_score == 0
        assert rewards["a"].uncapped_reward == -60

    def test_two_winning_solvers_split_the_winning_score(self):
        rewards = uncapped_rewards(
            [Win("a", 100, True), Win("b", 80, True)], {"a": 80, "b": 100}
        )
        # winning_score is 180 for both; each earns it minus its own contribution's
        # replacement value.
        assert rewards["a"].uncapped_reward == 180 - 100 + 100 - 80
        assert rewards["b"].uncapped_reward == 180 - 80 + 80 - 100

    def test_one_solver_with_two_solutions_one_late(self):
        """Scores sum per solver; only the settled one reaches `observed_score`."""
        rewards = uncapped_rewards(
            [Win("a", 100, True), Win("a", 50, False), Win("b", 30, True)],
            {"a": 30, "b": 150},
        )
        assert rewards["a"] == SolverReward(
            solver="a",
            competition_score=150,
            observed_score=100,
            reference_score=30,
            uncapped_reward=180 - 150 + 100 - 30,
        )
        assert rewards["b"].uncapped_reward == 180 - 30 + 30 - 150

    def test_a_winner_without_a_reference_score_is_loud(self):
        """Substituting 0 would silently inflate the reward by the whole winning
        score, so it refuses instead."""
        with pytest.raises(MissingReferenceScoreError):
            uncapped_rewards([Win("a", 100, True)], {})

    def test_no_winners_no_rewards(self):
        assert uncapped_rewards([], {}) == {}


class TestRewardValidation:
    def reward(self, solver: str = "a", uncapped: int = 40) -> SolverReward:
        return SolverReward(
            solver=solver,
            competition_score=100,
            observed_score=100,
            reference_score=60,
            uncapped_reward=uncapped,
        )

    def test_matching_rows_meet_the_gate(self):
        validation = RewardValidation()
        validation.check_auction(1, {"a": self.reward()}, {"a": self.reward()})
        assert (validation.rows, validation.rows_matched) == (1, 1)
        assert validation.gate_met

    def test_a_disagreeing_row_names_its_fields(self):
        validation = RewardValidation()
        validation.check_auction(1, {"a": self.reward(uncapped=40)}, {"a": self.reward(uncapped=41)})
        (mismatch,) = validation.mismatches
        assert mismatch.differing_fields == ("uncapped_reward",)
        assert not validation.gate_met

    def test_a_row_only_one_side_has_is_a_mismatch(self):
        validation = RewardValidation()
        validation.check_auction(
            1,
            {"a": self.reward("a"), "b": self.reward("b")},
            {"a": self.reward("a")},
        )
        (mismatch,) = validation.mismatches
        assert (mismatch.solver, mismatch.theirs) == ("b", None)
        assert mismatch.differing_fields == ("missing",)

    def test_an_auction_absent_from_the_mart_is_coverage_not_a_bug(self):
        validation = RewardValidation()
        validation.check_auction(1, {"a": self.reward()}, {})
        assert validation.auctions_missing_from_fct == [1]
        assert not validation.mismatches
        assert not validation.gate_met

    def test_an_auction_without_winners_is_only_counted(self):
        validation = RewardValidation()
        validation.check_auction(1, {}, {})
        assert (validation.auctions, validation.auctions_with_winners) == (1, 0)
        assert validation.gate_met


class TestRewardsInCounterfactual:
    def test_the_removed_solver_drops_out_and_its_replacement_earns_instead(self):
        result = analyse_auction(
            bundle(
                [
                    bid(0, "x", 200, [sell_order("o1", executed_buy=2200)], is_winner=True),
                    bid(1, "b", 150, [sell_order("o1")]),
                ]
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
        )
        # Baseline: x's reference score is b's 150. LOO: b runs unopposed.
        assert result.baseline_rewards["x"].uncapped_reward == 200 - 150
        assert "x" not in result.loo_rewards
        assert result.loo_rewards["b"].uncapped_reward == 150
        assert result.loo_rewards["b"].reference_score == 0
        assert result.delta_rewards == 50 - 150

    def test_the_reward_side_sees_the_same_settlement_as_the_surplus_side(self):
        """D5 in one assertion: the batch landed late, so the surplus side carries the
        order as unexecuted and the reward side pays no `observed_score` — one decision,
        both sides."""
        result = analyse_auction(
            bundle(
                [
                    bid(0, "x", 200, [sell_order("o1", executed_buy=2200)], is_winner=True),
                    bid(1, "b", 150, [sell_order("o1")]),
                ]
            ),
            WETH,
            frozenset({"x"}),
            settled={0: LANDED_LATE},
        )
        (diff,) = [d for d in result.order_diffs if d.solver_base == "x"]
        assert diff.executed_base is False and diff.late_base is True
        assert result.baseline_rewards["x"].observed_score == 0
        assert result.baseline_rewards["x"].uncapped_reward == -150

    def test_a_replacement_inheriting_a_reverted_slot_earns_nothing(self):
        """Under `inherited` the replacement's `observed_score` is 0 like the batch it
        displaced; under `observed` it is credited with settling and earns its full
        marginal value. The reward gap between the rules is the same asymmetry M2
        measured on the surplus side."""
        auction = bundle(
            [
                bid(0, "x", 200, [sell_order("o1", executed_buy=2200)], is_winner=True),
                bid(1, "b", 150, [sell_order("o1")]),
            ]
        )
        inherited = analyse_auction(
            auction, WETH, frozenset({"x"}), settled={0: NOT_SETTLED}
        )
        assert inherited.baseline_rewards["x"].uncapped_reward == -150
        assert inherited.loo_rewards["b"].uncapped_reward == 0

        observed = analyse_auction(
            auction,
            WETH,
            frozenset({"x"}),
            outcome_rule="observed",
            settled={0: NOT_SETTLED},
        )
        assert observed.baseline_rewards["x"].uncapped_reward == -150
        assert observed.loo_rewards["b"].uncapped_reward == 150

    def test_a_moved_reference_score_moves_a_reward_without_moving_the_win(self):
        """The D8 case with money attached: x never wins, but its bid sets b's
        reference score, so removing x raises b's reward while the winner set stays
        put."""
        result = analyse_auction(
            bundle(
                [
                    bid(0, "b", 100, [sell_order("o1")], is_winner=True),
                    bid(1, "x", 80, [sell_order("o1", executed_buy=2080)]),
                ]
            ),
            WETH,
            frozenset({"x"}),
            settled={0: SETTLED},
        )
        assert not result.winner_set_changed
        assert result.reference_scores_moved
        assert result.baseline_rewards["b"].uncapped_reward == 100 - 80
        assert result.loo_rewards["b"].uncapped_reward == 100
        assert result.delta_rewards == 20 - 100

    def test_surplus_mode_computes_no_rewards(self):
        result = analyse_auction(
            bundle(
                [
                    bid(0, "x", 200, [sell_order("o1", executed_buy=2200)], is_winner=True),
                    bid(1, "b", 150, [sell_order("o1")]),
                ]
            ),
            WETH,
            frozenset({"x"}),
            mode="surplus",
            settled={0: SETTLED},
        )
        assert result.baseline_rewards == {} and result.loo_rewards == {}
        assert result.delta_rewards == 0

    def test_the_analysis_aggregates_rewards(self):
        analysis = Analysis(solver="x", addresses=frozenset({"x"}))
        analysis.add(
            analyse_auction(
                bundle(
                    [
                        bid(0, "x", 200, [sell_order("o1", executed_buy=2200)], is_winner=True),
                        bid(1, "b", 150, [sell_order("o1")]),
                    ]
                ),
                WETH,
                frozenset({"x"}),
                settled={0: NOT_SETTLED},
            )
        )
        assert analysis.rewards_base == -150
        assert analysis.rewards_loo == 0
        assert analysis.delta_rewards == -150
        assert analysis.removed_reward_base == -150
        assert analysis.auctions_rewards_moved == 1
        assert (analysis.negative_rewards_base, analysis.negative_reward_sum_base) == (
            1,
            -150,
        )
        assert (analysis.negative_rewards_loo, analysis.negative_reward_sum_loo) == (0, 0)
