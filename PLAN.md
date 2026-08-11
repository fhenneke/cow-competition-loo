# Leave-one-out solver analysis — plan

Counterfactual: re-run past auctions with one solver removed and measure what users
and the protocol lose/save.

## 1. Questions to answer

For a solver `X`, a network, and a time window:

1. **User surplus.** How much surplus did users get with `X` vs. without? (per order, per
   auction, aggregated in native token / USD)
2. **Rewards.** How much would have been paid out in total if `X` had not participated?
   (`X`'s own rewards disappear; the remaining winners' rewards change because their
   reference scores change)
3. **Coverage.** How many orders were executed *only* because of `X` — i.e. traded in a
   winning solution in the real auction, but in no winning solution without `X`?

Secondary: how often `X` was a winner, how often `X` set someone else's reference score,
how often removing `X` *relaxes* the fairness filter (§4), concentration of impact over
token pairs / order sizes / app codes.

## 2. Data source

Postgres, credentials in `.env` (`ANALYTICS_DB_URL`, user `<db-user>`).
**One database per network**: `prod_mainnet`, `prod_xdai`, `prod_base`, `prod_arbitrum-one`, …
Models live in schema `dbt`.

| Table | Use |
| --- | --- |
| `dbt.stg_backend_data__proposed_solutions` | all bids: `auction_id, uid, solver, score, is_winner, filtered_out` — **`score` is the autopilot's own score per bid; we do not recompute it** |
| `dbt.stg_backend_data__proposed_trade_executions` | per-bid order executions: `auction_id, solution_uid, order_uid, executed_sell, executed_buy` |
| `dbt.int_backend_data__proposed_solution_data` | the two above joined + `sell_token, buy_token, order_surplus_atoms_in_surplus_token, bid_score, is_winner, winning_score` — **main input** |
| `dbt.stg_backend_data__competition_auctions` | `order_uids` (auction membership), `surplus_capturing_jit_order_owners`, `block_deadline` |
| `dbt.stg_backend_data__auction_prices` | native prices per `(auction_id, token)` |
| `dbt.stg_backend_data__orders`, `…__jit_orders` | `kind` (buy/sell), limit amounts, `partially_fillable` |
| `dbt.stg_backend_data__reference_scores` | ground truth for validating the reimplementation |
| `dbt.int_backend_data__solution_data` | winning solutions only: `reference_score, is_settled_in_time, upper_reward_cap, lower_reward_cap, is_excluded` |
| `dbt.fct_solver_rewards_per_auction` | ground truth rewards |
| `dbt.dune_data__cow_protocol__solvers` | `address ↔ name` lookup |

Coverage notes (checked 2026-08-11 on `prod_mainnet`):

- `int_backend_data__proposed_solution_data` covers auctions from `12709602` and lags the
  raw staging tables by ~2–3 days (max `13509867` vs. `13562448`). For the freshest
  auctions, join `stg_backend_data__proposed_solutions` × `…__proposed_trade_executions`
  directly.
- **`stg_backend_data__fee_policies` is only populated for executed orders**, even though
  essentially every order carries one or more policies (only some JIT orders are exempt).
  Two consequences:
  - It cannot be used as the "is this an auction order" test — use
    `competition_auctions.order_uids` (see §3, `contributes_to_score`).
  - Protocol fees cannot be computed for orders that were never executed. This is why we
    take scores from the DB rather than recomputing them (§3). It bites again only for
    counterfactual reward *caps* (§6), where a workaround is to backfill an order's
    policies from another auction in which the same `order_uid` was executed.

## 3. Winner selection to replicate

Source of truth: `~/Work/Code/services/crates/winner-selection/src/arbitrator.rs`
(`Arbitrator::arbitrate`, `pick_winners`, `compute_reference_scores`,
`compute_baseline_scores`); the autopilot only wraps it
(`crates/autopilot/src/domain/competition/winner_selection.rs`).

Per auction, with `max_winners = 20` (`crates/configs/src/autopilot/run_loop.rs:10`):

1. **Value each solution per directed token pair.** For every order in the solution that
   `contributes_to_score` (order is in `competition_auctions.order_uids`, or is a JIT order
   whose owner is in `surplus_capturing_jit_order_owners`), accumulate its value into
   `value[(sell_token, buy_token)]`. Solution total = sum over pairs.
