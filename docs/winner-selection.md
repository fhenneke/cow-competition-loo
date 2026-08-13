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
| end-to-end winner set vs. `is_winner` | 7740 / 7745 |
| end-to-end `filtered_out` | 7738 / 7745 |

The first two are the ones that matter: they hold the DB's own filter decisions fixed and
re-run only the step under test, so no approximation is anywhere in their path — every
quantity is either a recorded score or a token pair read off a trade. Both are exact.

Every end-to-end difference therefore traces to step 4, the fairness filter, which is the
one place the missing per-pair split enters.

### The filter runs on surplus

Scores are stored per solution, so the per-pair split step 4 compares is not recorded
anywhere. Something has to stand in for it. Two things narrow the problem first: the pair
*set* is exact regardless, since it comes from the trades and not from any value, and only
5.3% of solutions touch more than one pair (5,211 of 98,669) — single-pair solutions are
exempt from the filter outright.

**Both sides of the comparison use native user surplus**: a batch's surplus on a pair
against the best surplus any single-pair solution reached on that pair. Nothing is invented
and the units match. Solution *totals* remain recorded scores, so ranking, winner picking
and reference scores are unaffected — per-pair values feed the filter and nothing else, and
in score mode a solution's total deliberately exceeds the sum of its pair values by its
protocol fees.

Two alternatives were built and measured before being removed, since the result is worth
recording. Both tried to keep the comparison on the *score* scale: `scaled` split a batch's
score across its pairs in proportion to surplus and left single-pair baselines at full
score; `raw` put surplus on the left and full-score baselines on the right. Over the
three-day window:

| filter | decisions differing from the DB | auctions with a different winner set |
| --- | --- | --- |
| **surplus on both sides** | **7 / 5211 (0.13%)** | **5** |
| `scaled` | 196 / 5211 (3.8%) | 76 |
| `raw` | 200 / 5211 (3.8%) | 74 |

Surplus wins by more than an order of magnitude despite being the one option that does not
try to reconstruct the score split. The reason is that both sides move together: `scaled`
and `raw` compare a batch against a baseline inflated by that baseline solution's own
protocol fees, a systematic bias no split of the batch's score can correct for.

#### Bounding what the missing split can cost

The unknown split is not unconstrained. For a solution with recorded score `S` and per-pair
user surplus `u_i`, every valid per-pair score split satisfies

```
u_i  <=  v_i,     Σ v_i  =  S
```

