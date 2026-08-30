"""Tests for the winner selection reimplementation.

Each test names the behaviour in `arbitrator.rs` it pins down.
"""

from __future__ import annotations

from loo.primitives import Pair
from loo.winner_selection import (
    Solution,
    arbitrate,
    compute_baseline_scores,
    compute_reference_outcomes,
    compute_reference_scores,
    filter_guess_could_matter,
    filter_proxied,
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
    pair_values: dict[Pair, int] | None = None,
    winner_pairs: frozenset[Pair] | None = None,
    pair_scores: dict[Pair, int] | None = None,
) -> Solution:
    values: dict[Pair, int] = (
        pair_values if pair_values is not None else {WETH_USDC: total}
    )
    return Solution(
        solver=solver,
        solution_uid=uid,
        total=total,
        pair_values=values,
        pair_scores=pair_scores or {},
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

    def test_the_baseline_is_the_whole_total_not_the_pair_surplus(self):
        """A single-pair solution puts its entire value on that pair, so in score mode the
        baseline is the recorded score — exact even where surplus is far below it, and
        needing no decomposition at all."""
        solutions = [
            solution("a", 0, 90, {WETH_USDC: 30}),
            solution("b", 1, 40, {WETH_USDC: 40}),
        ]
        assert compute_baseline_scores(solutions) == {WETH_USDC: 90}


class TestFairness:
    """With no `pair_scores`, surplus stands in for every pair — the pre-M14 behaviour,
    and still what surplus mode does."""

    def test_single_pair_solutions_are_exempt(self):
        """The exemption exists to stop reference scores collapsing to 0
        (`arbitrator.rs:88`)."""
        weak = solution("a", 0, 10, {WETH_USDC: 10})
        assert is_fair(weak, {WETH_USDC: 1000})

    def test_multi_pair_must_beat_every_baseline(self):
        baselines: dict[Pair, int] = {WETH_USDC: 50, DAI_USDC: 50}
        assert is_fair(solution("a", 0, 120, {WETH_USDC: 60, DAI_USDC: 60}), baselines)
        assert not is_fair(solution("b", 1, 100, {WETH_USDC: 60, DAI_USDC: 40}), baselines)

    def test_pair_without_a_baseline_passes(self):
        assert is_fair(solution("a", 0, 10, {WETH_USDC: 5, DAI_USDC: 5}), {WETH_USDC: 1})

    def test_equality_is_fair(self):
        assert is_fair(
            solution("a", 0, 100, {WETH_USDC: 50, DAI_USDC: 50}),
            {WETH_USDC: 50, DAI_USDC: 50},
        )


BASELINES: dict[Pair, int] = {WETH_USDC: 150, DAI_USDC: 150}


class TestFilterComparison:
    """One currency — the pair's score against the best single-pair solution's score —
    with surplus standing in as a *lower bound* where the score is unrecorded."""

    def test_a_known_score_is_compared_exactly(self):
        """Fees lift both sides, so a batch that clears a surplus-sized bar can still
        fail the score one — and it is the score comparison the protocol runs."""
        batch = solution(
            "a", 0, 260, {WETH_USDC: 60, DAI_USDC: 60},
            pair_scores={WETH_USDC: 160, DAI_USDC: 100},
        )
        assert not is_fair(batch, BASELINES)
        assert not filter_proxied(batch, BASELINES)

    def test_surplus_clearing_the_baseline_proves_fairness_without_the_fee(self):
        """Score is surplus plus a non-negative fee, so a surplus above the baseline puts
        the score above it too. No fee needed, and no approximation to report."""
        batch = solution("a", 0, 400, {WETH_USDC: 200, DAI_USDC: 200})
        assert is_fair(batch, BASELINES)
        assert not filter_proxied(batch, BASELINES)

    def test_surplus_below_the_baseline_is_the_only_guess(self):
        """Here the missing fee genuinely decides, and the filter says so."""
        batch = solution("a", 0, 400, {WETH_USDC: 60, DAI_USDC: 200})
        assert not is_fair(batch, BASELINES)
        assert filter_proxied(batch, BASELINES)

    def test_a_pair_with_no_baseline_is_never_a_guess(self):
        """It passes on any value, so falling back there decides nothing."""
        batch = solution(
            "a", 0, 400, {WETH_USDC: 200, DAI_USDC: 5}, pair_scores={WETH_USDC: 200}
        )
        assert is_fair(batch, {WETH_USDC: 150})
        assert not filter_proxied(batch, {WETH_USDC: 150})

    def test_one_dark_pair_does_not_blind_the_others(self):
        """The case auction 13492893 exposed. `DAI_USDC` has no score, but `WETH_USDC`
        does and fails its baseline, so the solution is unfair whatever the dark pair is
        worth."""
        batch = solution(
            "a", 0, 400, {WETH_USDC: 60, DAI_USDC: 200}, pair_scores={WETH_USDC: 100}
        )
        assert not is_fair(batch, BASELINES)

    def test_the_fallback_can_only_ever_be_stricter(self):
        """Surplus is a lower bound, so a dark pair can make the filter drop something the
        protocol kept — never keep something it dropped."""
        dark = solution("a", 0, 400, {WETH_USDC: 100, DAI_USDC: 200})
        lit = solution("a", 0, 400, {WETH_USDC: 100, DAI_USDC: 200},
                       pair_scores={WETH_USDC: 160, DAI_USDC: 240})
        assert not is_fair(dark, BASELINES)
        assert is_fair(lit, BASELINES)


class TestProxiedUids:
    def test_arbitrate_records_which_verdicts_rested_on_the_fallback(self):
        ranking = arbitrate([
            # Sets the WETH_USDC baseline at its whole total, 150.
            solution("a", 0, 150, {WETH_USDC: 30}),
            # Dark, and 60 does not clear 150: the missing fee decides.
            solution("b", 1, 500, {WETH_USDC: 60, DAI_USDC: 200}),
            # Dark too, but 200 clears 150 on surplus alone — proved fair, not guessed.
            solution("c", 2, 400, {WETH_USDC: 200, DAI_USDC: 200}),
        ])
        assert ranking.baselines == {WETH_USDC: 150}
        assert ranking.proxied_uids == {1}

    def test_single_pair_solutions_are_never_proxied(self):
        """They are exempt from the filter, so nothing was approximated for them."""
        ranking = arbitrate([solution("a", 0, 10, {WETH_USDC: 10})])
        assert ranking.proxied_uids == frozenset()

    def test_tested_excludes_solutions_dropped_before_the_filter(self):
        ranking = arbitrate([
            solution("a", 0, 100, {WETH_USDC: 100}),
            solution("b", 1, 0, {DAI_USDC: 0, WETH_USDC: 0}),
        ])
        assert [s.solution_uid for s in ranking.tested] == [0]
        assert ranking.dropped_uids == {1}


class TestFilterGuessCouldMatter:
    """The counterfactual's residual filter uncertainty: could a guessed verdict have
    moved the winners? `False` is a proof, so the tests pin both directions."""

    def test_no_guesses_means_nothing_to_worry_about(self):
        ranking = arbitrate([
            solution("a", 0, 150, {WETH_USDC: 30}),
            solution("b", 1, 400, {WETH_USDC: 200, DAI_USDC: 200}),
        ])
        assert ranking.proxied_uids == frozenset()
        assert not filter_guess_could_matter(ranking)

    def test_a_guess_that_would_win_if_reversed_matters(self):
        """uid 1 outscores the only winner and is filtered on a guess: had its dark pair
        cleared, it would have taken the auction."""
        ranking = arbitrate([
            solution("a", 0, 150, {WETH_USDC: 30}),
            solution("b", 1, 500, {WETH_USDC: 60, DAI_USDC: 200}),
        ])
        assert ranking.proxied_uids == {1}
        assert filter_guess_could_matter(ranking)

    def test_a_guess_that_loses_anyway_does_not(self):
        """Same guess, but a kept solution outscores it and claims both its pairs, so
        restoring it changes nothing."""
        ranking = arbitrate([
            solution("a", 0, 150, {WETH_USDC: 30}),
            solution("b", 1, 100, {WETH_USDC: 60, DAI_USDC: 200}),
            solution("c", 2, 900, {WETH_USDC: 400, DAI_USDC: 400}),
        ])
        assert ranking.proxied_uids == {1}
        assert not filter_guess_could_matter(ranking)

    def test_flipping_all_guesses_at_once_is_the_upper_bound(self):
        """`False` has to prove no *subset* of guesses could move the winners either.
        Restoring both here leaves the winner set alone, and so does restoring each one
        alone — each is blocked by a solution that wins in every scenario."""
        solutions = [
            solution("a", 0, 150, {WETH_USDC: 30}),
            solution("b", 1, 100, {WETH_USDC: 60, DAI_USDC: 200}),
            solution("c", 2, 90, {WETH_USDC: 60, DAI_USDC: 190}),
            solution("d", 3, 900, {WETH_USDC: 400, DAI_USDC: 400}),
        ]
        ranking = arbitrate(solutions)
        assert ranking.proxied_uids == {1, 2}
        assert not filter_guess_could_matter(ranking)
        for kept_back in ({1}, {2}):
            restored = [s for s in solutions if s.solution_uid in {0, 3} | kept_back]
            restored.sort(key=lambda s: (-s.total, s.solution_uid))
            winners = frozenset(
                restored[i].solution_uid for i in pick_winners(restored)
            )
            assert winners == ranking.winner_uids


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
