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

## Design decisions

Every judgement call in the pipeline, in one place: what was chosen, what it was chosen
over, where it lives in code, and what to touch to revise it. The narrative rationale
stays in the sections and docs each row links to; the milestone sections cite these IDs.

| | decision | chosen over | lives in | to revise |
| --- | --- | --- | --- | --- |
| D1 | Rank solutions on the **recorded score**; surplus mode is a separate, self-consistent answer to the user-value question, not an approximation of the competition ([§2](#2-two-valuations-deliberately-separate)) | recomputing scores — impossible, fee policies only exist for executed orders | `valuation.solution_total` | `--mode surplus` exists |
| D2 | The fairness filter compares per-pair **user surplus on both sides**, in both modes ([§2](#2-two-valuations-deliberately-separate), [measured](docs/winner-selection.md#the-filter-runs-on-surplus)) | `scaled` / `raw` score-scale splits — built, measured 30× worse, removed | `build_solutions` filling `Solution.pair_values` from `pair_surplus` | feed a different per-pair decomposition; the measured cost of this one is the proxy error rate ([§4.1](#41-m1-result)) |
| D3 | The LOO ranking is a **full re-arbitration** — steps 3–6, so removing a solver can lower a baseline and un-filter solutions ([§5](#5-m2--loo-ranking-and-surplus-deltas--done)) | re-picking on the recorded kept set, as reference scores do — would have missed all 14 un-filter wins | `counterfactual.leave_one_out` | — |
| D4 | A winner's settlement attaches to the **slot**, not the solver: a replacement inherits the recorded outcome of the token pairs it claims ([§5](#5-m2--loo-ranking-and-surplus-deltas--done)) | `observed` — kept as the provable lower bound; `assume-settled` — kept as the usual upper bound | `counterfactual.OUTCOME_RULE`, `side_outcomes` | `--outcome-rule` exists |
| D5 | "Executed" means **`is_settled_in_time`** on both the surplus and the reward side; a late batch counts as a failure and its real surplus is reported, not absorbed ([why](docs/analytics-db.md#settled-and-settled_in_time-come-apart-and-which-one-you-want-depends)) | `tx_hash is not null` — what users actually received | `extract.Settlement.counts_as_executed` | flip that one property |
| D6 | Executed amounts always come from **`proposed_trade_executions`**; only the settlement *status* is read from chain ([verified exact](docs/analytics-db.md#a-settled-proposal-is-exact--the-only-divergence-is-settling-at-all)) | reading on-chain trade amounts — redundant, a settled proposal executes exactly, to the atom | `extract.py` | — |
| D7 | Tie-breaks reproduce the record by feeding solutions in **`uid` order** through stable sorts ([why](docs/analytics-db.md#proposed_solutionsuid-encodes-the-ranking)) | — the autopilot's pre-sort shuffle is not recoverable from anything else | uid-ordered extraction, stable sorts in `arbitrate` | not revisable |
| D8 | Retain an auction when **reference scores move**, not only when the winner set changes — rewards move on a reference score alone ([§5.1](#51-m2-result)) | winner-set-only retention — silently drops 43% of the auctions whose rewards move | `AuctionCounterfactual.anything_moved` | — |
| D9 | `--solver` matches names **exactly** and removes **every** rotated address together ([traps](docs/analytics-db.md#resolving-a-solver-name)) | substring match — `Arc` ⊂ `Arctic`, both bid, two competitors silently removed | `extract.SOLVER_SQL`, `resolve_solver` | — |
| D10 | Auctions the solver never bid in are **skipped but stay in the denominator**, so rates describe the window rather than the solver's own subset | rates over the solver's auctions only | `analyse_auction` early return, `Analysis.add` | — |

## 2. Two valuations, deliberately separate

Decisions D1 and D2. The competition ranks on **score** = user surplus + protocol fees.
Our surplus question is about **user surplus** alone. These are not interchangeable: over 17.6k single-order bids
the median score/surplus ratio is 1.08, p90 is 2.70, and 3.2% of bids are fee-dominated
(surplus under 1% of score) — see [docs/winner-selection.md](docs/winner-selection.md#measured-divergence--surplus-is-not-a-stand-in-for-score).

So the pipeline carries both, and `arbitrate` takes them as inputs rather than deriving them:

| Input to `arbitrate` | score mode (default) | surplus mode |
| --- | --- | --- |
| solution total (steps 2, 5, 6) | `proposed_solutions.score` | Σ per-order surplus in native |
| per-pair decomposition (steps 3, 4) | per-pair surplus, used as a proxy | same |

Both sides of the step-4 comparison use surplus, baselines included — a batch's surplus on a
pair against the best surplus any single-pair solution reached on that pair. Nothing is
invented and the units match. Two score-scale alternatives were measured and are 30× further
from the recorded filter, so they were dropped rather than kept as options
([numbers](docs/winner-selection.md#the-filter-runs-on-surplus)).

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
  primitives.py        # as_erc20, price_in_eth, ceil_div, order owner, per-network WETH
  extract.py           # window -> auction ids; per-auction bid/order/price bundles
  valuation.py         # per-order surplus in native; per-solution totals and pair maps
  winner_selection.py  # pure: baselines, fairness filter, pick_winners, reference_scores
  validate.py          # M1: reproduce the recorded competition and attribute every diff
  counterfactual.py    # baseline vs. LOO per auction -> per-order and per-auction diffs
  rewards.py           # uncapped (+ later capped) rewards
  cli.py               # --network --solver <name|addr> --start --end --mode --out
tests/
  test_winner_selection.py
  test_valuation.py
  test_validate.py
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

## 4. M1 — extraction and baseline reproduction — **done**

**The gate.** Everything downstream inherits errors made here. Results in
[§4.1](#41-m1-result); what follows is what was built.

1. **Connect and window.** Implement `db.connect(network)` per
   [docs/analytics-db.md](docs/analytics-db.md#connecting) (the URL has no scheme and no
   dbname). Resolve one day of auctions via the date→block→auction query in
   [the same doc](docs/analytics-db.md#mapping-a-date-window-to-auctions). Only the
   surplus cross-check needs `int_backend_data__proposed_solution_data`, whose lag is
   large and variable — measured at 7.5 days, not the 2–3 originally assumed
   ([why](docs/analytics-db.md#coverage-and-lag)) — so check its frontier rather than
   picking a window by age.

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
   [docs/winner-selection.md](docs/winner-selection.md#the-algorithm). For the comparison,
   `validate.py` re-runs single steps against the DB's own decisions: `observed_pick`
   re-picks winners on the recorded kept set, and `observed_ranking` rebuilds the
   recorded ranking for `compute_reference_scores`.

5. **Compare against the DB, in score mode:**

   | Recomputed | Ground truth |
   | --- | --- |
   | winner set | `proposed_solutions.is_winner` |
   | `filtered_out` | `proposed_solutions.filtered_out` |
   | reference scores (via the observed ranking) | `stg_backend_data__reference_scores` |

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

### 4.1 M1 result

```bash
uv run python -m loo.cli validate --start 2026-08-01 --end 2026-08-04 --cross-check-surplus
```

7,745 mainnet auctions, 98,669 solutions, score mode. **Gate met** — the command exits 0
only when nothing is left unexplained.

| | |
| --- | --- |
| step 5 re-picked on the recorded kept set vs. `is_winner` | **7745 / 7745** |
| step 6 re-run on the recorded ranking vs. `reference_scores` | **7745 / 7745** |
| solutions that could not be valued | **0** |
| per-order surplus vs. the dbt model | 93941 / 94565, every difference exactly −1 |
| end-to-end winner set / `filtered_out` | 7740 / 7738 of 7745 |
| filter decisions differing from the DB | **7** — 2 provably the deliberate surplus-filter difference, 5 proxy-attributable |

The comparison is structured so a cause is *proven* rather than asserted. Two of the three
checks hold the DB's own filter decisions fixed and re-run a single step, so no
approximation is in their path — both are exact, which clears steps 5 and 6 outright and
localises every remaining difference to step 4.

For step 4, the true per-pair split is unknown but constrained: each pair's score is at
least its user surplus (fees are non-negative) and the splits sum to the recorded score,
so a split the recorded filter could have *kept* exists iff
`Σ max(baseline, surplus) ≤ score`. That feasibility test decides **95%** of multi-pair
solutions regardless of the fees. All 7 differences go the same way — the recorded filter
dropped a batch the surplus filter keeps, always on a partially fillable order. For 2 of
them keeping is provably infeasible, so they are the deliberate consequence of filtering
on surplus (`model`); the other 5 fall in the undetermined 5% where the split genuinely
decides (`proxy`). Full numbers in
[docs/winner-selection.md](docs/winner-selection.md#the-filter-runs-on-surplus).

Expected explanation (a), partial fills, turned out **not** to contribute: with exact
integer ceiling division the surplus formulas reproduce the dbt model on every order up to
that model's own rounding. The whole residual is explanation (b).

Two findings worth carrying forward, both now in `docs/`:

- `proposed_solutions.uid` is assigned best-to-worst, so it records the arbitrator's
  output ordering including the tie-breaks from its pre-sort shuffle. Reference scores
  depend on that ordering, which is *not* plain score order
  ([why](docs/winner-selection.md#ranked-order-is-load-bearing)).
- `order_surplus_atoms_in_surplus_token` is off by one atom whenever Postgres numeric
  division rounds the quotient before `ceil()` runs
  ([why](docs/analytics-db.md#order_surplus_atoms_in_surplus_token-rounds)).

Not built, deferred to their milestones: `counterfactual.py` (M2), `rewards.py` (M3),
`notebooks/analysis.ipynb` (M4). Surplus mode is implemented and unit-tested but has not
been run over a window, since M1's comparisons are all score-mode by definition.

## 5. M2 — LOO ranking and surplus deltas — **done**

Results in [§5.1](#51-m2-result); what follows is what was built.

1. `loo_ranking = arbitrate([s for s in solutions if s.solver != X])` — **full
   re-arbitration** (D3), steps 3–6, so removing `X` can lower a baseline and *un-filter*
   solutions. Track auctions where the kept/filtered partition changes, and whether a
   newly-kept solution wins, as its own statistic. Expected to be rare and the most
   interesting case when it happens.
2. Skip auctions where `X` submitted nothing; keep them in the denominator (D10).
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

**Decided — neither (a) nor (b), but a third rule (D4).** Both options in the paragraph
above attach settlement to the *solution*, and both are biased:

- (a) `--outcome-rule observed` charges the baseline for its own reverts while assuming
  every replacement settles, because no record exists to consult. So where `X` won and
  reverted it reports users *gaining* from `X`'s removal, on nothing but the assumption
  that the replacement would have landed. Biased against `X`.
- (b) `--outcome-rule assume-settled` assumes *everyone* settles, baseline included, so it
  overstates what users actually received wherever a winner reverted.

The default is `--outcome-rule inherited`, which attaches settlement to the **slot**
instead: a winner that also won for real takes its own recorded outcome, and a replacement
inherits the outcome of the recorded winner(s) that held its token pairs. A batch that
really reverted stays reverted whoever is put in its place. Settlement therefore cancels out
of `Δsurplus` and the result measures the competition's *decision*, which is the question M2
asks. (a) and (b) are kept as the pessimistic and optimistic bounds around it, which matters
because the gap between them is **larger than the signal** and flips the sign of the answer
for one of the two solvers measured — see [§5.1](#51-m2-result).

Slots are keyed on `winner_pairs`, the `as_erc20`-normalised directed pairs `pick_winners`
claims, since those are exactly what makes one solution displace another. Three details:

- A replacement spanning several slots needs *all* of them to have settled, since one
  reverting leg would have taken the whole batch with it.
- A replacement claiming a pair no recorded winner held has nothing to inherit, so its
  settlement is assumed. That count is PLAN §5's "how often the mapping fails" and is
  reported as `settlement assumed anyway`.
- All three rules are applied to *both* sides identically, so a winner that survives the
  removal cancels exactly rather than manufacturing a difference.

`1,533` of the `10,301` winners in the window never settled, so this is not a corner case.

Two facts measured over the M1 window settled the rest of the design; the details and
verification live in
[docs/analytics-db.md](docs/analytics-db.md#observed-outcomes-what-actually-settled). A
settled proposal executes **exactly what it proposed, to the atom**, so executed amounts
always come from `proposed_trade_executions` and the only thing read from chain is the
settlement *status* (D6). And "executed" means **`is_settled_in_time`**, so a late batch
counts as a failure — deliberately discarding the real surplus of 16 late winners so that
M2 and M3 agree on which winners delivered, with the discarded amount reported as
`orders_lost_to_lateness` rather than absorbed (D5).

### 5.1 M2 result

```bash
uv run loo analyse --solver Sector --start 2026-08-01 --end 2026-08-04
```

Same 7,745 mainnet auctions as M1, score mode, all three outcome rules:

| | Fractal | Sector |
| --- | --- | --- |
| auctions bid in | 5,863 (75.7%) | 4,872 (62.9%) |
| auctions won, recomputed / recorded | 1,007 / 1,004 | 1,025 / 1,025 |
| baseline winner set differs from the DB | 5 | 0 |
| **Δsurplus, `inherited`** (the default) | **+0.2632 ETH** | **+8.0082 ETH** |
| Δsurplus, `observed` (lower bound) | −0.0347 ETH | +2.8919 ETH |
| Δsurplus, `assume-settled` | +0.5116 ETH | +8.1318 ETH |
| user orders compared | 8,439 | 6,961 |
| executed only with the solver (`inherited`) | 636 | 184 |
| executed only *without* it (`inherited` / `observed`) | 10 / 95 | 13 / 52 |
| user orders the baseline lost to a failed settlement | 1,187 | 827 |
| …of which merely late, so their surplus was real | 13 | 9 |
| replacements inheriting a reverted slot | 80 | 38 |
| replacements with nothing to inherit (mapping failures) | **0** | **0** |
| fairness filter relaxed (and a newly-kept solution won) | 3 (3) | 11 (11) |
| helped set another solver's reference score | 814 pairs | 1,282 pairs |

**How a replacement's settlement is modelled dominates the answer, and that is the main
finding.** Under `observed` removing Fractal appears to make users *better off*; the sign is
an artefact. That rule charges the baseline for the removed solver's own reverts while
assuming every replacement lands, because no record exists for a solution that never won —
so wherever `X` won and reverted it books a free gain from `X`'s removal.

Attaching settlement to the **slot** removes the artefact: 1,533 winners (14.9%) never
settled and they carry **50.2% of all winning score**
([numbers](docs/analytics-db.md#failures-are-concentrated-in-the-biggest-solutions)), so this
is where most of the money is. Under `inherited` a reverted slot contributes zero to
*both* sides and drops out, Fractal's sign flips to positive, and the remaining gap to
`assume-settled` is small (0.26 vs 0.51, 8.01 vs 8.13) — that residual is genuine uncertainty
about whether reverted batches would have landed, not an accounting asymmetry.

Two facts make the slot rule cheap rather than a modelling gamble:

- **The mapping never failed.** Across both solvers, *every* replacement claimed a token pair
  some recorded winner held, so its settlement was always derived and never assumed —
  the `0` row above. PLAN §5 asked for this rate expecting it to be small; it is zero.
- **It also fixes the "executed only without the solver" anomaly.** That count falls from 95
  to 10 for Fractal and 52 to 13 for Sector, so ~85-90% of those orders were the settlement
  asymmetry rather than a real effect. What is left is the blocked-batch mechanism below.

Treating a late settlement as a failure cost nothing here, but only by luck: none of the
window's 16 late winners belong to either solver measured (they are BRRRolver 5, Baseline 4,
Barter 3, Helixbox 3, Wraxyn 1). For those two, every late batch was some other solver's
winner that won on both sides and cancelled, so Δsurplus is identical to four decimals under
either criterion — it moved only the `unsettled` count, by exactly the 9 and 13 late orders.
Removing one of those five solvers would move the headline, so `orders_lost_to_lateness` is
worth reading, not just carrying.

`observed` is retained because it is a *provable* lower bound: it differs from `inherited`
only on reverted slots, where it credits the replacement with positive surplus and
`inherited` credits it with nothing, the baseline being zero either way. `assume-settled` is
usually but not provably the upper bound — a reverted slot whose replacement carries more
user surplus than the winner it displaced would push it below `inherited`, which score-mode
ranking permits since score is not surplus.

Three further results worth carrying into M3 and M4:

- **Un-filtering is rare and always decisive.** The filter relaxed in 3 auctions for Fractal
  and 11 for Sector, and in *every single one* a newly-kept solution went on to win. So step
  1's guess that this would be "rare and the most interesting case" holds on both counts:
  full re-arbitration is required, and `compute_reference_scores`' cheap variant would have
  missed all 14.
- **"Executed only without the solver" is legitimate, not a bug** — contrary to step 4,
  which called it a correctness signal. Under `inherited` it is down to 10 orders for
  Fractal and 13 for Sector, and what remains is the blocked-batch mechanism:
  `pick_winners` needs *every* pair of a solution free, so a single-order winner can block a
  multi-order batch and leave orders unexecuted that nobody else bid on
  ([traced](docs/winner-selection.md#a-blocked-batch-keeps-orders-unexecuted)).
- **Headline sums are whale-dominated; report medians in M4.** Sector's +8.0082 ETH is
  +8.0085 over 976 auctions against −0.0003 over 7, and a *single* auction (13498037)
  carries +6.5587 ETH — **82% of the total**, leaving 1.4495 ETH for the other 982. The
  median non-zero auction moves 0.000056 ETH. Fractal is less extreme but still skewed: its
  largest auction is 20% of the total.
- **Almost every negative contribution was a settlement artefact.** Under `observed` Sector
  had −5.12 ETH spread over 43 auctions pulling against the headline; under `inherited` that
  collapses to −0.0003 ETH over 7, and Fractal has none at all. So the removed solver
  essentially never *hurt* users where it won — the earlier appearance that it did came from
  crediting its replacements with settlements that never happened.

Scope notes. A counterfactual cannot resurrect solutions the arbitrator never saw:
`max_solutions_per_solver` is a pre-arbitration filter in the autopilot
([why](docs/winner-selection.md#max_solutions_per_solver-is-applied-before-arbitrate)).
`--solver` resolves a name to *every* submission address that bid in the window, because
several solvers have rotated keys and all of a rotation must be removed together (D9,
[traps](docs/analytics-db.md#resolving-a-solver-name)). Surplus mode runs but was not the
basis of these numbers; on a 200-auction slice it moved Sector's Δ from 0.00161 to 0.00206
ETH and disagreed with the recorded winner set in 2 auctions, as expected from §2.

Not built, deferred: `rewards.py` (M3), `notebooks/analysis.ipynb` (M4). The per-auction
records carry both sides' winner sets, winning totals and reference scores, plus which
solvers supplied each reference score, so M3 needs no further extraction.

One trap that cost a rewrite (D8): retention must key on **reference scores moving**, not
on the winner set changing. Removing `X` moves a surviving solver's reference score in far more
auctions than it moves a win, because one of `X`'s *non-winning* solutions can be a winner of
the without-`s` pick inside `compute_reference_scores`. For Sector that is 1,798 auctions
retained against 1,025 with a changed winner set — and reference scores are the denominator of
the uncapped reward, so keeping only the latter would have silently dropped 43% of the
auctions whose rewards actually move. Δsurplus is unaffected, so nothing in M2's own output
would have shown it.

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

**Lead with `inherited` and carry `observed` as the lower bound.** M2 found that how a
replacement's settlement is modelled moves the headline by several ETH and, under the
solution-attached rule, flips a sign ([§5.1](#51-m2-result)). Quoting a single figure without
naming its rule would be a choice disguised as a result.

Report **medians alongside sums**: a single auction supplies 82% of Sector's net Δsurplus,
against a median non-zero auction of 0.000056 ETH, so a sum on its own says more about one
whale than about the solver.

State these caveats with the results:

1. **No behavioural response.** The remaining solvers' bids are held fixed. In reality they
   would bid differently without `X`; this is not modelled and no claim is made about which
   direction it would move the result. Note this cuts one specific way too:
   `max_solutions_per_solver` is applied before arbitration, so no rival's suppressed
   fourth solution can step in
   ([why](docs/winner-selection.md#max_solutions_per_solver-is-applied-before-arbitrate)).
2. **Settlement risk** — which §5 rule was used. `inherited` hands a replacement the
   settlement of the slot it displaced, so a reverted batch stays reverted and settlement
   cancels out of Δsurplus; quote `observed` alongside it as the provable lower bound.
   14.9% of winners never settled and they hold 50.2% of winning score, so this is a
   first-order caveat, not a footnote — the residual `inherited`-to-`assume-settled` gap is real
   uncertainty about whether those batches would ever have landed.
3. **Filter proxy** — the measured per-pair surplus proxy error from M1 step 5.
4. **Quote rewards excluded** — no data on counterfactual quoting.