2. Drop solutions with total 0. Sort descending.
3. **Baselines.** For each directed pair, `baseline[pair] = max` total among solutions that
   touch *exactly one* pair.
4. **Fairness filter.** Keep a solution if it touches one pair, or if for every pair
   `value[pair] >= baseline[pair]`. Otherwise → `filtered_out`.
5. **Pick winners.** Greedy over the kept solutions in descending order: a solution wins if
   its directed token pairs are disjoint from all pairs already claimed; stop at `max_winners`.
6. **Reference score** of winner `s` = total of the winners picked by step 5 applied to the
   ranked set with **all of `s`'s solutions** removed (a solver may submit up to 3).

Detail worth preserving: step 1 uses the raw `sell_token`/`buy_token`, while step 5
normalises them through `as_erc20` (ETH → WETH). The asymmetry is in the Rust and should be
mirrored.

### What "value" means — surplus vs. score

The autopilot's value is the CIP-38 score: `(surplus + protocol_fees)` in the surplus token,
converted to the buy token and then to native. We cannot recompute that for non-executed
orders (§2), but we do not have to:

- **Ranking (steps 2, 5) and reference scores (step 6)** only need a *total per solution* —
  available directly as `proposed_solutions.score`.
- **The fairness filter (steps 3–4)** is the only place needing a *per-token-pair
  decomposition*. Approximate it with surplus:
  `order_surplus_atoms_in_surplus_token` → buy token → × native price.

Plan accordingly:

- **v1:** surplus everywhere (filter and ranking). Simplest; self-consistent; good enough
  for the surplus questions.
- **v2:** filter on the surplus decomposition, rank and compute reference scores on the
  stored `score`. This is what the reward numbers should be based on.

Building both is cheap — the decomposition and the total are separate inputs to the same
`arbitrate` function.

## 4. Counterfactual procedure

Per auction in the window:

```
solutions        = all bids for the auction (incl. filtered_out)
baseline_ranking = arbitrate(solutions)
loo_ranking      = arbitrate([s for s in solutions if s.solver != X])   # full re-arbitration
```

**Full re-arbitration** (steps 3–5 re-run, not just the winner pick). Removing `X` can lower
a baseline and thereby *un-filter* solutions that were previously judged unfair. This is
expected to be rare but is exactly the interesting case, so **track it as its own statistic**:
number of auctions where the kept/filtered partition changes, and whether a newly-kept
solution ends up winning.

Note this differs from how the autopilot computes reference scores — `compute_reference_scores`
re-runs only `pick_winners` on the already-filtered set. Keep that cheaper variant available
as `arbitrate_fixed_filter`, since it is what validates against
`stg_backend_data__reference_scores` in M1.

Auctions where `X` submitted nothing are unchanged; skip them but keep them in the denominator.

### Open decision: outcomes for replaced winners

The baseline uses *observed* outcomes (did the winner settle in time, at what execution). A
counterfactual winner that never won in reality has no observed outcome. Options:

- **(a) reuse observed outcome** where the counterfactual winner also won in the baseline,
  and take the bid's proposed execution otherwise;
- **(b) always take the proposed execution** (assume every counterfactual winner settles as bid).

(a) is preferred and should usually be applicable: solutions rarely batch overlapping orders,
so mapping baseline winners onto counterfactual winners generally succeeds. Report how often
the mapping fails. **Decide this before M2 and state the choice in the output.**

## 5. Surplus metrics

Per order, over the union of orders traded by baseline winners and by LOO winners:

| | with `X` | without `X` |
| --- | --- | --- |
| executed | bool | bool |
| surplus (native, and surplus-token atoms) | `s_base` or `None` | `s_loo` or `None` |

**Surplus can legitimately be zero for an executed order.** Keep `executed` as an explicit
flag and surplus as `None` when unexecuted; substitute 0 only when aggregating.

- `Δsurplus = s_base − s_loo` → aggregate over the window, native and USD.
- **Orders only executed because of `X`**: `executed_base and not executed_loo`.
- Report the reverse too (executed without `X` but not with `X`) — should be rare, and is a
  correctness signal.

## 6. Reward metrics

Formulas confirmed against
`~/Work/Code/cow-dagster/cow_dagster/cow_dbt/models/solver_accounting/marts/fct_solver_rewards_per_auction.sql`
and `…/intermediary/backend_data/int_backend_data__solution_data.sql`:

