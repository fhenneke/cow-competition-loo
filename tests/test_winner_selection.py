"""Tests for the winner selection reimplementation.

Each test names the behaviour in `arbitrator.rs` it pins down.
"""

from __future__ import annotations

from loo.winner_selection import (
    Solution,
    arbitrate,
    compute_baseline_scores,
    compute_reference_outcomes,
    compute_reference_scores,
    is_fair,
    pick_winners,
)

WETH_USDC = ("weth", "usdc")
DAI_USDC = ("dai", "usdc")
USDC_WETH = ("usdc", "weth")


def solution(
    solver: str,
    uid: int,
    total: int,
    pair_values: dict | None = None,
    winner_pairs: frozenset | None = None,
) -> Solution:
    values = pair_values if pair_values is not None else {WETH_USDC: total}
    return Solution(
        solver=solver,
        solution_uid=uid,
        total=total,
        pair_values=values,
        order_uids=frozenset(),
        winner_pairs=winner_pairs if winner_pairs is not None else frozenset(values),
    )


class TestPickWinners:
    def test_disjoint_pairs_both_win(self):
        solutions = [
            solution("a", 0, 100, {WETH_USDC: 100}),
            solution("b", 1, 90, {DAI_USDC: 90}),
        ]
        assert pick_winners(solutions) == {0, 1}

    def test_conflicting_pair_loses_to_higher_total(self):
        solutions = [
            solution("a", 0, 100, {WETH_USDC: 100}),
            solution("b", 1, 90, {WETH_USDC: 90}),
        ]
        assert pick_winners(solutions) == {0}

    def test_direction_matters(self):
        """(sell, buy) is directed: WETH->USDC does not conflict with USDC->WETH."""
        solutions = [
            solution("a", 0, 100, {WETH_USDC: 100}),
            solution("b", 1, 90, {USDC_WETH: 90}),
        ]
        assert pick_winners(solutions) == {0, 1}

    def test_max_winners_is_a_hard_stop(self):
        solutions = [
            solution(f"s{i}", i, 100 - i, {(f"t{i}", "usdc"): 100 - i}) for i in range(5)
        ]
        assert pick_winners(solutions, max_winners=3) == {0, 1, 2}

    def test_winner_pairs_can_exceed_scored_pairs(self):
        """`pick_winners` claims pairs from *all* orders, including ones that do not
        contribute to score (`arbitrator.rs:587` vs `:184`)."""
        solutions = [
            solution("a", 0, 100, {WETH_USDC: 100}),
            solution(
                "b", 1, 90, {DAI_USDC: 90}, winner_pairs=frozenset({DAI_USDC, WETH_USDC})
            ),
        ]
        assert pick_winners(solutions) == {0}


class TestBaselines:
    def test_only_single_pair_solutions_are_candidates(self):
        solutions = [
            solution("a", 0, 100, {WETH_USDC: 60, DAI_USDC: 40}),
            solution("b", 1, 50, {WETH_USDC: 50}),
        ]
        assert compute_baseline_scores(solutions) == {WETH_USDC: 50}

    def test_takes_the_maximum(self):
        solutions = [
            solution("a", 0, 30, {WETH_USDC: 30}),
            solution("b", 1, 70, {WETH_USDC: 70}),
            solution("c", 2, 50, {WETH_USDC: 50}),
        ]
        assert compute_baseline_scores(solutions) == {WETH_USDC: 70}


