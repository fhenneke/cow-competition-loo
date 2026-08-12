# Leave-one-out solver analysis

Counterfactual analysis of the CoW Protocol solver competition: remove one solver from
past auctions, re-run winner selection, and measure the impact on user surplus, solver
rewards and order coverage.

Orientation: **[PLAN.md](PLAN.md)** for the work, **[docs/](docs/)** for the DB and
protocol background.

## Setup

`uv` handles the environment; there is nothing to install by hand.

Put the analytics DB credentials in `.env` (gitignored):

```
ANALYTICS_DB_URL=user:password@host:port
```

No scheme and no database name — the database is derived from `--network`.

## Run

Two commands. `validate` reproduces the recorded competition and accounts for every
difference — it is the gate the counterfactual rests on. `analyse` is the counterfactual
itself.

```bash
uv run loo analyse --solver Sector --start 2026-08-01 --end 2026-08-04
```

Removes one solver from every auction in the window, re-runs winner selection, and reports
what users would have gained or lost. `--solver` takes a name or a submission address,
matched exactly — `Arc` and `Arctic` are different solvers and both compete.

### The outcome rule

A winner that never won for real never settled either, so the counterfactual has to decide
what its orders do. That decision is worth several ETH — 14.9% of winning solutions never
settled and they carry half of all winning score — so the rule is explicit:

- `inherited` (**default**) — settlement belongs to the **slot**, not the solver. A
  replacement inherits the outcome of the recorded winner that held its token pairs, so a
  batch that really reverted stays reverted whoever replaces it. Settlement cancels out of
  Δsurplus and what is left is the competition's *decision*.
- `observed` — settlement belongs to the **solution**. A replacement has no record, so it is
  assumed to settle. This charges the baseline for real reverts while assuming the
  counterfactual never reverts, so it is a **lower bound**, provably below `inherited`.
- `assume-settled` — every winner settles on both sides, so failures are ignored entirely.
  Normally the upper bound.

Report the default and quote the bound you care about; for Sector the three give 8.01, 2.89
and 8.13 ETH. See [PLAN.md §5.1](PLAN.md#51-m2-result).

Two things the rule names do **not** vary, both measured facts
([details](docs/analytics-db.md#observed-outcomes-what-actually-settled)): executed
amounts are always the **proposed** ones — a batch that lands executes its proposal
exactly, to the atom, so the only on-chain input the pipeline has is the settlement
status — and "settled" means landed **in time**, so the real surplus of late batches is
reported as `of which merely late` rather than absorbed.

```bash
uv run loo analyse --solver Sector --start 2026-08-01 --end 2026-08-04 --outcome-rule observed
```

| flag | |
| --- | --- |
| `--outcome-rule` | `inherited` (default), `observed` or `assume-settled` — see above |
| `--mode` | `score` (default) ranks on recorded scores; `surplus` ranks on user surplus |
| `--limit N` | only the first N auctions — start here |
| `--out report.json` | per-auction records, including both sides' reference scores |

Exit code is 0 normally, 1 if the window has no auctions, 2 if any auction could not be
valued, 4 if `--solver` did not resolve, 5 if the settlement source does not cover the
window.

### Validate

```bash
uv run loo validate --start 2026-08-01 --end 2026-08-02
```

`--start` is inclusive, `--end` exclusive. Exit code is 0 when every difference has a
named cause, 1 if the window has no auctions, 2 when something is unexplained, 3 on a bad
cross-check window.

Useful flags:

| flag | |
| --- | --- |
| `--limit N` | only the first N auctions — start here, a full day is ~2,700 |
| `--cross-check-surplus` | also diff per-order surplus against the dbt model |
| `--out report.json` | write the full report, including every disagreeing auction |
| `--network` | defaults to `mainnet`; see `loo/db.py` for the rest |

A ~2,700-auction day takes about five minutes.

`--cross-check-surplus` needs `int_backend_data__proposed_solution_data`, which lags the
raw tables by over a week. It fails with a clear message rather than checking nothing:

```bash
uv run python -c "from loo import db; c = db.connect('mainnet'); print(db.fetch(c, 'select max(auction_id) from dbt.int_backend_data__proposed_solution_data'))"
```

Everything else reads the staging tables and works up to the present.

## Tests

```bash
uv run --extra dev pytest
```

109 tests, no DB access — `loo/winner_selection.py`, `loo/valuation.py` and
`loo/counterfactual.py` take plain dataclasses and hold no connection.

## Reading a run

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
