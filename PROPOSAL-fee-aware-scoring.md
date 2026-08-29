# Proposal — score the fairness filter on score, not surplus

Status: proposal, measured but not implemented. Branch `claude/analytics-scoring-fees-63852c`.

## The claim

`stg_backend_data__fee_policies` is far more complete than
[docs/analytics-db.md](docs/analytics-db.md#fee-policies-are-incomplete) says, and complete
in a shape that happens to line up with what the fairness filter needs. Protocol and
partner fees can therefore be recomputed, and the per-pair values the filter compares can
be put on the **score** scale — where the protocol puts them — instead of the surplus
proxy in use today.

Measured on the M1 window (mainnet, auctions 13480599–13517908, 2026-08-01..04):
**99.956% of solutions get an exact per-pair score decomposition**, against 0% today.

## 1. The storage rule, and why the docs are wrong

`docs/analytics-db.md` claims the table "only has rows for orders that were executed".
That is not the rule. The autopilot writes fee policies for every order of every
**non-filtered-out** solution (`services/crates/autopilot/src/run_loop.rs:551`):

```rust
let fee_policies: Vec<_> = ranking
    .ranked()                                  // <- kept solutions only, no filtered_out
    .flat_map(|bid| bid.solution().order_ids())
    .unique()
    .filter_map(|order_id| order_lookup.get(order_id) ...)   // <- auction orders only
    .collect();
```

`Ranking::ranked` is the kept set; `Ranking::filtered_out` is a separate field
(`arbitrator.rs:658`). So the rule is: **an auction order has its fee policies recorded
for auction `a` iff it appears in at least one solution of `a` with
`filtered_out = false`.** Executed-ness never enters.

The rule holds exactly. Over the M1 window, grouping (auction, order) pairs by whether the
order appears in a kept solution:

| in auction | in a kept solution | has fee-policy rows | (auction, order) pairs |
| --- | --- | --- | --- |
| yes | yes | **yes** | 20,210 |
| yes | no | **no** | 1,638 |
| no (JIT) | yes | no | 6 |

No exceptions in either direction, and **zero** fee-policy rows exist for an order with no
trade execution in that auction. Two useful corollaries:

- A covered order with no rows would mean "genuinely no policies, fee 0". It does not
  occur in this window — all 20,210 covered orders have at least one policy — but the
  coverage test is what disambiguates it from "not recorded", so it is decidable either way.
- JIT orders are never in `auction.orders`, so they never have policies and their protocol
  fee is exactly 0. That is not a gap; it is the right answer.

## 2. Coverage — what fraction cannot be scored

Same window: 13,503 auctions carrying trade executions, 181,023 execution rows,
169,153 solutions.

**Execution rows:**

| | rows | share |
| --- | --- | --- |
| in a kept solution — fees recorded | 161,949 | 89.46% |
| in a filtered-out solution, order also in a kept one | 17,078 | 9.43% |
| **fees not recorded** | **1,996** | **1.10%** |

**Solutions:**

| | solutions |
| --- | --- |
| kept — every order covered, by construction | 160,403 |
| filtered out, every order covered anyway | 6,925 |
| filtered out, ≥1 order with unrecorded fees | **1,825** (1.08%) |

**Directed token pairs**, at the solution × raw `(sell_token, buy_token)` grain the filter
actually compares (auction orders only, so contributing orders):

| | solution-pairs | (auction, pair) | distinct pairs |
| --- | --- | --- | --- |
| kept | 160,993 | 18,571 | 2,156 |
| filtered out, covered | 16,982 | 5,353 | 1,097 |
| **filtered out, affected** | **1,916 (1.07%)** | **1,535** | **103** |

1,444 of 13,503 auctions (10.7%) contain at least one affected execution row.

### Two structural facts that shrink this further

Both follow from the Rust, not from the window:

1. **Every baseline is exactly computable.** Baselines come only from solutions that touch
   exactly one directed pair (`compute_baseline_scores`, `arbitrator.rs:627`), and a
   single-pair solution is kept unconditionally — `aggregated_scores.len() == 1` short-circuits
   the fairness test (`arbitrator.rs:100`). Every baseline-setter is therefore a kept
   solution, and every kept solution's orders are covered.
2. **Every kept solution's per-pair split is exact.** Same coverage argument, applied to the
   left-hand side of the comparison.

So the entire gap is confined to filtered-out solutions — and even there, most of it is
recoverable.

## 3. The recovery ladder

A solution's recorded `proposed_solutions.score` is the exact sum of its per-pair scores
(`compute_scores_by_solution`, `arbitrator.rs:154` folds the pair map into the stored
total). That identity is a free equation, and it closes most of the remaining gap.

For a solution with recorded score `S`, known pair values `v_j`, and unknown pairs `U`:

```
Σ_{i ∈ U} v_i  =  S − Σ_{j ∉ U} v_j
```

- **|U| = 1** → the unknown pair's value is determined **exactly** by the residual.
- **|U| ≥ 2** → only the sum is known; the split needs the existing joint-feasibility
  bracket (`u_i ≤ v_i`, `Σ v_i = residual`), which decides most cases and marks the rest
  `undetermined` exactly as
  [docs/winner-selection.md](docs/winner-selection.md#bounding-what-the-missing-split-can-cost)
  already does.

Measured over the 1,825 affected solutions:

| | solutions | outcome |
| --- | --- | --- |
| unknown fees confined to one pair | **1,750 (95.9%)** | exact by residual |
| unknown fees across 2+ pairs | **75 (4.1%)** | bracket, as today |

**Net: 169,078 of 169,153 solutions (99.956%) get an exact per-pair score split. 75
solutions (0.044%) do not.** Today's surplus proxy is inexact for every multi-pair
solution — 5,211 of 98,669 over the three-day sub-window — so this is a ~70× reduction in
the number of solutions where the filter runs on a stand-in.

### Backfill is available but mostly unnecessary

Policies derive from order class, app data and quote, so they should be stable across
auctions for a given order. They are, exactly: of 17,083 orders with policies in the
window, 1,485 appear in more than one auction and **all 1,485 have byte-identical policy
lists in every auction** — zero variation. 688 of the 1,638 uncovered (auction, order)
pairs can be backfilled from another auction in the window alone.

Given the residual trick already resolves 95.9% of affected solutions, backfill is worth
holding as a fallback for the 75 hard cases rather than building first.

## 4. Computing the fee

`arbitrator.rs:260` (`protocol_fees`). Policies apply **in reverse order**, each one seeing
clearing prices adjusted for the fees accumulated so far:

```
total = 0; current = base_prices
for i in reversed(range(len(policies))):
    total += protocol_fee(order, policies[i], current)
    if i != 0:
        current = calculate_custom_prices(order, total, base_prices)
```

Ordering comes from `application_order` ascending — a **global** serial, not a per-order
index, so only the relative order within an order is meaningful.

The three policy kinds map onto the table's columns directly (`surplus`, `volume`,
`priceimprovement`). Price improvement needs the order's quote, which is
`stg_backend_data__order_quotes` — one row per order, no auction dimension — with

```
fee = floor(gas_amount * gas_price / sell_token_price)     # exact rational, then floor
```

matching `autopilot/src/infra/persistence/dto/quote.rs`. Compute it in `Fraction`, not
float: the three inputs are `double precision` and the quotient is floored, so a float
divide rounds the wrong way often enough to matter.

### The rounding that costs a day

`surplus_fee` and `volume_fee` both go through `U256::checked_mul_f64`
(`number/src/u256_ext.rs:105`):

```
scaled = factor * 1e18        # f64 multiply
result = x * U256::from(scaled) / 1e18
```

The f64→U256 conversion **rounds**; it does not truncate. Truncating instead reproduces
99.1% of scores and leaves a tail of small negative errors — 23 of 2,512 solutions,
relative error 3.6e-16 (median) to 1.2e-13. Tracing one case (auction 13509109, a sell
order with `priceimprovement(0.5, 0.0098)` + `volume(3e-5)` + `volume(0.003)`) isolates it:

| variant | recomputed fee | recorded |
| --- | --- | --- |
| truncate | 367424346879096657 | −480 |
| exact rational | 367424346879096953 | −184 |
| **round** | **367424346879097137** | **exact** |

Note that the exact-rational answer is also wrong: the protocol's own f64 detour has to be
reproduced, not improved on. `factor / (1 - factor)` and `factor / (1 + factor)` are f64
operations too and must stay that way.

### Validation

`compute_order_score` with fees, summed per solution, against
`proposed_solutions.score`, **wei-exact**, over auctions 13505000–13509867:

```
19,795 solutions, 21,355 executions, 2,203 quotes (0 unusable)
compared                     19,653
exact                        19,653      <- every one, to the wei
differs                           0
skipped: no policies stored     142      (the coverage gap of §2)
```

With truncation instead of rounding the same test gives 2,489/2,512 exact on a smaller
sub-window and a tail of 23 small negative errors; with the rounding fixed, nothing
diverges at all. So the whole of `protocol_fees` reproduces from the analytics DB —
surplus fees, volume fees, partner fees and price improvement, in the reverse-order
application with price adjustment between policies — with no approximation anywhere on
the path.

## 5. What it buys

The fairness filter is the only place the missing per-pair split enters — every end-to-end
difference from the DB traces to it
([docs/winner-selection.md](docs/winner-selection.md#reproduction-status)). Today both
sides of its comparison use native user surplus, which is deliberate and measured: it beats
the two score-scale alternatives by an order of magnitude (7 wrong decisions of 5,211
versus ~198) precisely because a surplus batch compared against a *score* baseline is
biased by the baseline solution's own protocol fees, and no split of the batch's score can
correct for it.

Recomputing fees removes that bias at its source rather than cancelling it: both sides
become real per-pair scores. The 7 known differences should go to 0 or near it, and — more
importantly — the filter stops being a proxy in the leave-one-out counterfactual, where
removing a solver moves baselines and can un-filter solutions.

Two things this does **not** change: solution totals still come from
`proposed_solutions.score` (no reason to recompute what is recorded exactly), and reward
numbers are untouched.

## 6. Implementation sketch

1. `loo/fees.py` (new) — `Policy`, `Quote`, and a port of `protocol_fees` /
   `protocol_fee` / `surplus_fee` / `volume_fee` / `adjust_quote_to_order_limits` /
   `calculate_custom_prices`, plus `mul_f64` reproducing `checked_mul_f64`. Pure integer +
   the deliberate f64 steps. A working prototype exists in the scratchpad.
2. `loo/extract.py` — two queries: fee policies for the window (ordered by
   `application_order`), and quotes for the traded order uids. Plus a `fees_known` flag per
   (auction, order), computed as "appears in a solution with `filtered_out = false`".
3. `loo/valuation.py` — `value_solution` gains a `pair_score` map alongside `pair_surplus`,
   populated where fees are known; `SolutionValuation` carries which pairs are exact.
4. Residual closure — after valuing a solution, if exactly one pair is unknown, set it to
   `db_score − Σ(known pairs)`.
5. `loo/counterfactual.py` — the filter compares `pair_score` when every pair of the
   solution *and* every relevant baseline is exact, and falls back to the current surplus
   comparison otherwise. `validate` reports the fallback rate.
6. Gate: re-run the three-day reproduction and check the 7 filter differences and the 5
   differing winner sets.

The fallback in step 5 matters — this is an improvement in exactness for 99.96% of
solutions, not a replacement of a working mechanism, and the surplus path should stay
reachable and tested.

## 7. An independent cross-check exists

`int_backend_data__solution_data.upper_reward_cap` is built from
`int_backend_data__trade_data_processed_with_native_prices`, which already carries
`protocol_fee_native` and `partner_fee_native` **per trade**. It is settled-winners-only,
so it is no use as a data source for scoring — that is exactly the narrow slice the
counterfactual does not need. But it is a second, independently derived opinion on the
same quantity for that slice, and it splits partner fee out from protocol fee, which the
policy list does not label. Worth wiring into `validate` as a third check alongside the
score reproduction.

(`protocol_fees_scaling_cap_config` turns out to be reward-side only — it scales
`protocol_fee_without_partner_fee_native` into `upper_reward_cap`
(`int_backend_data__solution_data.sql:115`) and never touches a score. Mainnet is at 0.5
since 2026-04-14. Not a concern for this proposal.)

## 8. Open questions

- **Non-mainnet networks** — the storage rule is protocol-level so it should hold
  everywhere, but the coverage *rate* depends on how much gets filtered out and has only
  been measured on mainnet.
- **Older windows** — the `ranked()`-not-`all()` behaviour should be checked against the
  window under analysis rather than assumed; it is a one-line change upstream.
- The docs fix in §1 should land regardless of whether the rest is built.
