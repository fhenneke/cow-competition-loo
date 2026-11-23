# Rewards — formula reference

Reference context. The concrete work is in [../PLAN.md](../PLAN.md).

Transcribed from the dbt models at `~/Work/Code/cow-dagster`:

- `cow_dagster/cow_dbt/models/solver_accounting/marts/fct_solver_rewards_per_auction.sql`
- `cow_dagster/cow_dbt/models/solver_accounting/intermediary/backend_data/int_backend_data__solution_data.sql`
- `cow_dagster/cow_dbt/models/solver_accounting/intermediary/backend_data/int_backend_data__penalty_caps.sql`

## Per-auction, per-solver

All quantities are in native token, summed over that solver's **winning** solutions
(`int_backend_data__solution_data` contains winners only).

```
winning_score        = Σ over all winning solutions in the auction of score
competition_score(s) = Σ over s's winning solutions of score
observed_score(s)    = Σ over s's winning solutions of score where is_settled_in_time
                       # 0 if s won but failed to settle

uncapped_reward(s)   = winning_score
                       − competition_score(s)
                       + observed_score(s)
                       − min(winning_score, reference_score(s))
```

When the solver settles in time, `observed_score = competition_score` and this reduces to
`winning_score − min(winning_score, reference_score)` — the marginal value the solver added
to the auction. Verified exhaustively by `loo validate-rewards` over 2026-08-01..2026-08-04:
all 9,809 (auction, solver) rows built from the window's 10,301 winning solutions match
`fct_solver_rewards_per_auction.uncapped_reward` to the wei (`loo/rewards.py` is the
transcription). Uncapped rewards are routinely negative — a solver that won and failed to
settle is charged `−reference_score` uncapped, which only the lower cap turns into the
real penalty (a flat −0.01 ETH before 2026-09-08, the failed orders' CIP-87 caps since,
[below](#since-2026-09-08-the-lower-cap-is-per-order-cip-87)) — so uncapped sums must
never be read as payouts.

```
capped_reward(s) = 0                                   if auction is excluded
                   clamp(uncapped_reward(s), lower_reward_cap, upper_reward_cap)
```

## The caps

```
upper_reward_cap(s) = scaling_factor(block_deadline)
                      × Σ over s's winning solutions of
                        max(protocol_fee_native − partner_fee_native, 0)

lower_reward_cap(s) = 0                            inside a no_penalties_auctions window
                    = −Σ over s's winning solutions that did not settle in time
                        of penalty_cap_native        if every winning solution of s has one
                    = −reward_config.batch_reward_cap_lower
                                                     otherwise — every auction before 2026-09-08

penalty_cap_native(solution) = Σ over its user orders of the order's CIP-87 cap,
                               scaled to the executed fraction for partially fillable orders
                               (JIT orders have no cap); NULL when the solution has none
```

- `scaling_factor` comes from `protocol_fees_scaling_cap_config`, keyed by block time;
  **default 0.5**.
- `batch_reward_cap_lower` is 0.01 ETH on mainnet — see `dbt.reward_config` for other
  networks. Note the *static* `batch_reward_cap_upper` in that table (0.012 ETH) is **not**
  the cap actually applied; the applied cap is the fee-derived quantity above.
- `consistency_budget_contribution = upper_reward_cap − capped_reward` feeds
  `fct_consistency_rewards_per_solver_and_accounting_period`.

### Since 2026-09-08 the lower cap is per order (CIP-87)

The flat `−batch_reward_cap_lower` is now only the fallback. The backend records a
**per-order penalty cap** in `competition_auctions.penalty_caps_native`, an array parallel
to `order_uids` (services#4785, migration `V122`). Each cap is a fraction of the order's
volume, bounded by an absolute USD amount, with smaller factors for orders whose two tokens
fall in one "correlated" bucket (`crates/configs/src/autopilot/penalty_cap.rs`; the factors
and buckets are deployment config, not in the repo). On mainnet the arrays begin at auction
**13,763,364 (2026-09-08 08:46 UTC)**, and none is NULL after it.

Where the rule lives matters for anyone reading the models:

- It is computed in `fct_solver_rewards_per_auction`, **not** in
  `int_backend_data__solution_data.lower_reward_cap`, which still carries the flat value
  (or 0 in a no-penalties window) on every row. The per-solution input is
  `int_backend_data__solution_data.penalty_cap_native`.
- **NULL and 0 differ.** NULL means the solution has no cap and sends its solver back to
  the flat floor; 0 is a real cap that waives the penalty (fast-path auctions are written
  with caps of 0). cow-dagster#1058 (2026-10-16) separated the two; before it, the model
  coalesced NULL to 0 and sniffed "has caps" from `max(cap > 0)` per auction.
- The test is **per solver**: one of its solutions without a cap sends that solver to the
  flat floor, not the auction. A solver whose wins all settled has a floor of 0, which its
  non-negative reward never reaches.
- The mart's own `lower_reward_cap` column is NULL on rows built before the column existed
  (all of 2026-08-01..04, for instance), so `validate-rewards` compares the floor where the
  mart recorded one and otherwise checks it only through `batch_reward_native`.

The caps are small. Over 2026-10-12 on mainnet, 865 of the day's 5,443 winning solutions
missed the deadline, every one with a cap, and the median failed cap was **0.000069 ETH**.
Together they summed to 0.44 ETH, against the 8.65 ETH the flat floor would allow. So a
failed win now usually costs a small fraction of the flat 0.01 ETH.

`loo` follows the mart: `rewards.lower_reward_cap` is the rule above, fed by
`penalty_cap_native` per winning solution (`extract.REWARD_INPUTS_SQL`). `loo
validate-rewards` reproduces `batch_reward_native` and the mart's `lower_reward_cap` on
**every** row of 2026-10-12 (4,981 of 4,981), and still on every row of 2026-08-01..04
(9,809 of 9,809), measured 2026-10-22. The code before this change, which floored at the
flat value, failed 445 of the 2026-10-12 rows: −1.91 ETH of capped penalties against the
mart's −0.39 ETH, overstating one day's mainnet penalties by 1.52 ETH. Capped Δrewards
reported before 2026-10-22 for any window after 2026-09-08 carry that overstatement. The
four focus measures (surplus, fees, coverage, price) never read the cap.

## Why the cap is hard counterfactually

`upper_reward_cap` depends on the **realised protocol fees of the settled batch**. A
counterfactual winner that never settled has no realised fees, so they would have to be
estimated from its proposed trade executions plus fee policies, which are stored for
every order of every solution the arbitrator kept
([analytics-db.md](analytics-db.md#fee-policies-cover-every-order-of-every-kept-solution)).

Uncapped rewards need only scores and reference scores, both of which the winner-selection
code produces directly. Start there.

### The slot-inheritance estimate, and why the caps cannot be skipped

Measured over 2026-08-01..04, the caps are not a refinement: the window's 9,809 reward
rows sum to **−410.06 ETH uncapped and 0.7534 ETH actually paid**, with 54% of rows
clamped at the upper cap and 209 at the lower. So the counterfactual estimates capped
rewards rather than stopping at uncapped, by extending D4's slot rule to fees: a
replacement winner takes the `upper_reward_cap` of the recorded winner(s) whose token
pairs it claims, because realised fees follow the orders. This is self-consistent with
settlement inheritance for free — `int_backend_data__solution_data.upper_reward_cap`
is 0 for a batch that never settled (unrealised fees are no fees), so a replacement of
a reverted slot inherits cap 0 exactly where it inherits the revert.

The CIP-87 penalty cap is inherited by the same rule: a replacement takes the summed
`penalty_cap_native` of the slots it displaced, or none if any of them had none (so its
solver falls back to the flat floor, as in the mart). Under the `inherited` outcome rule a
replacement of a reverted slot therefore reverts **and** is charged that slot's penalty
cap — the same orders, the same charge. Unlike the upper cap, the penalty cap does not
need the batch to land: it is set per order before the auction runs, so a replacement's
*own* cap could in principle be computed from `competition_auctions.penalty_caps_native`
over its proposed executions. `loo` does not do that; inheriting keeps both caps and the
settlement on one slot rule.

Facts that make the estimate cheap, measured on the same window:

- every winning solution has a cap row — 10,301 of 10,301, no NULLs;
- the 16 late-landed winners have positive caps (their trades happened) but
  `is_settled_in_time = false`, consistent with earning nothing;
- `lower_reward_cap` (the flat value) and `is_excluded` are auction-level constants
  riding on every row;
- `penalty_cap_native` is NULL on every row of that window (pre-CIP-87). On 2026-10-12 it
  is NULL on none of 5,443.

One implementation trap: the caps come out of Postgres with ~40 significant digits and
Python's default `Decimal` context is 28 — per-solver sums silently round and the
exact comparison against `batch_reward_native` fails on every capped row. `loo.rewards`
sets the context to 78 digits (a full uint256).

## Native → COW

`dbt.int_accounting_period_data__conversion_rates` holds the per-accounting-period rate.
`reward_config.reward_token_address` gives the COW token per network.

Three facts about that table, measured on 2026-08-12:

- The grain is **one row per block** (`block_number`, unique key and indexed — filter on
  it, not on `block_time`), each carrying the `conversion_rate_cow_to_native` of the
  accounting period (Tuesday 00:00 UTC to Tuesday 00:00 UTC) the block's time falls in.
  Look rewards up at the auction's `block_deadline`.
- `conversion_rate_cow_to_native` is **double precision**, not `numeric` — go through
  `str()` when building a `Decimal`. `cow = native / rate`; the rate was ≈6.045e-05 for
  the period covering 2026-08-01..04, i.e. one period spans the whole M1/M2 window.
- The rate is snapshotted from Dune **after the period is paid out**, so recent blocks
  have a row with a NULL rate. Treat a missing rate as "not convertible yet", never as 0.

## Out of scope

Quote rewards. There is no good data on which solvers would have quoted had the removed
solver been absent, so they are excluded rather than estimated.
