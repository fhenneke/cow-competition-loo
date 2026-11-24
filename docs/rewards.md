# Rewards — formula reference

Transcribed from the dbt models in `~/Work/Code/cow-dagster`
(`cow_dbt/models/solver_accounting/`): `marts/fct_solver_rewards_per_auction.sql`,
`intermediary/backend_data/int_backend_data__solution_data.sql`,
`intermediary/backend_data/int_backend_data__penalty_caps.sql`. `loo/rewards.py` is the
transcription.

## Per-auction, per-solver

In native token, over that solver's **winning** solutions
(`int_backend_data__solution_data` holds winners only):

```
winning_score        = Σ over all winning solutions in the auction of score
competition_score(s) = Σ over s's winning solutions of score
observed_score(s)    = Σ over s's winning solutions of score where is_settled_in_time

uncapped_reward(s)   = winning_score − competition_score(s) + observed_score(s)
                       − min(winning_score, reference_score(s))

capped_reward(s)     = 0                                          if the auction is excluded
                       clamp(uncapped_reward(s), lower_reward_cap, upper_reward_cap)
```

A solver that settles in time earns `winning_score − min(winning_score, reference_score)`,
its marginal value to the auction. One that won and failed is charged `−reference_score`
uncapped, which only the lower cap turns into the real penalty — so uncapped sums are
never payouts. Over mainnet 2026-08-01..04 the 9,809 reward rows summed to **−410 ETH
uncapped and 0.75 ETH paid**, with 54% of rows at the upper cap.

`loo validate-rewards` recomputes uncapped and capped rewards from the mart's own inputs
and requires an exact match on every row: 4,981 of 4,981 on mainnet 2026-10-12 and 9,809
of 9,809 on 2026-08-01..04 (measured 2026-10-22). The reward side consumes the same
per-solution settlement decision as the surplus side
([`is_settled_in_time`](analytics-db.md#settled-and-settled_in_time-come-apart-and-which-one-you-want-depends)),
so the two never disagree about which winners delivered.

## The caps

```
upper_reward_cap(s) = scaling_factor(block_deadline)
                      × Σ over s's winning solutions of max(protocol_fee_native − partner_fee_native, 0)

lower_reward_cap(s) = 0                              inside a no_penalties_auctions window
                    = −Σ over s's winning solutions not settled in time of penalty_cap_native
                                                     if every winning solution of s has one
                    = −reward_config.batch_reward_cap_lower      otherwise

penalty_cap_native(solution) = Σ over its user orders of the order's CIP-87 cap, scaled to
                               the executed fraction for partially fillable orders;
                               NULL when the solution has none
```

- `scaling_factor` comes from `protocol_fees_scaling_cap_config` by block time; default 0.5.
- `batch_reward_cap_lower` is 0.01 ETH on mainnet (`dbt.reward_config` for others). The
  static `batch_reward_cap_upper` there (0.012 ETH) is **not** the applied upper cap.
- `upper_reward_cap` is 0 for a batch that never settled: unrealised fees are no fees.
- `consistency_budget_contribution = upper_reward_cap − capped_reward` feeds
  `fct_consistency_rewards_per_solver_and_accounting_period`.
- The caps come out of Postgres with ~40 significant digits; Python's default `Decimal`
  context (28) silently rounds per-solver sums, so `loo.rewards` uses 78 digits.

### Since 2026-09-08 the lower cap is per order (CIP-87)

The backend records a per-order penalty cap in `competition_auctions.penalty_caps_native`,
parallel to `order_uids` (services#4785): a fraction of the order's volume, bounded by an
absolute USD amount, smaller for correlated-token pairs (deployment config). On mainnet
the arrays start at auction 13,763,364 (2026-09-08 08:46 UTC) and none is NULL after it.

- The rule is applied in `fct_solver_rewards_per_auction`, **not** in
  `int_backend_data__solution_data.lower_reward_cap`, which still carries the flat value
  (or 0 in a no-penalties window). Always go through `rewards.lower_reward_cap`, fed by
  `penalty_cap_native` per winning solution (`extract.REWARD_INPUTS_SQL`).
- **NULL and 0 differ**: NULL means no cap and sends the solver to the flat floor; 0 is a
  real cap that waives the penalty (fast-path auctions are written with 0). Separated in
  cow-dagster#1058 (2026-10-16).
- The test is **per solver**: one of its solutions without a cap sends that solver, not
  the auction, to the flat floor.
- The mart's `lower_reward_cap` is NULL on rows built before the column existed, so
  `validate-rewards` checks the floor through `batch_reward_native` there.

The caps are small: on mainnet 2026-10-12, 865 of 5,443 winning solutions missed the
deadline, all with a cap, median **0.000069 ETH**, 0.44 ETH in total against the 8.65 ETH
the flat floor would allow. Flooring at the flat value overstated that day's penalties by
1.52 ETH.

## Why the cap is hard counterfactually

`upper_reward_cap` depends on the **realised fees of the settled batch**, and a
counterfactual winner never settled. Its fees could be estimated from its proposed
executions plus fee policies, which exist for every order of every kept solution
([rule](analytics-db.md#fee-policies-cover-every-order-of-every-kept-solution)). Uncapped
rewards need only scores and reference scores, which the winner-selection code produces
exactly.

### The slot-inheritance estimate, and why the caps cannot be skipped

Uncapped rewards alone are unusable as a payout answer (−410 ETH against 0.75 ETH), so
`loo` also estimates capped rewards. Settlement attaches to the slot: a replacement winner
inherits the recorded outcome of the token pairs it claims, so settlement cancels and the
result measures the competition's decision; attaching it to the solution instead (the
rejected `observed` rule) charges real reverts to the baseline while crediting every
replacement. The caps follow the same slot rule:

- A replacement inherits the `upper_reward_cap` of the recorded winner(s) whose pairs it
  claims, because realised fees follow the orders. A reverted slot's cap is 0 exactly
  where its settlement is a revert.
- It inherits their summed `penalty_cap_native`, or none if any had none (falling back to
  the flat floor, as in the mart). Under `inherited` a replacement of a reverted slot
  reverts **and** is charged that slot's penalty cap. Its own cap is computable from
  `penalty_caps_native` (the cap does not need the batch to land), but that would put the
  two caps on different rules.
- A winner with nothing to inherit drops its auction from the capped aggregate and is
  counted (`auctions_capped_skipped`), never guessed.

## Native → COW

`dbt.int_accounting_period_data__conversion_rates` carries `conversion_rate_cow_to_native`
per block, constant over each weekly accounting period (Tuesday 00:00 UTC to Tuesday);
`cow = native / rate`, and `reward_config.reward_token_address` names the token. Query it
by [period, not per block](analytics-db.md#int_accounting_period_data__conversion_rates-is-per-block-and-unindexed).

- Δrewards converts **per auction**, at the rate of its `block_deadline`; one window rate
  is wrong whenever a window straddles a period boundary.
- The rate is `double precision`: go through `str()` into `Decimal`.
- It is snapshotted from Dune **after** the period is paid out, so recent blocks carry
  NULL. An unconverted auction is reported, never treated as rate 0.

## Out of scope

Quote rewards ([quotes](quotes.md#out-of-scope-and-named-as-such)).
