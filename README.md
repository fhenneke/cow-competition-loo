# Leave-one-out solver analysis

Counterfactual analysis of the CoW Protocol solver competition: remove one solver from
past auctions, re-run winner selection, and measure the impact on user surplus, solver
rewards and order coverage.

## Setup

`uv` handles the environment; there is nothing to install by hand.

Put the analytics DB credentials in `.env` (gitignored):

```
ANALYTICS_DB_URL=user:password@host:port
```

No scheme and no database name — the database is derived from `--network`.

## The full flow

Windows are date ranges, `--start` inclusive, `--end` exclusive; the three-day window
below is ~7,700 mainnet auctions and each `analyse` run over it takes about five
minutes.

**Signs:** every delta is **counterfactual − actual** (without-solver minus
with-solver), so the numbers read directly as what the removal scenario changes:
negative Δsurplus means users would have received less, positive Δrewards means the
protocol would have paid more. How a change is turned into a *value of the solver* is
deliberately left to the reader. The tables state this convention with the numbers.

**1. The counterfactual**, once per solver and outcome rule. Only `inherited` is
required; `assume-settled` is the optional everything-lands-in-time scenario (what the
rules mean: [below](#the-outcome-rule)):

```bash
for solver in Fractal Sector; do
  for rule in inherited assume-settled; do
    uv run loo analyse --solver "$solver" --start 2026-08-01 --end 2026-08-04 \
        --outcome-rule "$rule" --out "out/$(echo $solver | tr 'A-Z' 'a-z')-$rule.json"
  done
done
```

**2. The comparison table** — seconds, it only reads the JSONs (plus one DB query for
the USD columns):

```bash
uv run loo compare out/*.json
```

Add `--markdown --out out/comparison.md` for a GitHub table, or `--skip-usd` to run
without a DB connection.

**3. The notebook** — the same aggregation plus the concentration curve, per-auction
distributions and the rule-sensitivity chart, from the same `out/*.json`:

```bash
uv run --extra notebook jupyter lab notebooks/analysis.ipynb
```

The measured result of exactly this flow is [PLAN.md §7.1](PLAN.md#71-m4-result).
First time on a new window, network or code change? Run the
[validation gates](#validating-the-implementation) first — the counterfactual is only
as good as its reproduction of the recorded competition.

## The commands

### analyse

```bash
uv run loo analyse --solver Sector --start 2026-08-01 --end 2026-08-04
```

Removes one solver from every auction in the window, re-runs winner selection, and reports
what users would have gained or lost. `--solver` takes a name or a submission address,
matched exactly — `Arc` and `Arctic` are different solvers and both compete.

| flag | |
| --- | --- |
| `--outcome-rule` | `inherited` (default) or `assume-settled` — see below |
| `--mode` | `score` (default) ranks on recorded scores; `surplus` ranks on user surplus |
| `--limit N` | only the first N auctions — start here |
| `--out report.json` | per-auction records, including both sides' reference scores and rewards |

Exit code is 0 normally, 1 if the window has no auctions, 2 if any auction could not be
valued, 4 if `--solver` did not resolve, 5 if the settlement source does not cover the
window.

#### The outcome rule

A winner that never won for real never settled either, so the counterfactual has to
decide what its orders do. That decision is not a footnote — 14.9% of winning solutions
never settled and they carry half of all winning score — so instead of hiding one
assumption in the code, the rule is an explicit choice between the two defensible
scenarios:

- `inherited` (**default**) — settlement belongs to the **slot**, not the solver. A
  replacement inherits the outcome of the recorded winner that held its token pairs, so a
  batch that really reverted stays reverted whoever replaces it. Settlement cancels out of
  Δsurplus and what is left is the competition's *decision*. This is the one scenario
  grounded in the record.
- `assume-settled` — everything lands in time, on both sides, so the comparison is about
  proposals alone and settlement risk is excluded entirely.

A third rule (`observed` — replacements assumed to settle while recorded winners keep
their real outcomes) existed through M3 as a "lower bound" and was removed: settlement
attached to the solution is not a counterfactual anyone would defend, and a number
nobody should quote is not made useful by calling it a bound. See
[PLAN.md §7.1](PLAN.md#71-m4-result).

Two things the rule names do **not** vary, both measured facts
([details](docs/analytics-db.md#observed-outcomes-what-actually-settled)): executed
amounts are always the **proposed** ones — a batch that lands executes its proposal
exactly, to the atom, so the only on-chain input the pipeline has is the settlement
status — and "settled" means landed **in time**, so the real surplus of late batches is
reported as `of which merely late` rather than absorbed.

#### Rewards

In score mode `analyse` reports solver rewards on both sides, twice:

- **Uncapped** — the mechanism's exact accounting. The removed solver's own reward
  drops out, and rivals' rewards grow because their reference scores fall without it.
  Not a payout: a failed settlement's uncapped penalty is `-reference_score` against a
  real floor of −0.01 ETH, and over the M1 window uncapped rewards sum to −410 ETH
  against 0.75 ETH actually paid.
- **Capped (estimate)** — the payout-scale answer, clamping each reward into the
  recorded caps. A replacement winner inherits the `upper_reward_cap` of the recorded
  winner whose token pairs it claims — realised fees follow the orders, and a reverted
  slot's cap is 0 exactly where its settlement is a revert
  ([details](docs/rewards.md#the-slot-inheritance-estimate-and-why-the-caps-cannot-be-skipped)).
  A winner with nothing to inherit drops its auction from the capped aggregate rather
  than being guessed at; the report counts those.

Both deltas are converted native → COW at each auction's accounting-period rate where
the rate has been snapshotted. Rewards use the same outcome rule as surplus, so the two
sides of one auction never disagree about which winners delivered.

#### Price sanity

Native prices in `auction_prices` are sometimes plain wrong — one token was priced
~15,300× too high for a whole window, fabricating 139 ETH scores on 0.80 ETH trades
([details](docs/analytics-db.md#native-prices-can-be-plain-wrong)). Every solution is
therefore cross-checked by valuing its executed amounts through both tokens' prices,
and an auction where the two sides of a trade disagree by more than 2× is **excluded
from every statistic** — the report names the excluded auction ids (0.6% of the M1
window, which carried 82% of Sector's Δsurplus, all fabricated).
`--include-price-suspects` keeps them in instead; the ids are printed either way.

### compare

```bash
uv run loo compare out/*.json
```

Aggregates `analyse --out` reports into the comparison: one column per solver-window,
the `assume-settled` scenario beside the `inherited` headline, medians, the sign split
and the largest auction's share beside every sum, and the caveats and sign convention
attached. Give every outcome-rule run of a solver-window together; a group without an
`inherited` run is refused rather than silently led by another rule.

USD columns are display-only conversions at each auction's own stablecoin-implied rate
(the analytics DB has no USD table —
[details](docs/analytics-db.md#no-usd-prices--stablecoin-native-prices-imply-the-rate)).

| flag | |
| --- | --- |
| `--skip-usd` | no DB connection, no USD columns |
| `--markdown` | render as a GitHub table |
| `--out FILE` | also write the rendering to a file |

## Validating the implementation

Everything above trusts that the pipeline reproduces the recorded competition. That is
not assumed — it is gated, and the gates are re-runnable. For using the analysis you
never need these; run them when the code, the window's data shape, or the network is
new, and before believing a surprising result:

```bash
uv run loo validate --start 2026-08-01 --end 2026-08-04
uv run loo validate-rewards --start 2026-08-01 --end 2026-08-04
```

### validate

Reproduces the recorded competition — winners, fairness filter, reference scores — and
accounts for every difference. Exit code is 0 when every difference has a named cause,
1 if the window has no auctions, 2 when something is unexplained, 3 on a bad
cross-check window.

| flag | |
| --- | --- |
| `--limit N` | only the first N auctions — start here, a full day is ~2,700 |
| `--cross-check-surplus` | also diff per-order surplus against the dbt model |
| `--out report.json` | write the full report, including every disagreeing auction |
| `--network` | defaults to `mainnet`; see `loo/db.py` for the rest |

`--cross-check-surplus` needs `int_backend_data__proposed_solution_data`, which lags the
raw tables by over a week. It fails with a clear message rather than checking nothing:

```bash
uv run python -c "from loo import db; c = db.connect('mainnet'); print(db.fetch(c, 'select max(auction_id) from dbt.int_backend_data__proposed_solution_data'))"
```

Everything else reads the staging tables and works up to the present.

### validate-rewards

Recomputes every winning solver's uncapped *and* capped reward from the DB's own
inputs — winning solutions, settlement flags, caps, reference scores — and diffs
against `fct_solver_rewards_per_auction`, row by row. Unlike `validate` there is no
accepted-difference category: nothing in this path is approximated, so anything but an
exact match on every row exits non-zero. Exit code 0 when every row matches, 1 on an
empty window, 2 on a mismatch, 3 when the mart does not cover the window.

### Reading a validate run

```
=== 7745 auctions, 98669 solutions ===
winner set matches:         7740/7745
filtered-out set matches:   7738/7745
reference scores (ours):    7740/7745
reference scores (observed): 7745/7745
pick on observed kept set:  7745/7745
valuation failures:         0

filter differs from DB:     7/5211 multi-pair solutions (0.1343%)
multi-pair bracket:         must_filter=4882, must_keep=66, undetermined=263
filter difference cause:    model=2, proxy=5

every difference has a named cause — M1 gate met.
```

The two `observed` lines are the real checks. They hold the DB's own filter decisions fixed
and re-run a single step of the algorithm — winner picking, then reference scores — so no
approximation is in their path and both should be exact.

The other lines can differ from the DB, because the fairness filter compares solutions
**per token pair** and the per-pair split of a solution's score is not stored anywhere. The
filter compares user surplus on both sides instead. `bracket` bounds how much the unknown
split could possibly matter, and `cause` classifies each difference:

- `proxy` — the split genuinely decides it and cannot be known
- `model` — provably a deliberate consequence of filtering on surplus rather than score
- `bug` — no valid split explains it; the run exits 2

See [PLAN.md §4.1](PLAN.md#41-m1-result) for the full argument.

## Tests

```bash
uv run --extra dev pytest
```

168 tests, no DB access — `loo/winner_selection.py`, `loo/valuation.py`,
`loo/counterfactual.py`, `loo/rewards.py` and `loo/aggregate.py` take plain dataclasses
and files and hold no connection.

## Background

- **[PLAN.md](PLAN.md)** — the milestones, every design decision (D1–D15) in one
  table, and the measured results per milestone (§4.1, §5.1, §6.1, §7.1).
- **[docs/analytics-db.md](docs/analytics-db.md)** — DB connection, tables, join keys,
  coverage and lag, the wrong-native-price story, the USD rate source.
- **[docs/winner-selection.md](docs/winner-selection.md)** — the algorithm, Rust source
  map, score/surplus formulas.
- **[docs/rewards.md](docs/rewards.md)** — reward and cap formulas, and why the cap is
  hard counterfactually.
