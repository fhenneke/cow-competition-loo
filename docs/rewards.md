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

### The consistency budget offsets the capped delta

Each (auction, solver) row adds `consistency_budget_contribution = upper_reward_cap −
batch_reward_native` to its week's `total_consistency_budget`, and the week's consistency
rewards are that budget times shares summing to one, so it is paid out in full. Per week,
performance plus consistency payouts are Σ `upper_reward_cap`, and whatever the capped
estimate moves, the budget moves back:

```
Δconsistency budget = Σ_loo (upper cap − capped) − Σ_base (upper cap − capped)
Δcapped rewards + Δconsistency budget = Δ Σ upper caps
```

`loo` reports Δconsistency budget (estimate) per auction from the same inherited caps and
over the same `auctions_capped` as the capped delta, and their sum beside it. Checked
2026-11-25: the row identity holds on all 70,827 rows of mainnet 2026-11-10..17 and on
1,426,263 recent rows of mainnet, xdai, base, arbitrum-one, bnb, polygon, avalanche and
linea; none of those is excluded, and for an excluded auction it holds by the model's own
SQL (capped 0). The week's contributions sum to its `total_consistency_budget` to the
last digit (59.62 ETH), and its rewards add up to the same 59.62 ETH.

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

## Rewards paid to a solver (recorded)

`analyse` also reports what each scenario's addresses were paid over the window, and
`compare` shows it below the net row (`loo/payouts.py`, loaded once per window for every
scenario):

| component | mart | window part |
| --- | --- | --- |
| performance | `fct_solver_rewards_per_auction.batch_reward_native` | Σ over the window's auctions |
| consistency | `fct_consistency_rewards_per_solver_and_accounting_period.consistency_reward_native` | pro rata, below |
| quote (CIP-88) | `fct_quote_rewards_per_solver_and_accounting_period.quote_reward_native` | pro rata, below |

Both weekly rewards are linear in a per-auction metric, with every other factor fixed per
period (cow-dagster `02cb3ec`, 2026-11-24):

```
consistency(p, s) = budget(p) × T(p, s)·success(p, s) / Σ_s' T(p, s')·success(p, s')
                    T(p, s) = Σ_{a ∈ p} relative_surplus(a, s)
quote(p, s)       = quote_budget(p) × Σ_{a ∈ p} share(a, s) / N(p)
```

with `relative_surplus` from `int_backend_data__solver_relative_surplus_on_executed_orders`,
`share` the `quote_reward_share` of `int_quote_data__quote_reward_shares`, and `N(p)` the
period's orders with a positive share. The window's part is therefore exact:

```
reward(p, s) × Σ_{a ∈ window ∩ p} metric(a, s) / Σ_{a ∈ p} metric(a, s)
```

Each auction carries its term of that sum, rounded to the wei, so `compare` converts it
at the auction's own USD rate.

- Periods run Tuesday 00:00 UTC to Tuesday, and an auction belongs to the period of its
  `block_deadline` — the criterion of `extract.WINDOW_SQL`, which for 2026-11-10..17
  returned the marts' auction set exactly (39,110 auctions). Auction ids rise with the
  deadline, so a period's id range holds exactly its auctions: none of the week's 70,827
  reward rows or 382,748 (auction, solver) relative-surplus groups in its id range
  belonged to another week.
- Both sums come from the int table, so the split stays defined when an int table is
  rebuilt after its mart (cow-dagster#1364 excluded a wallet from quote rewards for the
  2026-11-17..24 payout). The int table's full-period sum is still reconciled with the
  mart's own total — Σ relative surplus against `total_relative_surplus`, budget × Σ share
  / N against `quote_reward_native` — at a relative 1e-9, and a disagreement is listed in
  the report and warned about in `compare`.
- A period missing from a weekly mart is not paid out yet; an auction with bids and no
  `fct_solver_rewards_per_auction` row is not accounted yet (none in 2026-11-10..17).
  Their auctions are counted per component and the row says `incomplete`; they are never
  counted as zero. On 2026-11-25 both weekly marts ended at 2026-11-10..17, and
  `int_backend_data__solver_relative_surplus_on_executed_orders` at that week's last
  auction (14,262,308): it is built per paid week.
- Every auction of the window counts, fast-path and price-suspect ones included: payouts
  of record do not depend on `loo`'s valuation.
- Gross of the solver service fee (`fct_data_per_solver_and_accounting_period`), which is
  not modelled.
- Not a counterfactual: the consistency and quote pools are budgets tied to protocol
  revenue, and without the solver its share would go to the remaining solvers. The rows
  say what the solver received, not what the protocol would save.

Checked on mainnet, 2026-11-25:

| check | result |
| --- | --- |
| Σ relative surplus over 2026-11-10..17 vs `total_relative_surplus` | 28 of 28 solvers, worst relative difference 1.0e-14 |
| budget × Σ share / N vs `quote_reward_native`, same week | 21 of 21 solvers, worst 5.0e-15; N = 35,921 in both |
| the week as a window, three largest consistency earners (Kipseli, Dfusion, BRRRolver) | consistency and quote equal the mart rows (ratio 1.000000000000) |
| the week as a window, all 29 solvers with a row in any of the three marts | performance equal to Σ `batch_reward_native` exactly; consistency within 2.1e-15 and quote within 2.8e-15 relative; nothing unaccounted, no mismatch (66.95, 59.62 and 29.50 ETH in all) |
| 2026-11-10..13 and 2026-11-13..17 as two windows | each solver's two parts sum to its week within 4,859 wei (per-auction rounding) |
| 2026-11-12..14 as a window (2 of 7 days), same solvers | consistency 26.0–30.5% and quote 29.3–30.6% of the week's rows |
| loading a week for three solvers / all 29 | 3.6 s / 4.7 s |

## Out of scope

The counterfactual quote reward — what the replacement quoters would earn
([quotes](quotes.md#out-of-scope-and-named-as-such)); the quote rewards recorded as paid
are reported ([above](#rewards-paid-to-a-solver-recorded)). The solver service fee.
