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

M1 ships one command: reproduce the recorded competition over a date window and account
for every difference.

```bash
uv run loo validate --start 2026-08-01 --end 2026-08-02
```

`--start` is inclusive, `--end` exclusive. Exit code is 0 when every difference has a
named cause, 2 when something is unexplained, 3 on a bad cross-check window.

Useful flags:

| flag | |
| --- | --- |
| `--limit N` | only the first N auctions — start here, a full day is ~2,700 |
| `--cross-check-surplus` | also diff per-order surplus against the dbt model |
| `--out report.json` | write the full report, including every disagreeing auction |
| `--network` | defaults to `mainnet`; see `loo/db.py` for the rest |
| `--pair-proxy {surplus,scaled,raw}` | what the fairness filter compares per token pair |

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

71 tests, no DB access — `loo/winner_selection.py` and `loo/valuation.py` are pure.

## Reading a run

```
winner set matches:         2670/2675
filtered-out set matches:   2668/2675
reference scores (observed): 2675/2675
pick on observed kept set:  2675/2675
valuation failures:         0
filter differs from DB:     7/1905 multi-pair solutions (0.3675%)
multi-pair bracket:         must_filter=1465, must_keep=19, undetermined=421
filter difference cause:    proxy=7

every difference has a named cause — M1 gate met.
```

The two `observed` lines are the real checks. They hold the DB's own filter decisions fixed
and re-run a single step of the algorithm — winner picking, then reference scores — so no
approximation is in their path and both should be exact.

The other lines can differ from the DB, because the fairness filter compares solutions
**per token pair** and the per-pair split of a solution's score is not stored anywhere.
`--pair-proxy` chooses what to compare instead; the default values every pair by user
surplus on both sides of the comparison. `bracket` bounds how much the unknown split could
possibly matter, and `cause` classifies each difference:

- `proxy` — the split genuinely decides it and cannot be known
- `model` — a deliberate consequence of filtering on surplus rather than score
- `bug` — no valid split explains it; the run exits 2

See [PLAN.md §4.1](PLAN.md#41-m1-result) for the full argument.