class TestFairness:
    def test_single_pair_solutions_are_exempt(self):
        """The exemption exists to stop reference scores collapsing to 0
        (`arbitrator.rs:88`)."""
        weak = solution("a", 0, 10, {WETH_USDC: 10})
        assert is_fair(weak, {WETH_USDC: 1000})

    def test_multi_pair_must_beat_every_baseline(self):
        baselines = {WETH_USDC: 50, DAI_USDC: 50}
        assert is_fair(solution("a", 0, 120, {WETH_USDC: 60, DAI_USDC: 60}), baselines)
        assert not is_fair(solution("b", 1, 100, {WETH_USDC: 60, DAI_USDC: 40}), baselines)

    def test_pair_without_a_baseline_passes(self):
        assert is_fair(
            solution("a", 0, 10, {WETH_USDC: 5, DAI_USDC: 5}), {WETH_USDC: 1}
        )

    def test_equality_is_fair(self):
        assert is_fair(
            solution("a", 0, 100, {WETH_USDC: 50, DAI_USDC: 50}),
            {WETH_USDC: 50, DAI_USDC: 50},
        )


class TestArbitrate:
    def test_zero_total_solutions_are_dropped(self):
        ranking = arbitrate(
            [solution("a", 0, 100), solution("b", 1, 0, {DAI_USDC: 0})]
        )
        assert ranking.dropped_uids == frozenset({1})
        assert [s.solution_uid for s in ranking.ranked] == [0]

    def test_unfair_solution_is_filtered_not_ranked(self):
        ranking = arbitrate(
            [
                solution("a", 0, 100, {WETH_USDC: 100}),
                solution("b", 1, 90, {DAI_USDC: 90}),
                solution("c", 2, 150, {WETH_USDC: 90, DAI_USDC: 60}),
            ]
        )
        assert [s.solution_uid for s in ranking.filtered_out] == [2]
        assert ranking.winner_uids == frozenset({0, 1})

    def test_ties_break_on_input_order(self):
        """Feeding solutions in `uid` order reproduces the ordering the autopilot
        recorded; its own tie-break came from a shuffle and is not recoverable."""
        ranking = arbitrate(
            [
                solution("a", 0, 100, {WETH_USDC: 100}),
                solution("b", 1, 100, {WETH_USDC: 100}),
            ]
        )
        assert ranking.winner_uids == frozenset({0})

    def test_ranked_puts_winners_first_not_scores(self):
        """`arbitrate` finishes with `(Reverse(is_winner), Reverse(score))`, so a
        non-winner can outscore a winner that sorts ahead of it."""
        ranking = arbitrate(
            [
                solution("a", 0, 100, {WETH_USDC: 100}),
                solution("b", 1, 90, {WETH_USDC: 90}),
                solution("c", 2, 80, {DAI_USDC: 80}),
            ]
        )
        assert [s.solution_uid for s in ranking.ranked] == [0, 2, 1]
        assert [s.total for s in ranking.ranked] == [100, 80, 90]


class TestReferenceScores:
    def test_single_winner_falls_back_to_the_runner_up(self):
        ranking = arbitrate(
            [
                solution("a", 0, 100, {WETH_USDC: 100}),
                solution("b", 1, 90, {WETH_USDC: 90}),
            ]
        )
        assert compute_reference_scores(ranking) == {"a": 90}

    def test_all_of_a_solvers_solutions_are_removed(self):
        ranking = arbitrate(
            [
                solution("a", 0, 100, {WETH_USDC: 100}),
                solution("a", 1, 95, {WETH_USDC: 95}),
                solution("b", 2, 90, {WETH_USDC: 90}),
            ]
        )
        assert compute_reference_scores(ranking) == {"a": 90}

    def test_reference_score_is_zero_without_an_alternative(self):
        ranking = arbitrate([solution("a", 0, 100, {WETH_USDC: 100})])
        assert compute_reference_scores(ranking) == {"a": 0}

    def test_uses_ranked_order_not_score_order(self):
        """The load-bearing subtlety: `compute_reference_scores` re-picks winners on
        `ranking.ranked`, which is winner-first rather than score-descending. Solution
        `b` outscores `c` but sorts behind it, and that changes the answer.

        `b` is single-pair for scoring — so it is exempt from the fairness filter — but
        claims a second pair in `pick_winners` via a non-contributing order.
        """
        ranking = arbitrate(
            [
                solution("a", 0, 100, {WETH_USDC: 100}),
                solution(
                    "b",
                    1,
                    90,
                    {WETH_USDC: 90},
                    winner_pairs=frozenset({WETH_USDC, DAI_USDC}),
                ),
                solution("c", 2, 80, {DAI_USDC: 80}),
            ]
        )
        assert [s.solution_uid for s in ranking.ranked] == [0, 2, 1]
        assert ranking.winner_uids == frozenset({0, 2})

        # Walking `ranked`: c (80) is taken first and claims DAI->USDC, so b conflicts.
        # Had the list been re-sorted by score, b (90) would have won instead.
        assert compute_reference_scores(ranking) == {"a": 80, "c": 100}

    def test_max_winners_caps_the_number_of_reference_scores(self):
        solutions = [
            solution(f"s{i}", i, 100 - i, {(f"t{i}", "usdc"): 100 - i}) for i in range(5)
        ]
        ranking = arbitrate(solutions, max_winners=2)
        assert len(compute_reference_scores(ranking, max_winners=2)) == 2


