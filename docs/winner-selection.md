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

### One asymmetry to preserve

Step 1 keys pairs on the **raw** `sell_token`/`buy_token`. Step 5 normalises them through
`as_erc20`, mapping the native-token sentinel `0xee…ee` to WETH. So a solution trading
ETH→USDC and one trading WETH→USDC are scored under separate pairs but conflict when
picking winners. This is in the Rust and should be mirrored, not tidied up.

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
