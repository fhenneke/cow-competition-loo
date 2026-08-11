# Leave-one-out solver analysis — plan

Counterfactual: re-run past auctions with one solver removed and measure what users and
the protocol lose or save.

Background context lives in [docs/](docs/) and is not repeated here:

- [docs/analytics-db.md](docs/analytics-db.md) — connecting, tables, join keys, coverage,
  date→auction mapping, how to read dbt model source
- [docs/winner-selection.md](docs/winner-selection.md) — the algorithm, Rust source map,
  score/surplus formulas, measured score-vs-surplus divergence
- [docs/rewards.md](docs/rewards.md) — reward and cap formulas

## 1. Questions

For a solver `X`, a network, and a time window:

1. **Surplus** — how much surplus did users get with `X` vs. without?
2. **Rewards** — how much would have been paid out without `X`?
3. **Coverage** — how many orders were executed *only* because of `X`?

Secondary: how often `X` won, how often `X` set another solver's reference score, how often
removing `X` relaxes the fairness filter, concentration of impact by token pair and app code.

## 2. Two valuations, deliberately separate

The competition ranks on **score** = user surplus + protocol fees. Our surplus question is
about **user surplus** alone. These are not interchangeable: over 17.6k single-order bids
the median score/surplus ratio is 1.08, p90 is 2.70, and 3.2% of bids are fee-dominated
(surplus under 1% of score) — see [docs/winner-selection.md](docs/winner-selection.md#measured-divergence--surplus-is-not-a-stand-in-for-score).

So the pipeline carries both, and `arbitrate` takes them as inputs rather than deriving them:

| Input to `arbitrate` | score mode (default) | surplus mode |
| --- | --- | --- |
| solution total (steps 2, 5, 6) | `proposed_solutions.score` | Σ per-order surplus in native |
| per-pair decomposition (steps 3, 4) | per-pair surplus, used as a proxy | same |

- **Score mode is the default** and is what reward numbers and "who would have won" must
  use. Scores come straight from the DB, so no protocol-fee reimplementation is needed.
- **Surplus mode** answers "how much user value did `X` add" self-consistently, and is the
  fallback if the per-pair proxy turns out to distort the filter.
- The per-pair decomposition is the one thing not available from the DB — scores are stored
  per solution, not per token pair. Surplus is the proxy in both modes. Quantify the damage
  in M1 step 5 rather than assuming it away.

## 3. Deliverables

```
loo/
  db.py                # connect(network) -> conn; run(sql, params) -> DataFrame
  extract.py           # window -> auction ids; per-auction bid/order/price bundles
  valuation.py         # per-order surplus in native; per-solution totals and pair maps
  winner_selection.py  # pure: baselines, fairness filter, pick_winners, reference_scores
  counterfactual.py    # baseline vs. LOO per auction -> per-order and per-auction diffs
  rewards.py           # uncapped (+ later capped) rewards
  cli.py               # --network --solver <name|addr> --start --end --mode --out
tests/
  test_winner_selection.py
notebooks/analysis.ipynb
```

`winner_selection.py` takes plain dataclasses and no DB handle:

```python
@dataclass(frozen=True)
class Solution:
    solver: str                          # hex address
    solution_uid: int
    total: int                           # score or surplus, in native wei
    pair_values: dict[tuple[str, str], int]   # (sell_token, buy_token) -> value, raw tokens
    order_uids: frozenset[str]
    winner_pairs: frozenset[tuple[str, str]]  # as_erc20-normalised, for pick_winners
```

Integer arithmetic throughout the valuation path — no floats.

## 4. M1 — extraction and baseline reproduction

**The gate.** Everything downstream inherits errors made here.

1. **Connect and window.** Implement `db.connect(network)` per
   [docs/analytics-db.md](docs/analytics-db.md#connecting) (the URL has no scheme and no
   dbname). Resolve one day of auctions via the date→block→auction query in
   [the same doc](docs/analytics-db.md#mapping-a-date-window-to-auctions). Pick a day at
   least 4 days old so `int_backend_data__proposed_solution_data` covers it.

2. **Extract per auction.** Bids and executions from
   `int_backend_data__proposed_solution_data`; `order_uids` and
   `surplus_capturing_jit_order_owners` from `stg_backend_data__competition_auctions`;
   native prices from `stg_backend_data__auction_prices`; `kind` and limit amounts from
   `stg_backend_data__orders` ∪ `stg_backend_data__jit_orders`.
   If joining the raw staging tables instead, join `solution_uid` to
   `proposed_solutions.uid` — **not** `.id`
   ([why](docs/analytics-db.md#join-keys)).

3. **Value.** Per-order surplus → native using the sell/buy formulas in
   [docs/winner-selection.md](docs/winner-selection.md#score-vs-surplus); note the `/1e18`
   and that buy orders need limit amounts. Skip orders failing `contributes_to_score`.
   Build `pair_values` on raw tokens and `winner_pairs` on `as_erc20`-normalised tokens.

4. **Implement `arbitrate`** — steps 1–6 of
   [docs/winner-selection.md](docs/winner-selection.md#the-algorithm), plus
   `arbitrate_fixed_filter` (step 5 only, on a given kept set) for reference scores.

5. **Compare against the DB, in score mode:**

   | Recomputed | Ground truth |
   | --- | --- |
   | winner set | `proposed_solutions.is_winner` |
   | `filtered_out` | `proposed_solutions.filtered_out` |
   | reference scores (via `arbitrate_fixed_filter`) | `stg_backend_data__reference_scores` |

   **Inspect every auction where the winner set differs, individually — this is not a
   percentage gate.** For each, record: number of solutions, whether any order is
   partially fillable, whether the disagreement is in the filter or the pick, and the score
   gap between the swapped solutions.

   Expected explanations, in order of likelihood: (a) partial fills — scaled limit amounts
   and ceiling division in `surplus_over`; (b) the per-pair surplus proxy flipping a
   fairness decision (§2). Anything else is a bug and must be resolved before M2.

   Also report, as its own number, how often the surplus proxy changes the filter outcome
   versus scores — this is the honest measure of how much §2's approximation costs.

**Exit criterion:** every winner-set discrepancy has a named, verified cause, and the
filter-proxy error rate is measured and written down.

## 5. M2 — LOO ranking and surplus deltas

1. `loo_ranking = arbitrate([s for s in solutions if s.solver != X])` — **full
   re-arbitration**, steps 3–6, so removing `X` can lower a baseline and *un-filter*
   solutions. Track auctions where the kept/filtered partition changes, and whether a
   newly-kept solution wins, as its own statistic. Expected to be rare and the most
   interesting case when it happens.
2. Skip auctions where `X` submitted nothing; keep them in the denominator.
3. Per-order diff over the union of orders traded by baseline and LOO winners:

   | field | note |
   | --- | --- |
   | `executed_base`, `executed_loo` | explicit booleans |
   | `surplus_base`, `surplus_loo` | `None` when unexecuted |

   **Surplus can legitimately be zero for an executed order** — keep the flag separate and
   substitute 0 only when aggregating.
4. Report: `Δsurplus`, orders executed only with `X` (`executed_base and not executed_loo`),
   and the reverse (a correctness signal — should be rare).

**Decide before starting:** outcomes for replaced winners. The baseline uses *observed*
outcomes; a counterfactual winner that never won has none. Either (a) reuse the observed
outcome where the counterfactual winner also won in the baseline and fall back to its
proposed execution otherwise, or (b) always use the proposed execution. (a) is preferred and
should usually apply, since solutions rarely batch overlapping orders. Record how often the
mapping fails, and state the choice in the output.

## 6. M3 — rewards

Score mode only. Apply the formulas in [docs/rewards.md](docs/rewards.md) to the baseline
and LOO winner sets:

1. Validate the baseline reproduction against `fct_solver_rewards_per_auction.uncapped_reward`
   before computing anything counterfactual.
2. Counterfactual uncapped rewards from the LOO winner set and its recomputed reference
   scores. `X`'s reward drops out entirely.
3. `Δrewards = baseline_total − loo_total`; convert native → COW.
4. Caps only if the protocol-fee estimate for replacement winners proves tractable — see
   [docs/rewards.md](docs/rewards.md#why-the-cap-is-hard-counterfactually). Uncapped is a
   legitimate stopping point; say so in the output if you stop there.

## 7. M4 — aggregation and write-up

Window aggregation, USD conversion, per-solver comparison table, notebook.

Headline row per solver-window: Δsurplus (native/USD), Δrewards, net value =
Δsurplus − Δrewards, orders saved, auctions affected, share of auctions won,
filter-relaxation count.

State these caveats with the results:

1. **No behavioural response.** The remaining solvers' bids are held fixed. In reality they
   would bid differently without `X`; this is not modelled and no claim is made about which
   direction it would move the result.
2. **Settlement risk** — which §5 variant was used.
3. **Filter proxy** — the measured per-pair surplus proxy error from M1 step 5.
4. **Quote rewards excluded** — no data on counterfactual quoting.
