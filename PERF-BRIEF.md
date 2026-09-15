# Brief: speed up `loo analyse`

> **Correction, 2026-09-15.** The "85% of wall time is Python" conclusion below is wrong.
> A one-solver run over one mainnet day (2,958 auctions) measured 42 s of wall time and
> **1.7 s of CPU** — the process waits on the DB for ~96% of the run — and an
> all-solvers run of the same day (25 scenarios × 2 rules) took the same 34 s of
> extraction plus 16 s of CPU in total. The SQL profile below was taken warm and
> subtracted from a cold run's wall time, which is where the gap came from. What was
> real: `analyse_auction` does recompute the valuation and baseline arbitration per
> (scenario, rule) — about 8 s of CPU per run per month, ~7 minutes for 26 solvers under
> both rules — and `ALL_SOLVERS_SQL` / `SOLVER_SQL` were planned as sequential scans of
> `proposed_solutions`, now fixed by bounding them to the window's auction-id range
> ([docs](docs/analytics-db.md#bound-joins-against-proposed_solutions-to-the-windows-auction-id-range)).
> The extraction cache remains the one structural win; per-query micro-optimisation is
> still aimed at the wrong place, but for the opposite reason to the one given below.

Context for an agent picking up extraction/analysis performance. Written 2026-08-29 on
branch `m13-protocol-fee-delta`, after a full-month × 10-network rerun made the runtime
the binding constraint on how often the shared artifacts can be refreshed.

## The problem, sized

One month of mainnet is ~78,000 auctions. A three-day mainnet window (7,745 auctions,
3 solvers) takes **~5 minutes**, so a month is **~50 minutes per network**, and the
artifact matrix is 10 networks (23 solver-chain combinations, grouped into 10 `analyse`
invocations because a network's extraction pass is shared across its solvers).

## Measured: SQL is not the bottleneck

Timing every query of one 200-auction chunk on mainnet (`/tmp/profile_extract.py`, worth
re-creating — it just calls `fetch` on each `extract.*_SQL` in turn):

| query | seconds | share | rows |
| --- | --- | --- | --- |
| EXECUTIONS | 0.44 | 36.9% | 2,429 |
| SOLUTIONS | 0.18 | 15.2% | 2,289 |
| QUOTES | 0.17 | 14.0% | 255 |
| PARTNER_COUNTS | 0.12 | 9.8% | 63 |
| PRICES | 0.10 | 8.1% | 492 |
| FEE_POLICIES | 0.08 | 6.8% | 691 |
| AUCTIONS | 0.06 | 4.7% | 200 |
| REFERENCE_SCORES | 0.05 | 4.5% | 250 |
| **total** | **1.20** | | |

That is **6.0 s per 1,000 auctions**, so July mainnet is **~8 minutes of query time**
inside a ~50-minute run. **Roughly 85% of wall time is Python, not the database.** Any
work that starts by optimising SQL is aimed at the wrong 15%.

Do not take this as licence to hammer the DB with parallel queries either — see the
"don't" list below.

## Where to look first

The profile above stops at the chunk boundary; the Python side has **not** been profiled
yet. That is step one, and `/tmp/profile_run.py` is a starting point:

```python
import cProfile, pstats, sys
sys.path.insert(0, ".")
from loo.db import connect
from loo.run import analyse_window
p = cProfile.Profile(); p.enable()
analyse_window(connect("mainnet"), ["Rizzolver"], "2026-08-01", "2026-08-02", log=lambda m: None)
p.disable(); pstats.Stats(p).sort_stats("cumulative").print_stats(25)
```

Hypotheses worth testing, in the order I would test them:

1. **Row → dataclass conversion in `extract._build_bid`.** Every execution row builds an
   `Order` with six `as_int` calls, and `as_int` goes through `isinstance` checks on
   `Decimal`. ~2,400 rows per 200 auctions means ~1M `as_int` calls for a mainnet month.
   `psycopg2` can be told to return integers for `numeric` directly with a custom type
   caster, which would delete this layer rather than speed it up.
2. **Arbitration cost × solver count.** `analyse_auction` runs the whole arbitration
   twice (baseline and LOO) *per solver*, so a 3-solver run arbitrates 6× per auction.
   The **baseline side is identical for every solver** and is currently recomputed for
   each. Hoisting it out is a straight 25–40% cut on multi-solver runs and changes no
   number. Check `run.analyse_window`'s loop before assuming it is not already shared.
3. **`value_solution` per bid.** ~12 solutions per auction × 78,000 auctions, each doing
   integer maths plus (new in M13) the fee computation. The fee path adds a
   `policy_fees` walk per contributing order; `mul_f64` and `Fraction` in `quote_fee` are
   the suspicious parts. `quote_fee` results are **per order, not per auction** — cache
   them across auctions, since an order recurs in many.
4. **Chunk size.** `extract.CHUNK_SIZE = 200`. The `= any(array)` predicates are indexed
   on `auction_id`, so 1,000 would cut round-trips 5× — but per the table above that
   saves at most a couple of minutes, so treat it as cleanup, not a fix.

## Two structural wins, if the profile supports them

**Cache the extraction.** Iterating on the analysis (as M13 did — adding fee columns and
re-running everything) pays the full extraction cost each time to get identical rows. A
local cache keyed on `(network, auction_id range, extract schema version)`, written as
parquet or sqlite, makes the second run of a window nearly free. This is the biggest win
for how the tool is actually used, and it is independent of any micro-optimisation.

**Hoist the window-level loaders out of the per-solver loop.** `load_settlement_outcomes`,
`load_solution_caps`, `load_conversion_rates` and `load_usd_rates` are properties of the
window, not the solver. Confirm they are fetched once per invocation and not once per
solver.

## Don'ts

- **Do not parallelise queries against the analytics DB without asking.** It is shared,
  and a large `= any(array)` sweep against an unindexed dbt table already caused a
  DB CPU alert on 2026-08-14. See CLAUDE.md and
  [docs/analytics-db.md](docs/analytics-db.md#int_accounting_period_data__conversion_rates-is-per-block-and-unindexed).
  Process-level parallelism *across networks* is fine and is what the artifact rerun
  uses (`xargs -P 3`), because each network is a separate database.
- **Do not change any number.** This is a pure performance task. The gate is that
  `loo validate` still reproduces the recorded competition and that a re-run of an
  existing report is byte-identical apart from timing. `uv run pytest` (251 tests),
  `uv run ruff check .` and `uv run pyright` (strict) must stay green.
- **Do not edit `loo/` while a run is in flight** — a background `analyse` re-imports
  from disk and dies on a half-written module. A lost run costs five minutes and reads
  like a data problem (CLAUDE.md).
- Beware `jobs | wc -l` in a pipeline under zsh: it reads 0. Throttle with `xargs -P`.

## Where the code is

| what | where |
| --- | --- |
| chunked extraction, all SQL | `loo/extract.py` (`load_auctions`, `CHUNK_SIZE`) |
| row → dataclass | `loo/extract.py:_build_bid`, `loo/db.py:as_int` |
| per-auction arbitration | `loo/counterfactual.py:analyse_auction` |
| per-solver loop | `loo/run.py:analyse_window` |
| valuation + fees | `loo/valuation.py:value_solution`, `loo/fees.py:policy_fees` |
