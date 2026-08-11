# Winner selection — algorithm reference

Reference context. The concrete work is in [../PLAN.md](../PLAN.md).

## Source of truth

Cloned locally at `~/Work/Code/services`. The algorithm lives in a standalone crate; the
autopilot only wraps it.

| What | Where |
| --- | --- |
| whole mechanism | `crates/winner-selection/src/arbitrator.rs:38` (`arbitrate`) |
| fairness filter | `arbitrator.rs:61` (`partition_unfair_solutions`) |
| baselines | `arbitrator.rs:650` (`compute_baseline_scores`) |
| per-token-pair scoring | `arbitrator.rs:176` (`score_by_token_pair`) |
| per-order score (CIP-38) | `arbitrator.rs:208` (`compute_order_score`) |
| protocol fees | `arbitrator.rs:260` (`protocol_fees`) |
| surplus | `arbitrator.rs:325` (`surplus_over_limit_price`), `:337` (`surplus_over`) |
| clearing prices | `arbitrator.rs:505` (`calculate_custom_prices_from_executed`) |
| winner picking | `arbitrator.rs:570` (`pick_winners`) |
| reference scores | `arbitrator.rs:607` (`compute_reference_scores`) |
| `price_in_eth`, `as_erc20` | `crates/winner-selection/src/primitives.rs:20`, `:9` |
| autopilot wrapper | `crates/autopilot/src/domain/competition/winner_selection.rs` |
| config defaults | `crates/configs/src/autopilot/run_loop.rs:10` |

Parameters: `max_winners = 20`, `max_solutions_per_solver = 3`.

## The algorithm

Given all bids for one auction:

1. **Value each solution per directed token pair.** For each order that
   `contributes_to_score`, add its value to `value[(sell_token, buy_token)]`. Solution
   total = sum over pairs.
2. Drop solutions with total 0. Sort descending by total.
3. **Baselines.** For each directed pair, `baseline[pair]` = the highest total among
   solutions that touch *exactly one* pair.
4. **Fairness filter.** Keep a solution if it touches exactly one pair, **or** if
   `value[pair] >= baseline[pair]` for every pair it touches. Otherwise it is
   `filtered_out`. (The one-pair exemption is deliberate — it avoids reference scores
   collapsing to 0; see `arbitrator.rs:88`.)
5. **Pick winners.** Walk the kept solutions in descending order; a solution wins if its
   directed token pairs are disjoint from every pair already claimed by a winner. Stop at
   `max_winners`.
6. **Reference score** of winner `s` = total of the winners produced by step 5 re-run on
   the ranked set with **all of `s`'s solutions** removed.

`contributes_to_score` (`crates/winner-selection/src/auction.rs:45`): the order is in the
auction, **or** it is a JIT order whose owner is in
`surplus_capturing_jit_order_owners`. In the autopilot the first test is "has an entry in
the fee-policy map", which contains every auction order. In the DB the analogue is
`competition_auctions.order_uids` — *not* the `fee_policies` table, which is only
populated for executed orders.

### Two asymmetries to preserve

Step 1 keys pairs on the **raw** `sell_token`/`buy_token`. Step 5 normalises them through
`as_erc20`, mapping the native-token sentinel `0xee…ee` to WETH. So a solution trading
ETH→USDC and one trading WETH→USDC are scored under separate pairs but conflict when
picking winners.

Step 1 also skips orders failing `contributes_to_score`, while step 5 claims pairs from
**every** order in the solution (`arbitrator.rs:587` iterates `solution.orders()` with no
filter). A solution can therefore be single-pair for scoring — and so exempt from the
fairness filter — while blocking two pairs when winners are picked.

Both are in the Rust and should be mirrored, not tidied up.

### `ranked` order is load-bearing

`arbitrate` returns after
`sort_by_key(|s| (Reverse(s.is_winner()), Reverse(s.score())))` (`arbitrator.rs:51`), so
`ranking.ranked` is **winners by descending score, then non-winners by descending score**
— not plain score order. A non-winner can outscore a winner that sorts ahead of it:
scores `[100, 90, 80]` where the 90 conflicts with the 100 and the 80 does not ranks as
`[100, 80, 90]`.

`compute_reference_scores` then re-runs `pick_winners` on *that* list
(`arbitrator.rs:624`), and `pick_winners` is order-dependent. Re-sorting by score before
computing reference scores gives different numbers whenever a non-winner conflicts with a
lower-scoring winner. The ordering must be carried through, not recovered.

