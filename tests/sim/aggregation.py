"""The window aggregate, recomputed from the per-auction results by a second, naive
reading of the rules `Analysis` documents: suspects out of everything, valuation
failures counted and out of everything else, the window denominators over every clean
auction, the rest over the auctions a removed solver bid in, fees only where both sides
are priced, and an auction retained only if something moved."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields
from decimal import Decimal
from typing import Any

from loo.counterfactual import Analysis, AuctionCounterfactual
from loo.rewards import SolverReward


def expected(
    results: Sequence[AuctionCounterfactual],
    addresses: frozenset[str],
    *,
    exclude_suspects: bool,
    capped_estimate: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {name: 0 for name in (
        "auctions", "auctions_with_solver", "auctions_skipped",
        "auctions_solver_won_baseline", "auctions_solver_won_db",
        "auctions_winner_set_changed", "auctions_filter_relaxed", "multi_pair_solutions",
        "multi_pair_filter_proxied", "auctions_filter_guess_could_matter",
        "auctions_newly_kept_won", "auctions_baseline_differs_from_db", "surplus_base",
        "surplus_loo", "protocol_and_partner_fees_base", "protocol_and_partner_fees_loo",
        "partner_fees_base", "partner_fees_loo", "auctions_fees_counted",
        "auctions_fees_incomplete", "window_volume_base", "window_orders_base",
        "window_order_executions_base", "rewards_base", "rewards_loo",
        "removed_reward_base", "auctions_rewards_moved", "negative_rewards_base",
        "negative_rewards_loo", "negative_reward_sum_base", "negative_reward_sum_loo",
        "auctions_capped", "auctions_capped_skipped", "cap_double_inherited", "cap_orphans",
        "auctions_quote_checked", "quote_substituted_orders", "auctions_quote_substituted",
        "auctions_quote_only", "quote_limit_unclearable_orders",
        "auctions_quote_shift_would_change_winners", "quote_fee_recharge",
        "quote_partner_fee_recharge", "orders_compared", "orders_only_with_solver",
        "orders_only_without_solver", "orders_unsettled_base", "orders_lost_to_lateness",
        "replacements_base", "replacements_loo", "inherited_reverts_loo", "orphans_base",
        "orphans_loo",
        "auctions_runner_up", "reference_margin_base", "reference_margin_loo",
        "reference_margin_rises", "reference_margin_falls",
        "auctions_reference_margin_moved", "reference_margin_volume")}
    out["rewards_base_capped"] = Decimal(0)
    out["rewards_loo_capped"] = Decimal(0)
    out["consistency_budget_base"] = Decimal(0)
    out["consistency_budget_loo"] = Decimal(0)
    out["margin_above_cap_base"] = Decimal(0)
    out["margin_above_cap_loo"] = Decimal(0)
    out["pressed_rewards_capped_base"] = Decimal(0)
    out["pressed_rewards_capped_loo"] = Decimal(0)
    out["settled_margin_base"] = 0
    out["settled_margin_loo"] = 0
    out["auctions_margin_cap_unknown"] = 0
    out["auctions"] = len(results)
    out["price_suspect_auctions"] = [r.auction_id for r in results if r.price_suspect]
    clean = [r for r in results if not (exclude_suspects and r.price_suspect)]
    failed = [r for r in clean if r.valuation_failures]
    out["auctions_skipped"] = len(failed)
    out["valuation_failures"] = [(r.auction_id, uid, e) for r in failed
                                 for uid, e in r.valuation_failures]
    out["auctions_solver_won_db"] = sum(r.solver_won_db for r in clean)
    ok = [r for r in clean if not r.valuation_failures]
    out["window_volume_base"] = sum(r.executed_volume_base for r in ok)
    out["window_orders_base"] = sum(r.executed_orders_base for r in ok)
    out["window_order_executions_base"] = sum(r.executed_orders_all_base for r in ok)
    present = [r for r in ok if r.solver_present]
    quote_only = [r for r in ok if not r.solver_present and r.quote_carried_uids]
    out["auctions_with_solver"] = len(present)
    out["addresses_seen"] = frozenset().union(*(r.removed_present for r in present))
    for r in present:
        out["multi_pair_solutions"] += r.multi_pair_solutions
        out["multi_pair_filter_proxied"] += r.multi_pair_filter_proxied
        out["auctions_filter_guess_could_matter"] += r.filter_guess_could_matter
        out["auctions_solver_won_baseline"] += r.solver_won_baseline
        out["auctions_winner_set_changed"] += r.baseline_winner_uids != r.loo_winner_uids
        out["auctions_filter_relaxed"] += bool(r.un_filtered_uids)
        out["auctions_newly_kept_won"] += bool(r.un_filtered_uids & r.loo_winner_uids)
        out["auctions_baseline_differs_from_db"] += not r.baseline_matches_db
        out["surplus_base"] += sum(d.surplus_base or 0 for d in r.order_diffs)
        out["surplus_loo"] += sum(d.surplus_loo or 0 for d in r.order_diffs)
        if all(d.fees_known_base and d.fees_known_loo for d in r.order_diffs):
            out["auctions_fees_counted"] += 1
            out["protocol_and_partner_fees_base"] += sum(d.fee_base or 0 for d in r.order_diffs)
            out["protocol_and_partner_fees_loo"] += sum(d.fee_loo or 0 for d in r.order_diffs)
            out["partner_fees_base"] += sum(d.partner_fee_base or 0 for d in r.order_diffs)
            out["partner_fees_loo"] += sum(d.partner_fee_loo or 0 for d in r.order_diffs)
            out["quote_fee_recharge"] += sum(r.quote_fee_recharges.values())
            out["quote_partner_fee_recharge"] += sum(r.quote_partner_fee_recharges.values())
        else:
            out["auctions_fees_incomplete"] += 1
        base = [w.uncapped_reward for w in r.baseline_rewards.values()]
        loo = [w.uncapped_reward for w in r.loo_rewards.values()]
        out["rewards_base"] += sum(base)
        out["rewards_loo"] += sum(loo)
        out["removed_reward_base"] += sum(
            w.uncapped_reward for s, w in r.baseline_rewards.items() if s in addresses)
        out["auctions_rewards_moved"] += r.baseline_rewards != r.loo_rewards
        out["negative_rewards_base"] += sum(1 for x in base if x < 0)
        out["negative_rewards_loo"] += sum(1 for x in loo if x < 0)
        out["negative_reward_sum_base"] += sum(x for x in base if x < 0)
        out["negative_reward_sum_loo"] += sum(x for x in loo if x < 0)
        if capped_estimate:
            sides = [
                [w.capped_reward for w in rewards.values()]
                for rewards in (r.baseline_rewards, r.loo_rewards)
            ]
            if any(x is None for side in sides for x in side):
                out["auctions_capped_skipped"] += 1
            else:
                out["auctions_capped"] += 1
                out["rewards_base_capped"] += sum((x for x in sides[0] if x is not None),
                                                  Decimal(0))
                out["rewards_loo_capped"] += sum((x for x in sides[1] if x is not None),
                                                 Decimal(0))
                # What the caps leave over: the weekly consistency budget's contributions.
                for key, rewards in (("consistency_budget_base", r.baseline_rewards),
                                     ("consistency_budget_loo", r.loo_rewards)):
                    for w in rewards.values():
                        assert w.upper_cap is not None and w.capped_reward is not None
                        out[key] += w.upper_cap - w.capped_reward
        out["cap_double_inherited"] += r.cap_double_inherited
        out["cap_orphans"] += len(r.cap_orphans_loo)
        out["replacements_base"] += len(r.replacements_base)
        out["replacements_loo"] += len(r.replacements_loo)
        out["inherited_reverts_loo"] += len(r.inherited_reverts_loo)
        out["orphans_base"] += len(r.orphans_base)
        out["orphans_loo"] += len(r.orphans_loo)
        out["auctions_runner_up"] += bool(r.runner_up_uids)
        margins = [(m, r.reference_margins_loo[s]) for s, m in r.reference_margins_base.items()]
        out["reference_margin_base"] += sum(m for m, _ in margins)
        out["reference_margin_loo"] += sum(m for _, m in margins)
        out["reference_margin_rises"] += sum(1 for b, lo in margins if lo > b)
        out["reference_margin_falls"] += sum(1 for b, lo in margins if lo < b)
        out["auctions_reference_margin_moved"] += any(b != lo for b, lo in margins)
        out["reference_margin_volume"] += r.pressed_volume_base
        if r.baseline_rewards or not r.reference_margins_base:
            # Score mode: each settled pressed winner's margin beyond its upper cap.
            above_base = _above_cap(r.reference_margins_base, r.baseline_rewards)
            above_loo = _above_cap(r.reference_margins_loo, r.loo_rewards)
            paid_base = _capped(r.reference_margins_base, r.baseline_rewards)
            paid_loo = _capped(r.reference_margins_loo, r.loo_rewards)
            if above_base is None or above_loo is None or paid_base is None or paid_loo is None:
                out["auctions_margin_cap_unknown"] += 1
            else:
                out["margin_above_cap_base"] += above_base
                out["margin_above_cap_loo"] += above_loo
                out["pressed_rewards_capped_base"] += paid_base
                out["pressed_rewards_capped_loo"] += paid_loo
                # Uncapped margin of the pressed winners whose wins all settled.
                for key, margins, rewards in (
                        ("settled_margin_base", r.reference_margins_base, r.baseline_rewards),
                        ("settled_margin_loo", r.reference_margins_loo, r.loo_rewards)):
                    out[key] += sum(m for s, m in margins.items()
                                    if rewards[s].observed_score == rewards[s].competition_score)
        for d in r.order_diffs:
            out["orders_compared"] += 1
            out["orders_only_with_solver"] += d.executed_base and not d.executed_loo
            out["orders_only_without_solver"] += d.executed_loo and not d.executed_base
            out["orders_unsettled_base"] += d.observed_base and not d.executed_base
            out["orders_lost_to_lateness"] += d.late_base
    for r in quote_only:
        out["auctions_quote_only"] += 1
        if r.quote_substituted_uids:
            recharged = [d for d in r.order_diffs if d.order_uid in r.quote_substituted_uids]
            if all(d.fees_known_base and d.fees_known_loo for d in recharged):
                out["quote_fee_recharge"] += sum(r.quote_fee_recharges.values())
                out["quote_partner_fee_recharge"] += sum(r.quote_partner_fee_recharges.values())
            else:
                out["auctions_fees_incomplete"] += 1
    checked = present + quote_only
    checked_ids = {id(r) for r in checked}
    out["auctions_quote_checked"] = sum(bool(r.quote_carried_uids) for r in checked)
    out["quote_substituted_orders"] = len(set[str]().union(*(r.quote_substituted_uids for r in checked)))
    out["auctions_quote_substituted"] = sum(bool(r.quote_substituted_uids) for r in checked)
    out["quote_limit_unclearable_orders"] = len(
        set[str]().union(*(r.quote_limit_unclearable_uids for r in checked)))
    out["auctions_quote_shift_would_change_winners"] = sum(
        r.quote_shift_winner_set_changed for r in checked)
    out["quote_shift_auctions"] = [r.auction_id for r in checked
                                   if r.quote_shift_winner_set_changed]

    def moved(r: AuctionCounterfactual) -> bool:
        return (r.baseline_winner_uids != r.loo_winner_uids or bool(r.un_filtered_uids)
                or r.baseline_reference_scores != r.loo_reference_scores
                or bool(r.quote_substituted_uids))

    out["changed"] = [r.auction_id for r in ok if id(r) in checked_ids and moved(r)]
    return out


def _above_cap(margins: Mapping[str, int], rewards: Mapping[str, SolverReward]) -> Decimal | None:
    total = Decimal(0)
    for solver, margin in margins.items():
        reward = rewards.get(solver)
        if reward is None or reward.upper_cap is None:
            return None
        if reward.observed_score == reward.competition_score:
            total += max(Decimal(0), margin - reward.upper_cap)
    return total


def _capped(margins: Mapping[str, int], rewards: Mapping[str, SolverReward]) -> Decimal | None:
    rows = [rewards.get(solver) for solver in margins]
    if any(r is None or r.capped_reward is None for r in rows):
        return None
    return sum((r.capped_reward for r in rows if r is not None and r.capped_reward is not None),
               Decimal(0))


def compare(analysis: Analysis, want: dict[str, Any]) -> None:
    for f in fields(analysis):
        if f.name not in want:
            continue
        got = getattr(analysis, f.name)
        if f.name == "changed":
            got = [r.auction_id for r in got]
        assert got == want[f.name], f.name
    assert analysis.delta_surplus == want["surplus_loo"] - want["surplus_base"]
    assert analysis.delta_rewards == want["rewards_loo"] - want["rewards_base"]
    assert analysis.delta_reference_margin == (
        want["reference_margin_loo"] - want["reference_margin_base"])
    assert analysis.delta_score == (
        want["surplus_loo"] - want["surplus_base"]
        + want["protocol_and_partner_fees_loo"] - want["protocol_and_partner_fees_base"])
    if analysis.mode == "score":
        assert analysis.delta_margin_above_cap == (
            want["margin_above_cap_loo"] - want["margin_above_cap_base"])
        assert analysis.delta_pressed_rewards_capped == (
            want["pressed_rewards_capped_loo"] - want["pressed_rewards_capped_base"])
        assert analysis.delta_settled_margin == (
            want["settled_margin_loo"] - want["settled_margin_base"])
    else:
        assert analysis.delta_margin_above_cap is None
        assert analysis.delta_pressed_rewards_capped is None
        assert analysis.delta_settled_margin is None
    assert analysis.delta_protocol_fees == (
        want["protocol_and_partner_fees_loo"] - want["partner_fees_loo"]
        - want["protocol_and_partner_fees_base"] + want["partner_fees_base"])
    assert analysis.delta_partner_fees == want["partner_fees_loo"] - want["partner_fees_base"]
    assert analysis.delta_protocol_and_partner_fees == (
        want["protocol_and_partner_fees_loo"] - want["protocol_and_partner_fees_base"])
    assert analysis.protocol_fees_base == (
        want["protocol_and_partner_fees_base"] - want["partner_fees_base"])
    assert analysis.delta_rewards_capped == want["rewards_loo_capped"] - want["rewards_base_capped"]
    assert analysis.delta_consistency_budget == (
        want["consistency_budget_loo"] - want["consistency_budget_base"])
    assert analysis.quote_fee_recharge_protocol == (
        want["quote_fee_recharge"] - want["quote_partner_fee_recharge"])
