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
| `--pair-proxy {scaled,raw}` | how a multi-pair solution's score is split across pairs |

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
winner set matches:         2654/2675
filtered-out set matches:   2654/2675
reference scores (observed): 2675/2675
pick on observed kept set:  2675/2675
valuation failures:         0
filter proxy error:         78/1905 multi-pair solutions (4.0945%)
multi-pair bracket:         must_filter=1465, must_keep=19, undetermined=421
filter difference cause:    proxy=78

every difference has a named cause — M1 gate met.
```

The two `observed` lines are the real checks: they hold the DB's own filter decisions
fixed and re-run a single step of the algorithm, so no approximation is in their path.
The other lines will differ from the DB, because the per-pair score split the fairness
filter compares is not stored anywhere and has to be approximated. `bracket` bounds how
much that can matter and `cause` says whether each difference is the approximation
(`proxy`) or a real defect (`bug`) — see
[PLAN.md §4.1](PLAN.md#41-m1-result).
