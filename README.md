# Leave-one-out solver analysis

Counterfactual analysis of the CoW Protocol solver competition: remove one solver —
or several at once — from past auctions, re-run winner selection, and measure the
impact on user surplus, protocol fee revenue, solver rewards and order coverage.

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
negative Δsurplus means users would have received less, negative Δprotocol fee means the
protocol would have collected less, and positive Δrewards means it would have paid
more. The tables state this convention with the numbers.

**1. The counterfactual**, one invocation. `--solver` and `--outcome-rule` are both
repeatable and everything shares one extraction pass — extraction is nearly all of the
run time, so analysing five solvers under both rules costs barely more than one solver
under one. Only `inherited` is required; `assume-settled` is the optional
everything-lands-in-time scenario (what the rules mean: [below](#the-outcome-rule)):

```bash
uv run loo analyse --solver Fractal --solver Sector \
    --start 2026-08-01 --end 2026-08-04 \
    --outcome-rule inherited --outcome-rule assume-settled \
    --out "out/{solver}-{rule}.json"
```

The `{solver}` placeholder in `--out` becomes each solver's slug and `{rule}` the
outcome rule; each is required when several of its values are given.

A comma-separated `--solver` removes those solvers **together**:
`--solver "Fractal,Sector"` is one counterfactual with both absent at once, not two
runs. The deltas describe the joint scenario and are deliberately not attributed to
individual members — and the single removals do not sum to the joint one, because
removed solvers stop replacing each other. Mixing forms compares the two directly:
`--solver Fractal --solver "Fractal,Sector"`.

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

Removes one or more solvers from every auction in the window, re-runs winner selection,
and reports what users would have gained or lost. `--solver` takes a name or a
submission address, matched exactly — `Arc` and `Arctic` are different solvers and both
compete — or a comma-separated list of them, which is removed **jointly** as one
counterfactual (all absent at once, nothing attributed to individual members). It may
be repeated; the scenarios share one extraction pass and each gets its own report. If
a group member ends up bidding in none of the analysed auctions the run errors rather
than passing a smaller group's numbers off under the full label.

**`--all-solvers`** removes every solver that bid in the window, one at a time, without
naming any of them:

```bash
uv run loo analyse --all-solvers --start 2026-07-01 --end 2026-08-01 --out "reports/{solver}.json"
```

The list comes from the window, not the catalogue: one scenario per catalogue name — with
that name's rotated addresses removed together, as `--solver` would — and one per
uncatalogued address, named by the address. It is mutually exclusive with `--solver` and
needs the `{solver}` placeholder in `--out`.

One semantic differs from `--solver`, deliberately. A named solver that ends up bidding in
no analysed auction is an error, because its all-zero deltas would read as "removing this
solver costs nothing". Under `--all-solvers` nobody named it — the price-suspect exclusion
or `--limit` simply took it out — so it is reported and skipped, and the run still exits 0.

| flag | |
| --- | --- |
| `--all-solvers` | one scenario per solver that bid in the window; excludes `--solver` |
| `--outcome-rule` | `inherited` (default) or `assume-settled`, repeatable — all rules share the extraction pass; see below |
| `--mode` | `score` (default) ranks on recorded scores; `surplus` ranks on user surplus |
| `--limit N` | only the first N auctions — start here |
| `--out report.json` | per-auction records, including both sides' reference scores and rewards; `{solver}` in the path becomes each solver's slug, `{rule}` the outcome rule |

Exit code is 0 normally, 1 if the window has no auctions, 2 if any auction could not be
valued, 4 if a `--solver` did not resolve (or the flags are inconsistent, or a named
solver bid in no analysed auction), 5 if the settlement source does not cover the window.

A month of mainnet with every solver removed in turn — 84,767 auctions, 26 scenarios —
takes about 12 minutes, nearly all of it the single shared extraction pass.

From Python, the same pipeline is one call — the CLI is a rendering around it:

```python
from loo import db, run

conn = db.connect("mainnet")
window = run.analyse_window(conn, ["Fractal", "Sector"], "2026-08-01", "2026-08-04")
for solver_run in window.runs:
    print(solver_run.solver, solver_run.analysis.delta_surplus)
```

An entry `"Fractal,Sector"` would instead remove both in one joint counterfactual.

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
their real outcomes) was carried for a while as a "lower bound" and was removed: settlement
attached to the solution is not a counterfactual anyone would defend, and a number
nobody should quote is not made useful by calling it a bound. See
[PLAN.md §7.1](PLAN.md#71-m4-result).

Two things the rule names do **not** vary, both measured facts
([details](docs/analytics-db.md#observed-outcomes-what-actually-settled)): executed
amounts are always the **proposed** ones — a batch that lands executes its proposal
exactly, to the atom, so the only on-chain input the pipeline has is the settlement
status — and "settled" means landed **in time**, so the real surplus of late batches is
reported as `of which merely late` rather than absorbed.

#### Protocol fees

`analyse` reports the fee the winning solutions charged on each side, split into the
partners' share and the protocol's own revenue:

| row | meaning |
| --- | --- |
| **Δprotocol and partner fees** | the gross amount the mechanism charges |
| Δpartner fee | passed straight through to the app that declared it |
| **Δprotocol fee** | what the protocol keeps |

Over 2026-08-01..04 on mainnet partner fees were the larger half of the gross — 33.9 ETH
partner against 30.8 ETH protocol — so reading the gross figure as protocol revenue
roughly doubles it.

**The split is not a fixed fraction, so all three rows are reported.** A partner fee is a
volume policy, so it survives a rival winning the same order and is lost only when the
order stops trading. The partners' share of the *delta* therefore tracks how much of a
removal's cost is coverage rather than price, and it ranges from ~0% (Baseline on xdai
and plasma, July 2026) to 74% (Baseline on polygon).

**A naming warning.** The analytics DB and `arbitrator.rs` both use "protocol fee" for
the *gross* amount, and spell the net one `protocol_fee_without_partner_fee_native`.
This tool follows budget usage instead: "protocol fee" is what the protocol keeps, and
the gross quantity is named for what it contains. `loo/fees.py` carries the mapping.

Δprotocol fee is the direct value of a solver *to the protocol*, where Δsurplus is its
value to users. It is **gross** revenue, so it belongs beside Δrewards rather than
instead of it:
the protocol also pays for the competition, and the two usually move in opposite
directions. They are deliberately not netted into one number — fees are exact and
uncapped rewards are not the payout, and combining them would hide that.

Fees come from `stg_backend_data__fee_policies`, which covers every auction order of
every non-filtered-out solution, so they are recomputable for essentially every solution
the counterfactual prices. An auction where some executed order's policies are
unrecorded is dropped from **both** sides of the fee sum and counted separately, so the
delta stays like-for-like; that needs a solution the recorded competition filtered out to
win the counterfactual, and runs at a fraction of a percent of auctions.

#### Rewards

In score mode `analyse` reports solver rewards on both sides, twice:

- **Uncapped** — the mechanism's exact accounting. The removed solver's own reward
  drops out, and rivals' rewards grow because their reference scores fall without it.
  Not a payout: a failed settlement's uncapped penalty is `-reference_score` against a
  real floor of −0.01 ETH, and over the 2026-08-01..04 window uncapped rewards sum to −410 ETH
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
from every statistic** — the report names the excluded auction ids (0.6% of the
2026-08-01..04 window, which carried 82% of Sector's Δsurplus, all fabricated).
`--include-price-suspects` keeps them in instead; the ids are printed either way.

### compare

```bash
uv run loo compare out/*.json
```

Aggregates `analyse --out` reports into the comparison: one column per solver-window,
the `assume-settled` scenario beside the `inherited` headline, medians, the sign split
and the largest auction's share beside every sum, and the caveats and sign convention
attached. Give every outcome-rule run of a solver-window together; a group without an
`inherited` run is refused rather than silently led by another rule. Reports from an
older `analyse` (the format marker is checked) are refused with "re-run analyse" —
the files are cheap to regenerate and a silently misread one is not.

Beside the absolute Δsurplus, the table reads the price-movement slice in relative
terms: on the fill-or-kill orders that trade on both sides and whose surplus moved,
the volume-weighted price change in basis points of the baseline received value, with
the median moved order beside it. Partially fillable orders are excluded — their two
sides can execute different amounts, which would mix quantity into a price figure —
and within an order the ratio is immune to a wrong native price, since surplus and
volume are valued through the same buy-token price, which cancels. A companion row
averages the same Δsurplus over the window's whole executed fill-or-kill volume (the
recorded winners' executions, every analysed auction), so unmoved orders count as
zero price change: "when a price moves" and "the average traded unit" are different
questions, and the table answers both under their own labels.

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
winner set matches:         7745/7745
filtered-out set matches:   7745/7745
reference scores (ours):    7745/7745
reference scores (observed): 7745/7745
pick on observed kept set:  7745/7745
valuation failures:         0

filter differs from DB:     0/5211 multi-pair solutions (0.0000%)
multi-pair bracket:         must_filter=4882, must_keep=66, undetermined=263
filter verdict by:          score=5208, surplus=3  (surplus fallback 0.0576%)
  of those, guesses that could have moved the winners: 1 auctions

no disagreements.

every difference has a named cause — the validation gate is met.
```

The two `observed` lines hold the DB's own filter decisions fixed and re-run a single step
of the algorithm — winner picking, then reference scores — so no approximation is in their
path and both should always be exact.

The other lines are the whole mechanism run end to end, and on this window they are exact
too. They *can* differ, because the fairness filter compares solutions **per token pair**
and that split of a solution's score is not stored anywhere. It is recomputed from the
recorded fee policies and anchored on the recorded score. Baselines are never approximated
— a single-pair solution's whole score sits on its one pair — so only the left side of a
comparison can be missing, and there user surplus stands in as a *lower bound*: clearing
the baseline on surplus alone proves the pair fair, and failing it is the only guess the
filter makes. `filter verdict by` counts those guesses — 3 here, none of which changed a
decision — and the line under it counts the auctions where one *could* have, by restoring
every guess and re-picking the winners. A zero there is a proof that no guess mattered; in
`analyse`, where there is no recorded outcome to compare against, it is the only handle on
the filter's residual uncertainty. `bracket` bounds how much an unknown split could possibly matter, and `cause`
classifies any difference:

- `proxy` — a guess, where the split genuinely decides and cannot be known
- `model` — a guess, provably a deliberate consequence of comparing surplus
- `bug` — no valid split explains it, **or** every pair was decided by a proof and it still
  differs; the run exits 2

A `SCORE ANCHOR FAILURES` line appears only when the recomputed fees contradict a recorded
score. It should never appear; it means the fee path is wrong, not that fee coverage is
thin.

See [PLAN.md §15](PLAN.md#15-m14--fee-aware-scoring--done) for the argument, and
[§4.1](PLAN.md#41-m1-result) for what the filter looked like before the split was
recomputed.

## Tests, lint, types

```bash
uv run --extra dev pytest
uv run ruff check .
uv run pyright
```

The tests need no DB access — `loo/winner_selection.py`, `loo/valuation.py`,
`loo/counterfactual.py`, `loo/rewards.py` and `loo/aggregate.py` take plain dataclasses
and files and hold no connection. Pyright runs in strict mode; both tools are
configured in `pyproject.toml` and install with `uv sync --extra dev`.

## Background

- **[PLAN.md](PLAN.md)** — the milestones, every design decision (D1–D15) in one
  table, and the measured results per milestone (§4.1, §5.1, §6.1, §7.1).
- **[docs/analytics-db.md](docs/analytics-db.md)** — DB connection, tables, join keys,
  coverage and lag, the wrong-native-price story, the USD rate source.
- **[docs/winner-selection.md](docs/winner-selection.md)** — the algorithm, Rust source
  map, score/surplus formulas.
- **[docs/rewards.md](docs/rewards.md)** — reward and cap formulas, and why the cap is
  hard counterfactually.