Conveniently, the DB records it: `proposed_solutions.uid` is assigned in exactly this
order — see
[analytics-db.md](analytics-db.md#proposed_solutionsuid-encodes-the-ranking).

### Reproduction status

Reproduced against three days of mainnet (2026-08-01 to 2026-08-04, 7,745 auctions,
98,669 solutions), score mode, taking `proposed_solutions.score` as each solution's total:

| check | result |
| --- | --- |
| step 5 re-picked on the recorded kept set vs. `is_winner` | **7745 / 7745** |
| step 6 re-run on the recorded ranking vs. `reference_scores` | **7745 / 7745** |
| per-order surplus vs. the dbt model | 93941 / 94565, all 624 differences exactly −1 (the model's rounding, [above](analytics-db.md#order_surplus_atoms_in_surplus_token-rounds)) |
| solutions we could not value at all | **0** |
| end-to-end winner set vs. `is_winner` | 7669 / 7745 |
| end-to-end `filtered_out` | 7668 / 7745 |

The first two are the ones that matter: they hold the DB's own filter decisions fixed and
re-run only the step under test, so no approximation is anywhere in their path — every
quantity is either a recorded score or a token pair read off a trade. Both are exact.

Every end-to-end difference therefore traces to step 4, the fairness filter, which is the
one place the per-pair proxy enters.

### What the per-pair proxy costs

Scores are stored per solution, so the per-pair split step 4 compares is not available and
has to be approximated. The approximation is narrower than it first appears:

- **Baselines are exact.** Only solutions touching exactly one pair set a baseline
  (step 3), and for those the pair's value *is* the solution's recorded score. The
  right-hand side of `value[pair] >= baseline[pair]` is never approximated.
- **The pair *set* is exact** either way — it comes from the trades, not from any value.
- Only the left-hand side of multi-pair solutions is proxied, and only 5.3% of solutions
  are multi-pair (5,211 of 98,669).

That leaves a bounded question, and the bound is computable. For a solution with total
score `S` and per-pair user surplus `u_i`, the true per-pair score `v_i` satisfies

```
u_i  <=  v_i  <=  S - Σ_{j≠i} u_j
```

since protocol fees are non-negative and the `v_i` sum to `S`. Comparing that interval
against the exact baselines decides many solutions outright, whatever the fees turn out
to be. Over the three-day window:

| bracket | multi-pair solutions | meaning |
| --- | --- | --- |
| `must_filter` | 4,064 (78%) | unfair under every valid split |
| `must_keep` | 66 (1.3%) | fair under every valid split |
| `undetermined` | 1,081 (21%) | genuinely depends on the fee split |

**196 filter decisions differ from the DB, and all 196 fall in the `undetermined` band.**
Not one lands where the bracket forces an answer — which is what makes the proxy, rather
than a bug, the verified cause. As rates: 3.8% of multi-pair solutions, 0.2% of all
solutions, and 18% of the genuinely ambiguous cases. 76 auctions (1.0%) end up with a
different winner set as a result.

Two ways to split the score across a multi-pair solution's pairs were measured on the same
window, and they come out equivalent:

| proxy | filter decisions wrong | auctions with a different winner set |
| --- | --- | --- |
| score split in proportion to surplus | 196 / 5211 | 76 |
| raw user surplus (PLAN.md §2's original) | 200 / 5211 | 74 |

Proportional splitting is marginally better per solution and marginally worse per auction,
so there is no real reason to prefer either. The `exact` and `must_filter` cases dominate
whichever is used.

### Reference scores do not re-filter

`compute_reference_scores` re-runs only step 5, on the already-filtered set. It does not
recompute baselines. A true leave-one-out re-runs steps 3–5, which can *un-filter*
solutions that were unfair only because the removed solver set a high baseline. Keep both
variants: the cheap one is what reproduces `stg_backend_data__reference_scores`.

## Score vs. surplus

The autopilot's value is the CIP-38 score: `(user surplus + protocol fees)` in the surplus
token, converted to the buy token, then to native.

The **surplus token** is the buy token for sell orders and the sell token for buy orders.
Conversion to native (`compute_order_score`, `arbitrator.rs:208`):

```
sell order:  score_native = surplus * price[buy_token] / 1e18
buy  order:  surplus_in_buy = surplus * buy_amount / sell_amount   # order limit amounts
             score_native   = surplus_in_buy * price[buy_token] / 1e18
```

so buy orders need the order's limit amounts from `stg_backend_data__orders`.

`surplus_over` (`arbitrator.rs:337`) scales limit amounts by the executed amount to support
partial fills and uses **ceiling division** on the sell side. This is the expected source
of small discrepancies when reproducing results.

### Measured divergence — surplus is not a stand-in for score

`int_backend_data__proposed_solution_data.order_surplus_atoms_in_surplus_token` is **raw
user surplus, excluding protocol fees**. Verified over 17,640 single-order sell-side bids
(auctions 13505000–13509867), comparing `bid_score` against
`surplus * price[buy_token] / 1e18`:

| | ratio score / surplus_native |
| --- | --- |
| p10 | 1.01 |
| median | 1.08 |
| p90 | 2.70 |

and **3.2% of bids are fee-dominated** (surplus below 1% of score). Concrete case,
auction 13509006 solution 9: the order is filled at exactly its limit price plus one atom
— surplus of 1 atom — yet it scores 2.2e13, essentially all protocol fee.

Consequence: ranking by surplus and ranking by score are **not** interchangeable. A
surplus-only run is a self-consistent analysis of *user-facing value*, but it is not an
approximation of the real competition, and reward numbers derived from it would be wrong.
See PLAN.md §3 for how the two are separated.