class TestReferenceOutcomes:
    """`compute_reference_outcomes` carries the setters alongside each score — the
    solvers whose solutions supply it, i.e. whose removal moves it."""

    def test_names_the_solvers_supplying_a_reference_score(self):
        ranking = arbitrate(
            [
                solution("a", 0, 100, {WETH_USDC: 100}),
                solution("x", 1, 90, {WETH_USDC: 90}),
                solution("b", 2, 80, {WETH_USDC: 80}),
            ]
        )
        outcomes = compute_reference_outcomes(ranking)
        assert outcomes["a"].score == 90
        assert outcomes["a"].setters == frozenset({"x"})

    def test_a_solver_with_no_alternative_has_no_setters(self):
        ranking = arbitrate([solution("a", 0, 100, {WETH_USDC: 100})])
        outcomes = compute_reference_outcomes(ranking)
        assert outcomes["a"].score == 0
        assert outcomes["a"].setters == frozenset()

    def test_follows_ranked_order_like_the_scores(self):
        """`ranked` is winner-first, not score-descending, and `pick_winners` is
        order-dependent — the subtlety docs/winner-selection.md flags as load-bearing.

        `x` is single-pair for scoring, so the fairness filter exempts it, but it claims
        a second pair when winners are picked via an order that does not contribute to
        score.
        """
        ranking = arbitrate(
            [
                solution("a", 0, 100, {WETH_USDC: 100}),
                solution(
                    "x",
                    1,
                    90,
                    {WETH_USDC: 90},
                    winner_pairs=frozenset({WETH_USDC, DAI_USDC}),
                ),
                solution("c", 2, 80, {DAI_USDC: 80}),
            ]
        )
        assert [s.solution_uid for s in ranking.ranked] == [0, 2, 1]
        # Walking `ranked` without `a`: `c` takes DAI->USDC first, so `x` conflicts and
        # contributes nothing. `a`'s reference score comes from `c` alone.
        outcomes = compute_reference_outcomes(ranking)
        assert {s: o.setters for s, o in outcomes.items()} == {
            "a": frozenset({"c"}),
            "c": frozenset({"a"}),
        }

    def test_the_scores_are_the_outcomes_scores(self):
        ranking = arbitrate(
            [
                solution("a", 0, 100, {WETH_USDC: 100}),
                solution("b", 1, 90, {DAI_USDC: 90}),
                solution("c", 2, 80, {WETH_USDC: 80}),
            ]
        )
        outcomes = compute_reference_outcomes(ranking)
        assert compute_reference_scores(ranking) == {
            solver: o.score for solver, o in outcomes.items()
        }


def test_ranking_winning_score_sums_only_winners():
    ranking = arbitrate(
        [
            solution("a", 0, 100, {WETH_USDC: 100}),
            solution("b", 1, 90, {WETH_USDC: 90}),
            solution("c", 2, 80, {DAI_USDC: 80}),
        ]
    )
    assert ranking.winning_score == 180