```
winning_score          = Σ over winning solutions of score
competition_score(s)   = Σ over s's winning solutions of score
observed_score(s)      = Σ over s's winning solutions of score where is_settled_in_time
uncapped_reward(s)     = winning_score − competition_score(s) + observed_score(s)
                         − min(winning_score, reference_score(s))
capped_reward(s)       = 0 if auction excluded
                         else clamp(uncapped_reward(s), lower_reward_cap, upper_reward_cap)
upper_reward_cap(s)    = scaling_factor(block_deadline)
                         × Σ over s's winning solutions of max(protocol_fee_native − partner_fee_native, 0)
                         # scaling_factor from protocol_fees_scaling_cap_config, default 0.5
lower_reward_cap       = −reward_config.batch_reward_cap_lower   # 0.01 ETH on mainnet
                         # 0 inside a no_penalties_auctions window
```

- **Start with uncapped rewards.** They need only scores and reference scores, both of which
  the LOO ranking produces directly.
- Capped rewards are a later refinement: `upper_reward_cap` depends on the *realised protocol
  fees of the settled batch*, which for a replacement winner must be estimated from its
  proposed trade executions plus fee policies — and fee policies are missing for orders that
  were never executed (§2). Backfilling policies from other auctions is the workaround.
- `Δrewards = baseline_total − loo_total`, native → COW via
  `dbt.int_accounting_period_data__conversion_rates`.

**Headline output**: one row per solver-window with Δsurplus (native/USD), Δrewards, net
value = Δsurplus − Δrewards, orders saved, auctions affected, share of auctions where `X` won,
count of filter-relaxation cases.

## 7. Caveats to state in the output

1. **No behavioural response.** The bids of the remaining solvers are taken as fixed. In
   reality they would change if `X` were absent; the analysis does not model that and makes
   no claim about which direction it would move the result.
2. **Settlement risk.** Depends on the §4 decision; state which variant was used.
3. `filtered_out` in the DB is the autopilot's decision and the recomputation may disagree on
   a few solutions — measure the disagreement rate rather than assuming it away.
4. Quote rewards are out of scope: there is no good data on which solvers would have quoted
   had `X` not been there.

## 8. Layout

```
leave-one-out/
  .env
  pyproject.toml              # uv; psycopg2-binary, pandas, python-dotenv, (matplotlib)
  loo/
    db.py                     # connection per network, query helpers
    queries.py                # SQL: bids, executions, prices, auction orders
    valuation.py              # per-order surplus in native; per-solution totals (surplus or stored score)
    winner_selection.py       # pure: baselines, fairness filter, pick_winners, reference_scores
    counterfactual.py         # baseline vs. LOO per auction → per-order and per-auction diffs
    rewards.py                # uncapped (+ later capped) rewards
    cli.py                    # --network --solver <name|addr> --start --end --out
  tests/test_winner_selection.py   # port the unit tests from arbitrator.rs
  notebooks/analysis.ipynb    # aggregation, plots, write-up
```

`winner_selection.py` takes plain dataclasses and no DB handle — one solution total plus one
`{directed_pair: value}` map per solution — so the Rust unit tests port over directly and the
surplus/score choice stays outside it. Integer arithmetic in the valuation path.

## 9. Milestones

**M1 — extraction + baseline reproduction (the gate).** Pull a day of auctions, run
`arbitrate` on the real solution set, compare against the DB:

- recomputed winner set vs. `stg_backend_data__proposed_solutions.is_winner`
- recomputed reference scores vs. `stg_backend_data__reference_scores` (using
  `arbitrate_fixed_filter`, which is what the autopilot does)
- recomputed `filtered_out` vs. the stored flag

**Inspect every auction where the winner set differs**, individually — this is not a
percentage gate. The expectation is that all discrepancies trace back to partially fillable
orders (scaled limit amounts and ceil-division in the surplus computation). Any discrepancy
that does *not* have that explanation is a bug in the reimplementation and must be resolved
before M2.

**M2 — LOO ranking + surplus deltas.** Full re-arbitration, per-order diff table with explicit
executed flags, "orders only executed because of `X`", filter-relaxation statistic.

**M3 — rewards.** Uncapped Δrewards on stored scores; caps afterwards if the protocol-fee
estimate for replacement winners turns out to be tractable.

**M4 — aggregation + notebook.** Window aggregation, USD conversion, per-solver comparison,
write-up with the caveats from §7.
