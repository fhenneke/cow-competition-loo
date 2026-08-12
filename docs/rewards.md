# Rewards — formula reference

Reference context. The concrete work is in [../PLAN.md](../PLAN.md).

Transcribed from the dbt models at `~/Work/Code/cow-dagster`:

- `cow_dagster/cow_dbt/models/solver_accounting/marts/fct_solver_rewards_per_auction.sql`
- `cow_dagster/cow_dbt/models/solver_accounting/intermediary/backend_data/int_backend_data__solution_data.sql`

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
real −0.01 ETH penalty — so uncapped sums must never be read as payouts.

```
capped_reward(s) = 0                                   if auction is excluded
                   clamp(uncapped_reward(s), lower_reward_cap, upper_reward_cap)
```

## The caps

```
upper_reward_cap(s) = scaling_factor(block_deadline)
                      × Σ over s's winning solutions of
                        max(protocol_fee_native − partner_fee_native, 0)

lower_reward_cap    = −reward_config.batch_reward_cap_lower
                    = 0 inside a no_penalties_auctions window
```

- `scaling_factor` comes from `protocol_fees_scaling_cap_config`, keyed by block time;
  **default 0.5**.
- `batch_reward_cap_lower` is 0.01 ETH on mainnet — see `dbt.reward_config` for other
  networks. Note the *static* `batch_reward_cap_upper` in that table (0.012 ETH) is **not**
  the cap actually applied; the applied cap is the fee-derived quantity above.
- `consistency_budget_contribution = upper_reward_cap − capped_reward` feeds
  `fct_consistency_rewards_per_solver_and_accounting_period`.

## Why the cap is hard counterfactually

`upper_reward_cap` depends on the **realised protocol fees of the settled batch**. A
counterfactual winner that never settled has no realised fees, so they would have to be
estimated from its proposed trade executions plus fee policies — and fee policies are
stored only for executed orders (see [analytics-db.md](analytics-db.md)). The backfill
workaround is to take an order's policies from another auction where it was executed.

Uncapped rewards need only scores and reference scores, both of which the winner-selection
code produces directly. Start there.

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