since protocol fees are non-negative. The recorded filter kept the solution iff
`v_i >= baseline_i` on every pair, so a split it could have *kept* exists iff the
requirements fit inside the score: `Σ max(baseline_i, u_i) <= S`. Conversely a filtering
split always exists when some pair has `u_i < baseline_i` (set that pair's `v_i = u_i`).
Against the **exact score baselines** — the best recorded score among single-pair
solutions, which is what the protocol itself used — this decides most solutions outright
whatever the fees turn out to be:

| bracket | multi-pair solutions | meaning |
| --- | --- | --- |
| `must_filter` | 4,882 (94%) | no valid split reaches every baseline: unfair under all of them |
| `must_keep` | 66 (1.3%) | every pair's surplus alone clears its baseline: fair under all of them |
| `undetermined` | 263 (5.0%) | genuinely depends on the fee split — both outcomes are reachable |

(An earlier version tested each pair's interval `[u_i, S - Σ_{j≠i} u_j]` separately, which
cannot see two baselines that are individually reachable but do not fit *together*; it
decided only 79% and its `undetermined` band was 21%. The joint feasibility test is exact:
each verdict now proves what it claims.)

The bracket deliberately uses **score** baselines, not the surplus ones the filter itself
compares against, which is what makes it a statement about the recorded competition rather
than about our model. A decisive verdict therefore binds the DB absolutely, and binds us in
one direction only:

- Surplus baselines sit at or below score baselines, so keeping a solution the score filter
  drops (`must_filter`) is expected. `validate` reports it as `model`.
- The reverse is impossible. `must_keep` means every pair's surplus already clears its
  *score* baseline, which is at least its surplus baseline, so the surplus filter keeps it
  too. Dropping it anyway means our baselines or valuation are wrong: `bug`.

All 7 differences have a uniform shape: every one is the recorded filter dropping a batched
solution that the surplus filter keeps, and all 7 involve a partially fillable order, where
surplus and score diverge most. **For 2 of the 7 the bracket is decisive** (`must_filter`,
auctions 13488644 and 13490028): the recorded decision is forced, so keeping the solution
is provably the deliberate consequence of filtering on surplus — `model`, not an unknown.
The other 5 fall in the `undetermined` band, where the missing split genuinely decides:
`proxy`.

### `max_solutions_per_solver` is applied before `arbitrate`

The cap is enforced by the autopilot while collecting bids, not by the arbitrator
(`crates/autopilot/src/run_loop.rs:613-633`): `bids.retain(...)` counts solutions per
**driver name** and drops the fourth onward, then shuffles. `arbitrate` never sees them.

Two consequences for anything counterfactual. The recorded solutions are already truncated
per solver, so removing a solver **cannot** let a rival's fourth solution appear — the
counterfactual is over the set the arbitrator actually saw, and that is the honest scope.
And the cap keys on driver name while everything else keys on submission address, so a
solver running two drivers gets two allowances.

### A blocked batch keeps orders unexecuted

`pick_winners` takes a solution only when **every** pair it touches is unclaimed, so a
single-order winner can block a whole multi-order batch. The orders in the rest of that
batch then go unexecuted even when nobody else bid on them at all. Removing the blocker
lets the batch win and fills all of them.

This is not hypothetical. Auction 13488369:

| uid | solver | score | orders |
| --- | --- | --- | --- |
| 0 | Sector | 1.1543e15 | `091fcb6a` |
| 1 | Quasi | 1.1082e15 | `091fcb6a`, `4a9b4669` |
| 3 | Helixbox | 1.0187e15 | `091fcb6a`, `4a9b4669` |

Solution 0 wins on score and claims the pair, so **both** batches are blocked and order
`4a9b4669` goes unfilled — even though two separate solvers offered to fill it, and the
better of them scored only 4% less. Remove Sector and Quasi's batch wins, filling both
orders.

This is why a leave-one-out run finds orders that execute *only when a solver is removed* —
14 of them over three days for Fractal — and those are a real coverage effect, not a defect.

### A JIT leg can block another solution's execution — and the filter never sees it

The two asymmetries above combine into a mechanism-level quirk. A non-contributing JIT
order is invisible to the fairness filter (step 1 skips it) yet claims its directed pair
in `pick_winners` (step 5 iterates every order). A batch of [user order + the solver's
own JIT counterparty on the reverse direction] therefore passes the filter as a
single-pair solution while claiming **both** directions — and blocks any solution on the
reverse direction outright, without that leg ever having been compared to a baseline.

Measured over 2026-07-12..2026-08-12 on mainnet: 1,010 auctions have a winner claiming a
pair only via a JIT order; in 67 that claim causally changed the pick (verified by
re-running `arbitrate` with JIT-only claims stripped — a kept solution newly wins). 59 of
the 67 are benign: both solutions fill the *same* CoW-AMM order, so the block is
one-fill-per-pair working as intended. In **8**, the blocked solution filled a different
user order, and in all 8 that order was executed by no winner at all — a real coverage
loss to a JIT leg. Six of the eight blockers are one uncatalogued solver (`26a6fdc0…`)
running a JIT-counterparty strategy on USDC↔WETH.

Auction `13472415` is the sharpest case, a full chain: BRRRolver's batch matching user
orders in both WETH/USDC directions was filtered out — its baseline on USDC→WETH was set
by Baseline's single-pair solution (uid 22). Uncatalogued (uid 1) then won WETH→USDC
with a user order plus its own JIT counterparty on USDC→WETH, whose claim blocked uid 22
from winning. User order `8d5b1fc0…` went unfilled with two willing solutions in the
auction — one filtered, one blocked. Flagged to the backend team on 2026-08-13.

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
See PLAN.md §2 for how the two are separated.
