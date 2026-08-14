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
| D4 | A winner's settlement attaches to the **slot**, not the solver: a replacement inherits the recorded outcome of the token pairs it claims ([§5](#5-m2--loo-ranking-and-surplus-deltas--done)). `assume-settled` (everything lands in time) is the one alternative scenario kept | `observed` (settlement attached to the solution) — carried through M3 as a "lower bound", **removed in the M4 review**: charging the baseline for real reverts while crediting every replacement is not a counterfactual anyone would defend, and calling it a bound kept inviting it to be quoted ([§7.1](#71-m4-result)) | `counterfactual.OUTCOME_RULE`, `side_outcomes` | `--outcome-rule` exists |
| D5 | "Executed" means **`is_settled_in_time`** on both the surplus and the reward side; a late batch counts as a failure and its real surplus is reported, not absorbed ([why](docs/analytics-db.md#settled-and-settled_in_time-come-apart-and-which-one-you-want-depends)) | `tx_hash is not null` — what users actually received | `extract.Settlement.counts_as_executed` | flip that one property |
| D6 | Executed amounts always come from **`proposed_trade_executions`**; only the settlement *status* is read from chain ([verified exact](docs/analytics-db.md#a-settled-proposal-is-exact--the-only-divergence-is-settling-at-all)) | reading on-chain trade amounts — redundant, a settled proposal executes exactly, to the atom | `extract.py` | — |
| D7 | Tie-breaks reproduce the record by feeding solutions in **`uid` order** through stable sorts ([why](docs/analytics-db.md#proposed_solutionsuid-encodes-the-ranking)) | — the autopilot's pre-sort shuffle is not recoverable from anything else | uid-ordered extraction, stable sorts in `arbitrate` | not revisable |
| D8 | Retain an auction when **reference scores move**, not only when the winner set changes — rewards move on a reference score alone ([§5.1](#51-m2-result)) | winner-set-only retention — silently drops 43% of the auctions whose rewards move | `AuctionCounterfactual.anything_moved` | — |
| D9 | `--solver` matches names **exactly** and removes **every** rotated address together ([traps](docs/analytics-db.md#resolving-a-solver-name)) | substring match — `Arc` ⊂ `Arctic`, both bid, two competitors silently removed | `extract.SOLVER_SQL`, `resolve_solver` | — |
| D10 | Auctions the solver never bid in are **skipped but stay in the denominator**, so rates describe the window rather than the solver's own subset | rates over the solver's auctions only | `analyse_auction` early return, `Analysis.add` | — |
| D11 | Rewards are reported twice: **uncapped exactly**, and **capped as an estimate** in which a replacement inherits the displaced slot's recorded `upper_reward_cap` — realised fees follow the orders, and a reverted slot's cap is 0 exactly where its settlement is a revert ([§6.1](#61-m3-result)) | uncapped only — measured unusable as a payout answer (−410 ETH uncapped vs 0.75 ETH paid over the window); full fee-policy backfill — unnecessary while cap orphans are 0 | `rewards.uncapped_rewards` caps path, `counterfactual.winner_caps` | backfill fee policies per docs/rewards.md for orphans |
| D12 | The reward side consumes the **same per-solution settlement decision** as the surplus side, under whatever outcome rule is in force, so the two views of one auction cannot disagree about which winners delivered (D5 extended to M3) | reading `is_settled_in_time` again independently | `SideOutcomes.solution_executed` feeding `Win.settled` | — |
| D13 | Δrewards converts native → COW **per auction**, at the accounting-period rate of its `block_deadline`; an unsnapshotted rate leaves the auction unconverted and reported, never guessed | one window-level rate — wrong whenever a window straddles a Tuesday period boundary | `run.convert_delta_rewards` | — |
| D14 | Every solution's executed amounts are valued through **both** tokens' native prices; an auction with a >2× disagreement is **excluded from every statistic** and named in the report — a wrong price fabricates every number it touches, so quoting it at all misleads ([§6.1](#61-m3-result)) | trusting `auction_prices` — one token was ~15,300× off for a whole window, fabricating the M2 "whale"; cross-auction median checks — fail exactly there, the wrong price is the persistent one; with-and-without reporting — built first, dropped as noise once the with-suspects numbers proved purely artefactual ([details](docs/analytics-db.md#native-prices-can-be-plain-wrong)) | `counterfactual.price_imbalanced`, `Analysis.exclude_price_suspect` | `--include-price-suspects`, `PRICE_IMBALANCE_THRESHOLD` |
| D15 | USD figures are **display-only conversions**, per auction, at the rate implied by the auction's own stablecoin prices (median of USDC/USDT/DAI in `auction_prices`, window-median fallback); networks without curated reference tokens skip USD rather than guess ([source](docs/analytics-db.md#no-usd-prices--stablecoin-native-prices-imply-the-rate)) | an external price feed — a new dependency and a second trust domain for a cosmetic number; a single window rate — the per-auction rate costs nothing more (D13's logic) | `primitives.USD_REFERENCE_TOKENS`, `extract.load_usd_rates`, `aggregate.usd_total` | extend `USD_REFERENCE_TOKENS` after verifying addresses |
| D16 | Every delta is **counterfactual − actual** (without-solver minus with-solver), so numbers read as what the removal scenario changes and turning them into a "value of the solver" is left to the reader; the convention is printed on every rendering and stamped into report JSONs, which `compare` refuses without the marker (M4 review) | with − without, M2–M3's convention (a solver's value carries a plus sign by construction) — §5.1 and §6.1 keep their historical signs; flip them to compare with §7.1 | `counterfactual` delta properties, `aggregate.SIGN_CONVENTION_ID`, `load_report` | not worth revising again — a second flip would strand two conventions in the wild |
| D17 | Auctions with a traded order in **neither order table** are excluded transparently and named — `jit_orders` records only settled batches, so an unsettled solution's JIT legs are unrecoverable and even its `pick_winners` pair claims are unknown ([data](docs/analytics-db.md#jit-orders-are-recorded-only-when-the-batch-settles)); ~0.8% of a month window, zero in the M1 window by luck | zero-surplus tolerance — wrong for the 59% of missing orders that are surplus-capturing while CoW AMMs were live, and the missing pair claim measurably moves the pick ([8 real victims/month](docs/winner-selection.md#a-jit-leg-can-block-another-solutions-execution--and-the-filter-never-sees-it)); per-auction tolerate-and-validate — more coverage than 0.8% warrants in machinery | `extract.load_auctions` `missing_data`, `Analysis.missing_data_auctions` | for windows past the CoW-AMM deprecation (~late July 2026) zero-surplus is exact for score and filter; the missing pair claim and its blocking bias remain |
| D18 | The pipeline is a **library call** — `run.analyse_window(conn, solvers, start, end, …)` — and the CLI a rendering around it; several solvers share **one extraction pass**, since extraction is nearly all the cost (~5 min/3-day window) and the arbitration is milliseconds per auction ([§8](#8-m6--library-api-multi-solver-runs-derived-reports--done)) | one solver per run, orchestration living inside the CLI subcommand (M2–M5's shape) — N solvers cost N extractions and programmatic use meant shelling out | `run.analyse_window` | — |
| D19 | Report JSON is **derived from the dataclasses** — keys are the `Analysis`/`AuctionCounterfactual` field and property names — and versioned (format 3 since [§9](#9-m7--relative-price-impact--done)); `load_report` refuses a file without the current format marker ([§8](#8-m6--library-api-multi-solver-runs-derived-reports--done)) | a hand-written payload mirrored in four places (field, print, JSON key, loader) — every statistic cost four edits and the writer/reader key names had already drifted once | `run.report_payload`, `aggregate.REPORT_FORMAT` | bump `REPORT_FORMAT` on any shape change |
| D20 | The **relative price impact** is Δsurplus / baseline received-leg volume, in bps, over contributing **fill-or-kill** orders executed on both sides whose surplus moved; surplus and volume are valued through the same buy-token native price, so the per-order ratio survives even a wrong price ([§9](#9-m7--relative-price-impact--done)) | including partially fillable orders — the two sides can execute different amounts, mixing quantity into a price figure; counting unmoved still-traded orders — dilutes the figure toward zero as a function of batch composition; a whole-window normalisation — answers a different (average-user) question and needs a window-volume field the report does not carry | `valuation.order_volume_native`, `counterfactual.OrderDiff`, `aggregate.PriceImpact` | conditioning lives in `aggregate._price_impact`, applied at load time — format-3 reports need no re-run to revise it |

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
  aggregate.py         # M4: analyse reports -> medians, USD, per-solver comparison
  run.py               # M6: analyse_window() — the pipeline as one library call
  cli.py               # subcommands: validate, validate-rewards, analyse, compare
tests/
  test_winner_selection.py
  test_valuation.py
  test_validate.py
  test_counterfactual.py
  test_rewards.py
  test_aggregate.py
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

> **Addendum (M3, D14):** the price-sanity check added in M3 later showed that
> Sector's headline below is dominated by a *fabricated* native price, not by a whale:
> auction 13498037's +6.56 ETH — 82% of the +8.01 — is a 0.80 ETH trade whose buy
> token was priced ~15,300× too high across the whole window. `analyse` now excludes
> such auctions from every statistic by default; on the clean set Sector's Δsurplus is
> **+1.33 ETH** and Fractal is unchanged. The numbers below are kept as measured with
> the suspects still in; do not quote Sector's without that warning —
> [§6.1](#61-m3-result),
> [details](docs/analytics-db.md#native-prices-can-be-plain-wrong).
>
> **Addendum (M4 review, D4/D16):** the `observed` rule discussed at length below was
> **removed** — see D4 — and the sign convention has since flipped to counterfactual −
> actual (D16). The numbers in this section keep their historical with − without signs
> and include `observed`; the current-convention result is [§7.1](#71-m4-result).

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

## 6. M3 — rewards — **done**

Score mode only. Results in [§6.1](#61-m3-result); what follows is what was built.

1. Validate the baseline reproduction against `fct_solver_rewards_per_auction.uncapped_reward`
   before computing anything counterfactual. **Done — `loo validate-rewards`**, which
   recomputes every row from the DB's own inputs (winning solutions and settlement flags
   from `int_backend_data__winning_solutions_with_onchain_status`, reference scores from
   staging — the same models the mart reads). Nothing in that path is approximated, so
   unlike M1 the gate is absolute: any row that is not an exact match exits non-zero.
2. Counterfactual uncapped rewards from the LOO winner set and its recomputed reference
   scores. `X`'s reward drops out entirely. **Done** — both sides computed in
   `analyse_auction`, feeding each winner the same settlement decision as the surplus
   side (D12), so `observed_score` follows the outcome rule.
3. `Δrewards = baseline_total − loo_total`; convert native → COW. **Done** — converted
   per auction at the accounting-period rate of its `block_deadline` (D13). Only
   auctions retained in `changed` can carry a non-zero reward delta, which is what D8's
   wider retention was for.
4. Caps only if the protocol-fee estimate for replacement winners proves tractable — see
   [docs/rewards.md](docs/rewards.md#why-the-cap-is-hard-counterfactually). Uncapped is a
   legitimate stopping point; say so in the output if you stop there. **It proved
   tractable without any fee estimation (D11):** the recorded caps cover every winning
   solution in the window, and a replacement inherits the displaced slot's cap the same
   way it inherits the slot's settlement — realised fees follow the orders, and a
   reverted slot's cap is 0 exactly where its settlement is a revert. The capped
   formula is validated in the same gate as the uncapped one, against
   `batch_reward_native`. What stays an estimate is only *whose* cap a replacement
   gets; the mapping never falls back to a guess (a cap orphan drops its auction from
   the capped aggregate, and there were 0).

Added while answering step 4, because §6.1's first run made the need obvious (D14): a
**price-sanity check**. Every solution's executed amounts are valued through both
tokens' native prices; an auction with a >2× disagreement is excluded from every
statistic and named in the report (`--include-price-suspects` overrides). This caught
`auction_prices` being ~15,300× wrong on one token for the whole window — which had
fabricated the M2 "whale" ([§5.1 addendum](#51-m2-result)).

### 6.1 M3 result

> **Addendum (M4 review, D16):** signs in this section are the historical
> with − without convention; flip them to compare with [§7.1](#71-m4-result).

```bash
uv run loo validate-rewards --start 2026-08-01 --end 2026-08-04
uv run loo analyse --solver Sector --start 2026-08-01 --end 2026-08-04
```

**Gate met, exactly:** over the same 7,745 mainnet auctions as M1, all **9,809 / 9,809**
(auction, solver) reward rows recomputed from recorded inputs match
`fct_solver_rewards_per_auction` to the wei — `uncapped_reward`, `upper_reward_cap` and
`batch_reward_native` all six compared fields. (One trap: Python's default 28-digit
`Decimal` context rounds the cap sums the DB keeps exact; `loo.rewards` sets 78.) The
whole window sits in one accounting period (2026-07-28 → 2026-08-04, rate
6.045134508635538e-05 COW→native), so the COW figures below are single-rate.

Counterfactual, `inherited` outcome rule. **44 price-suspect auctions (0.6% of the
window) are excluded from every figure (D14)** — the same 44 for both solvers, since
the flag is a property of the auction:

| | Fractal | Sector |
| --- | --- | --- |
| auctions analysed / solver bid | 7,701 / 5,846 | 7,701 / 4,845 |
| Δsurplus | +0.2631 ETH | +1.3259 ETH |
| uncapped rewards with / without | −9.15 / −8.97 ETH | −12.11 / −9.03 ETH |
| Δrewards (uncapped) | −0.1784 ETH (−2,952 COW) | −3.0783 ETH (−50,921 COW) |
| the solver's own uncapped reward | +0.2096 ETH | −3.6563 ETH |
| capped rewards with / without | −0.2295 / −0.2108 ETH | +0.5248 / +0.6229 ETH |
| **Δrewards (capped estimate)** | **−0.0187 ETH** (−309 COW) | **−0.0981 ETH** (−1,622 COW) |
| cap double-inherited / orphans | 4 / 0 | 2 / 0 |
| auctions where any reward moved | 1,275 | 1,780 |
| negative uncapped rows, base / loo | 661 / 623 | 574 / 564 |

Four findings, and each of the first three decides how M4 may quote a number:

- **The capped estimate is the payout answer, and it is orders of magnitude below the
  uncapped figure.** Sector: −0.098 ETH capped against −3.08 uncapped (and against
  −113.28 before the price exclusion); Fractal −0.019 against −0.18. Both are
  negative — actual payments would *rise* slightly without either solver, because
  rivals' reference scores fall and their capped rewards grow — but at the scale of a
  tenth of an ETH over three days. The uncapped number remains the mechanism's exact
  accounting, penalty-dominated (−reference_score per failed settlement against a real
  floor of −0.01 ETH). Quote uncapped as accounting, capped as money.
- **44 auctions in the window have a fabricated native price, and before D14 they
  carried M2's headline.** The two-sided check values each trade through both tokens;
  the window's five biggest "whales" — including the auction that supplied **82% of
  Sector's published +8.01 ETH Δsurplus** — are one token priced ~15,300× too high, a
  0.80 ETH trade recorded as ~139 ETH of score. They are now excluded outright: with
  them, Sector's Δsurplus reads +8.01 instead of +1.33 ETH, its uncapped Δrewards
  −113.28 instead of −3.08, and even its own uncapped reward flips sign (+3.03 vs the
  real −3.66 — the phantom win was carrying its whole positive balance). Fractal
  barely moves. Ridiculous-with, plausible-without is exactly why exclusion is the
  default rather than a side-by-side.
- **The caps and the price check agree independently.** Before exclusion, the suspect
  auctions contributed −110.20 ETH of Sector's uncapped delta but only −0.0010 ETH of
  its capped delta: a fabricated score cannot fabricate realised fees, so the
  fee-derived cap neutralises the phantom on its own. That the two guards give the
  same answer by different routes is the best evidence either is right.
- **D8 priced out:** rewards moved in 1,780 Sector auctions against 1,011 with a
  changed win. Retaining on winner-set change alone would have dropped 43% of the
  auctions whose rewards move.

The slot rule extends to caps at almost no cost: caps were never orphaned (a
replacement always claimed a recorded winner's pair — the same 0 as M2's settlement
mapping) and double-inheritance, where two replacements split one displaced winner's
pairs and each takes its whole cap, happened 2–4 times per solver in ~5,000 auctions.
A replacement inheriting a reverted slot earns no `observed_score` and a 0 cap
consistently (38 for Sector, 80 for Fractal; D12). Other outcome rules were not run
over the window in M3 — the cap distortion dominates the rule sensitivity here, and M4
can add them if a bound is wanted.

Not built, deferred: `notebooks/analysis.ipynb` (M4). Cap backfill via fee policies
(D11's revision path) stays unneeded while orphans are 0.

## 7. M4 — aggregation and write-up — **done**

Window aggregation, USD conversion, per-solver comparison table, notebook. Results in
[§7.1](#71-m4-result); what follows is the spec and what was built.

Headline row per solver-window: Δsurplus (native/USD), Δrewards, net value =
Δsurplus − Δrewards, orders saved, auctions affected, share of auctions won,
filter-relaxation count.

**Built as post-processing, not a new pipeline.** `loo compare out/*.json` aggregates
reports written by `analyse --out`: the report already carries every changed auction's
deltas, and an auction where nothing moved cancels identically, so the whole window
lives in those files and a comparison costs seconds rather than six 5-minute re-runs.
`loo/aggregate.py` re-derives every sum from the per-auction moves and **rejects a
report whose totals disagree with its own auctions** (truncated, edited, or from an
incompatible `analyse`), and likewise rejects a report not stamped with the current
sign convention — both flanks of a stale file fail loudly instead of tabulating
wrong. A comparison refuses to tabulate a solver-window that lacks an `inherited`
run — the headline rule is a property of the method, not of whichever file happens to
exist. Direction stays visible beside the magnitude statistics as the per-auction
sign split, and the sign convention (counterfactual − actual, D16) is printed on
every rendering. The same module drives `notebooks/analysis.ipynb` (concentration
curve, per-auction distribution); the CLI and the notebook render one
`aggregate.comparison(...)` result.

USD conversion is D15: per auction, at the rate implied by the auction's own
stablecoin prices — the analytics DB has no USD table, and the three mainnet reference
tokens agree to ~0.1% over the window
([source](docs/analytics-db.md#no-usd-prices--stablecoin-native-prices-imply-the-rate)).
Δrewards is converted over the same changed auctions, so every USD figure is the exact
per-auction conversion of the ETH figure beside it.

**Lead with `inherited`; `assume-settled` is the one alternative scenario.** M2 found
that how a replacement's settlement is modelled moves the headline by several ETH
([§5.1](#51-m2-result)), so the rule is named with every figure. The M4 review then
removed the third rule (`observed`) outright — settlement attached to the solution is
not a counterfactual anyone would defend, and its number kept being misread as an
answer (D4). What remains are the two readings that mean something: the record's own
settlements (`inherited`) and everything-lands-in-time (`assume-settled`).

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
   cancels out of Δsurplus; `assume-settled` is the everything-lands-in-time reading.
   14.9% of winners never settled and they hold 50.2% of winning score, so this is a
   first-order caveat, not a footnote — the `inherited`-to-`assume-settled` gap is real
   uncertainty about whether those batches would ever have landed.
3. **Filter proxy** — the measured per-pair surplus proxy error from M1 step 5.
4. **Quote rewards excluded** — no data on counterfactual quoting.
5. **Two reward figures, not one** (D11). Uncapped is the mechanism's exact accounting
   but three orders of magnitude from payouts (−410 ETH uncapped vs 0.75 ETH paid over
   the window); the capped figure is the money answer but an *estimate* — replacements
   inherit the displaced slot's cap. Lead with capped for "what would payments do",
   keep uncapped for "what does the mechanism think", and never net either against
   Δsurplus without saying which — [§6.1](#61-m3-result).
6. **Price-suspect auctions are excluded** (D14). 44 auctions in this window carry a
   fabricated native price; they held 82% of Sector's M2 Δsurplus and 97% of its
   uncapped Δrewards. Every statistic is over the clean set; name the excluded
   auctions and their count (0.6% of the window) rather than quoting any number that
   includes them.

These plus D15's display-only USD caveat are rendered by `compare` and the notebook
with every table — the numbers are not supposed to travel without them.

### 7.1 M4 result

```bash
for solver in Fractal Sector; do
  for rule in inherited assume-settled; do
    uv run loo analyse --solver "$solver" --start 2026-08-01 --end 2026-08-04 \
        --outcome-rule "$rule" --out "out/$(echo $solver | tr 'A-Z' 'a-z')-$rule.json"
  done
done
uv run loo compare out/*.json --markdown
```

Same 7,745 mainnet auctions as M1–M3, score mode, every statistic over the 7,701-auction
clean set (D14). USD at each auction's stablecoin-implied rate, window median
$1,863.76/ETH (D15). Signs are D16's convention, stated on every rendering: **every
delta is counterfactual − actual**, so the numbers read as what the removal scenario
changes — negative Δsurplus means users would have received less — and turning a
change into a value of the solver is the reader's step. The table below is `compare`'s
output verbatim:

| | Fractal | Sector |
| --- | --- | --- |
| auctions analysed | 7,701 of 7,745 (44 price-suspect excluded) | 7,701 of 7,745 (44 price-suspect excluded) |
| solver bid | 5,846 (75.9%) | 4,845 (62.9%) |
| solver won | 1,005 (13.1%) | 1,011 (13.1%) |
| Δsurplus (inherited) | -0.2631 ETH (-$488.69) | -1.3259 ETH (-$2,463.34) |
| &nbsp;&nbsp;assume-settled | -0.5114 ETH (-$949.92) | -1.4449 ETH (-$2,684.29) |
| &nbsp;&nbsp;median non-zero auction | 0.000094 ETH ($0.18) over 750 auctions | 0.000058 ETH ($0.11) over 970 auctions |
| &nbsp;&nbsp;auctions moved + / − | 0 (0.0000 ETH) / 750 (-0.2631 ETH) | 7 (+0.0003 ETH) / 963 (-1.3262 ETH) |
| &nbsp;&nbsp;largest single auction | -0.0518 ETH — 20% of the total (auction 13509837) | -0.2595 ETH — 20% of the total (auction 13500787) |
| Δrewards uncapped | +0.1784 ETH (2951.69 COW, $329.26) | +3.0783 ETH (50921.50 COW, $5,738.93) |
| Δrewards capped (estimate) | +0.0187 ETH (308.99 COW, $34.46) | +0.0981 ETH (1622.03 COW, $184.29) |
| net change (Δsurplus − capped Δrewards) | -0.2818 ETH (-$523.15) | -1.4239 ETH (-$2,647.62) |
| orders executed only with the solver | 635 (7.6% of 8,386 compared) | 183 (2.7% of 6,897 compared) |
| orders executed only without | 10 | 12 |
| auctions where anything moved | 1,275 | 1,780 |
| fairness filter relaxed | 3 (newly kept won 3) | 10 (newly kept won 10) |

Four findings:

- **Removing either solver leaves users and the protocol worse off, at small dollar
  scale.** In the counterfactual, users lose 0.26 ETH ($489) without Fractal and
  1.33 ETH ($2,463) without Sector over three days, while capped payments *rise*
  (+0.02 / +0.10 ETH — rivals' reference scores fall, so their capped rewards grow):
  the net change, Δsurplus minus the *capped* reward delta per caveat 5, is
  −0.28 ETH ($523) and −1.42 ETH ($2,648). The reward side reinforces rather than
  offsets the surplus side, at a tenth-of-an-ETH scale.
- **The settlement scenario matters, and it is named with every figure.** The
  everything-lands-in-time reading (`assume-settled`) deepens the loss to −0.51 /
  −1.44 ETH; its gap to `inherited` (−0.25 / −0.12) is genuine uncertainty about
  whether reverted batches would ever have landed, which is caveat 2. The third rule
  M2 had carried as a "lower bound" (`observed`) was removed in this review (D4):
  charging the baseline for real reverts while crediting every replacement with
  settling is not a counterfactual anyone would defend, and on the clean set it
  swung Sector's answer by 5.1 ETH — a measure of how wrong an indefensible
  settlement assumption can be, not a bracket worth reporting.
- **Even the clean set is whale-shaped, which is why the medians ride in the table.**
  The largest single auction is 20% of the headline for both solvers, and the median
  non-zero auction moves $0.18 (Fractal) / $0.11 (Sector) — five orders of magnitude
  below the sum it contributes to. The direction, kept visible in the sign-split
  row, is near-uniform: all 750 of Fractal's moved auctions and 963 of Sector's 970
  lose surplus in the counterfactual. The notebook's concentration curve makes the
  whale point graphically: a handful of auctions carry the window, so any one of
  them being wrong (the D14 lesson) moves the headline materially, and per-auction
  inspection of the top movers stays part of reading a result.
- **Coverage is where the two solvers genuinely differ.** 635 orders (7.6% of those
  compared) trade only when Fractal is present, against Sector's 183 (2.7%) —
  Fractal wins fewer, smaller auctions but is more often the only bidder on an
  order. At near-identical win rates on the clean set (13.1% each), the coverage
  question and the surplus question give opposite rankings, which is the reason M4
  reports them side by side rather than collapsing them into one score.

Clean-set bookkeeping against §5.1/§6.1 (mind D16 — those sections keep the old
with − without signs): recomputed wins drop to 1,005 / 1,011 (from 1,007 / 1,025 with
suspects in), Sector's filter relaxations to 10 (from 11 — one was in a suspect
auction), and orders compared to 8,386 / 6,897. `compare` re-derives every total from
the per-auction moves and refuses a report whose own sums disagree or that predates
the sign convention, so the table cannot silently drift from the files behind it.

Deliverables land with this milestone: `loo/aggregate.py`, the `compare` subcommand,
`tests/test_aggregate.py`, and `notebooks/analysis.ipynb` (`uv run --extra notebook
jupyter lab`). The rendered `out/comparison.{txt,md}` are regenerable run artifacts —
`out/` stays untracked, and the table above is the record. Not covered, recorded
rather than implied: §1's secondary question on concentration by token pair and app
code (the per-order diffs carry no token pair in the report JSON), and multi-window /
multi-network sweeps — both are post-processing extensions of the same reports.

## 8. M6 — library API, multi-solver runs, derived reports — **done**

Not a new analysis — a restructuring, after a review of the whole repo against its
purpose ("compute the counterfactual for a solver, chain and window, as simply as
possible"). Three structural changes (D18, D19) and a cleanup:

1. **The pipeline is a function.** `run.analyse_window(conn, solvers, start, end, …)`
   does resolve → extract → arbitrate → COW conversion and returns a `WindowAnalysis`;
   `loo analyse` renders it. Programmatic use no longer means shelling out or
   re-implementing the CLI's orchestration.
2. **Several solvers per extraction** (D18). Extraction is nearly all of a run's ~5
   minutes; the arbitration is ~ms per auction. `--solver` is now repeatable and every
   solver is analysed in the same pass over the bundles, so a
   many-solvers-over-a-month sweep costs one extraction, not N. `--out` takes a
   `{solver}` placeholder.
3. **The report file is derived, not hand-written** (D19). `run.report_payload` dumps
   the `Analysis`/`AuctionCounterfactual` fields plus named properties; JSON keys are
   the dataclass names, integers stay integers, Decimals become strings. The file
   carries `format: 2` and `load_report` refuses anything else with "re-run analyse" —
   this retired the four-copy shape (dataclass field, print line, JSON key, loader
   field) and with it the `settlement`/`outcome_rule` key drift the review found.
4. **Milestone vocabulary left the runtime.** Output and docstrings now say what the
   code does ("the validation gate", "the rewards gate", dated windows) instead of
   citing milestones; M-numbers live only in this file. Decision IDs (D1–D19) remain
   the cross-reference between code comments and this table.

Also landed here: ruff + pyright (strict) configured in `pyproject.toml` with the
codebase brought clean — this absorbed and superseded the stale
`linting-typechecking-setup` worktree — plus the COW conversion now tracking the
capped delta left unconverted, and the duplicate `REWARD_INPUTS_SQL` loader collapsed.

No numbers changed: the counterfactual, validation and reward paths are untouched, and
the validate/validate-rewards gates over 2026-08-01..04 are the regression test that
they stayed untouched.

## 9. M7 — relative price impact — **done**

The absolute Δsurplus says how much value moves; this milestone adds how much *prices*
move in relative terms (D20): basis points of traded volume, on the orders the
scenario still fills. Two figures per solver in the comparison — the volume-weighted
bps change and the signed median moved order — derived at load time from the report's
order diffs, exactly like the coverage slice.

The definitions, each a deliberate choice:

- **Denominator = the baseline received leg.** Per order the volume is `executed_buy`
  valued at the buy-token native price — the same price `to_native` puts into the
  surplus numerator, so the per-order ratio is exact even under a wrong native price
  (D14's failure mode): the price cancels. Only the weighting *across* orders trusts
  price levels, and price-suspect auctions are excluded anyway. Baseline-side because
  the denominator should be what actually happened (D16's logic).
- **Fill-or-kill orders only** (review guidance): a partially fillable order can
  execute different amounts on the two sides, and Δsurplus over one side's volume is
  then a price/quantity mixture rather than a price change. Excluded and counted in
  the table cell.
- **Only orders whose surplus moved.** An order re-executed identically carries no
  price information; including it would dilute the figure toward zero as a function
  of batch composition. The conditioning is stated in the row label and the caveat.
  A moved order whose baseline volume floors to 0 wei (dust) has no denominator and
  stays out the same way.
- **The coverage slice stays absolute.** An order that stops trading altogether is
  not "a worse price"; it remains the separate `orders executed only with the solver`
  rows.

Mechanically: `valuation.order_volume_native` is filled per contributing order
alongside the surplus; `OrderDiff` carries `volume_base`/`volume_loo`/
`partially_fillable`; the report is **format 3** and `compare` refuses older files
with "re-run analyse" — every stored report predates the volumes and must be
regenerated (one ~5-minute `analyse` per window, all solvers in one pass).
`aggregate.PriceImpact` applies the conditioning at load time, so format-3 reports
never need re-running to revise it.
