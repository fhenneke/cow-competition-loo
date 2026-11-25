# Leave-one-out solver analysis

Counterfactual analysis of the CoW Protocol solver competition: remove one solver — or
several at once — from past auctions, re-run winner selection, and measure the impact on
user surplus, protocol fee revenue, solver rewards, order coverage and, optionally, the
quote competition.

`loo` replays only the mechanism live since **2026-10-28**; an earlier window exits 4
([why](docs/winner-selection.md#loo-replays-the-current-mechanism-only)). Windows are
dates, `--start` inclusive and `--end` exclusive.

## Setup

`uv` handles the environment. Put credentials in `.env` (gitignored):

```
ANALYTICS_DB_URL=user:password@host:port      # no scheme, no database name
CLICKHOUSE_URL=…                              # only for --quotes
CLICKHOUSE_USER=…
CLICKHOUSE_PASSWORD=…
```

The database is derived from `--network` (default `mainnet`). Details and traps:
[docs/analytics-db.md](docs/analytics-db.md).

## The flow

```bash
# 1. the counterfactual — one extraction pass shared by every scenario and rule
caffeinate -i uv run loo analyse --solver Fractal --solver Sector \
    --start 2026-11-17 --end 2026-11-20 \
    --outcome-rule inherited --outcome-rule assume-settled \
    --out "out/{solver}-{rule}.json"

# 2. the comparison table — seconds, reads the JSONs (plus one DB query for USD)
uv run loo compare out/*.json

# 3. the notebook — concentration curve, per-auction distributions, rule sensitivity
uv run --extra notebook jupyter lab notebooks/analysis.ipynb
```

On a new network, a new kind of window or after a code change, run the
[validation gates](#validate-and-validate-rewards) first: the counterfactual is only as
good as its reproduction of the recorded competition.

## Commands

### analyse

Removes solvers from every auction in the window, re-runs winner selection, and reports
the deltas.

| flag | |
| --- | --- |
| `--solver` | a name or submission address, matched **exactly** (`Arc` is not `Arctic`); all rotated addresses of a name go together. Repeatable. A comma-separated entry `"A,B"` is **one joint** removal: all absent at once, nothing attributed to members, and not the sum of the single removals |
| `--all-solvers` | one scenario per solver that bid in the window (rotated addresses together, uncatalogued ones by address); excludes `--solver`, needs `{solver}` in `--out` |
| `--outcome-rule` | `inherited` (default) or `assume-settled`, repeatable; see [below](#the-outcome-rule) |
| `--mode` | `score` (default) ranks on recorded scores; `surplus` ranks on user surplus — a separate user-value question, not an approximation of the competition |
| `--quotes` | also remove the solver from the quote competition ([below](#the-quote-side)) |
| `--quote-ranking` | `effective-sell` (default, prod's rule) or `autopilot` (the cross-check) |
| `--include-price-suspects` | keep price-suspect auctions in the statistics |
| `--limit N` | only the first N auctions — start here |
| `--out FILE` | the report JSON; `{solver}` becomes each solver's slug, `{rule}` the rule, each required when several values are given |

A named solver (or group member) that bids in no analysed auction is an error, since its
all-zero deltas would read as "costs nothing"; under `--all-solvers` it is reported and
skipped.

Exit codes: 0 ok; 1 no auctions in the window; 2 an auction could not be valued; 4 a
solver did not resolve, inconsistent flags, a named solver absent, or a window before the
replayed mechanism; 5 the settlement source does not cover the window; 6 the DB
connection was lost or a statement passed the 30-minute timeout.

From Python the pipeline is one call, which the CLI renders:

```python
from loo import db, run

conn = db.connect("mainnet")
window = run.analyse_window(conn, ["Fractal", "Fractal,Sector"], "2026-11-17", "2026-11-20")
for solver_run in window.runs:
    print(solver_run.solver, solver_run.analysis.delta_surplus)
```

#### Runtime

The run waits on the analytics DB for ~96% of its time, so runtime is the DB's and varies
2x or more by day: one mainnet day took 42 s of wall time and 1.7 s of CPU for one solver
(2026-09-15). A month of mainnet with every solver removed in turn has taken 12–25
minutes on different days, two outcome rules about 1.4x one. Arbitration adds ~8 s of CPU
per (solver, rule) per month. Measure before promising: `--limit 3000` over one day gives
the DB's current rate.

#### Long runs go under `caffeinate -i`

The server drops a client silent for about six minutes, so a Mac that sleeps loses the
run. `loo` notices within about two minutes and exits 6 with `lost the connection to the
analytics DB … This is the network, not the data`: rerun unchanged. `caffeinate -i`
prevents idle sleep for the command's duration (closing the lid still sleeps)
([details](docs/analytics-db.md#dead-connections-and-long-statements)).

### compare

```bash
uv run loo compare out/*.json [--summary] [--markdown] [--out FILE] [--skip-usd]
```

One column per solver-window, the `assume-settled` scenario beside the `inherited`
headline. `--summary` turns it around for many solvers: one row each, sorted by
Δsurplus, with win share, Δsurplus and orders lost, then three groups, each a
contribution with bids fixed beside the reward paid for that kind of work: winning
(Δscore, performance rewards), second-best bids (runner-up auctions, Δother winners'
capped rewards, consistency rewards) and quotes (Δquote, quote rewards; needs
`--quotes`). Quote rewards pay for accuracy, not for winning, so the quote pair sits
side by side without one paying for the other. Its amounts are in USD when every window
has rates, else native. Give every rule's run of a solver-window together; a group
without `inherited` is refused. Reports carry a format marker and older ones are refused with "re-run
analyse" — files are cheap to regenerate, a misread one is not. `--skip-usd` needs no DB.
USD needs one rate per auction with a recorded reward, which over a week is most of its
auctions: 11,083 mainnet auctions took 16 s cold and 3 s warm (2026-11-25).

### validate and validate-rewards

```bash
uv run loo validate --start 2026-11-18 --end 2026-11-19
uv run loo validate-rewards --start 2026-11-18 --end 2026-11-19
```

`validate` reproduces the recorded competition — winners, fairness filter, reference
scores — and names a cause for every difference. Exit 0 when every difference has a
named cause, 1 no auctions, 2 something unexplained, 3 a bad cross-check window, 6 DB.
`--cross-check-surplus` also diffs per-order surplus against
`int_backend_data__proposed_solution_data`; that model lags the raw tables by days, so it
fails loudly on an uncovered window rather than checking nothing.

`validate-rewards` recomputes every winner's uncapped and capped reward from the mart's
own inputs and diffs `fct_solver_rewards_per_auction` row by row. Nothing on that path is
approximated, so anything but an exact match exits 2 (1 empty window, 3 mart does not
cover the window, 6 DB). It has no mechanism cut-off.

## Reading a run

### Signs and denominators

Every delta is **counterfactual − actual** (without minus with): negative Δsurplus means
users would have received less, negative Δprotocol fee that the protocol would have
collected less, positive Δrewards that it would have paid more. The convention is printed
with every table and stamped into the JSON.

Auctions the solver never bid in stay in every denominator, so rates describe the window.
"Of N compared" is relative to the solver's own auctions and overstates window-level
dependence for a selective bidder, so coverage also has a window-level share: only-with
executions over every user-order execution the recorded winners traded, partially
fillable orders included.

### The outcome rule

A winner that never won for real never settled either, and 14.9% of winners never settle
while carrying half of all winning score
([measured](docs/analytics-db.md#observed-outcomes-what-actually-settled)), so the rule is
an explicit choice:

- `inherited` (**default**) — settlement belongs to the **slot**: a replacement inherits
  the recorded outcome of the token pairs it claims, so settlement cancels out and what
  is left is the competition's decision. The one scenario grounded in the record.
- `assume-settled` — everything lands in time on both sides; proposals alone.

Executed amounts are always the proposed ones (a settled proposal is exact to the atom),
and "settled" means **in time**, so late batches' real surplus is reported as `of which
merely late` rather than absorbed.

### Surplus and price

Beside the absolute Δsurplus, `compare` gives the price change in bps on the fill-or-kill
orders that trade on both sides and whose surplus moved (median moved order beside it),
and the same Δsurplus spread over the window's whole executed fill-or-kill volume, so
"when a price moves" and "the average traded unit" are both answered under their own
labels. Partially fillable orders are excluded from these, since their sides can execute
different amounts. The per-order ratio is immune to a wrong native price, which cancels.

An order executed **only without** the removed solver is real: a single-order winner can
block a whole batch ([why](docs/winner-selection.md#a-blocked-batch-keeps-orders-unexecuted)).

### Protocol fees

| row | meaning |
| --- | --- |
| **Δprotocol and partner fees** | the gross amount the mechanism charges — the headline |
| Δpartner fee | passed straight through to the app that declared it |
| **Δprotocol fee** | what the protocol keeps |

Partner fees are volume policies: they survive a rival winning the same order and are
lost only when the order stops trading. On mainnet they were most of the fee *level* (33.9
of 64.7 ETH over 2026-08-01..04) but almost none of its movement, while on chains where a
removal costs coverage they move with the orders (74% of Baseline's polygon fee delta,
July 2026); hence the gross headline with the split one tier down. Fee deltas run over the
union of orders either side traded, so an order that stops trading contributes its whole
fee. Δprotocol fee is gross revenue and belongs beside Δrewards, not netted against it:
fees are exact and uncapped rewards are not the payout. Auctions whose executed orders
lack fee policies are dropped from both sides and counted
([rule](docs/analytics-db.md#fee-policies-cover-every-order-of-every-kept-solution)).

### Rewards (score mode)

- **Uncapped** — the mechanism's exact accounting; not a payout (a failed win is charged
  `−reference_score` uncapped against a real CIP-87 penalty of a median 0.000069 ETH,
  mainnet 2026-10-12).
- **Capped (estimate)** — each reward clamped into the caps a replacement inherits from
  the slot it displaced; a winner with nothing to inherit drops its auction from the
  capped sum and is counted
  ([details](docs/rewards.md#the-slot-inheritance-estimate-and-why-the-caps-cannot-be-skipped)).
- **Δconsistency budget (estimate)** — each auction adds `upper cap − capped reward` to
  the week's consistency budget, which is paid out in full, so it offsets the capped
  delta over the same auctions; their sum is the change in summed upper caps
  ([details](docs/rewards.md#the-consistency-budget-offsets-the-capped-delta)).

Both convert native → COW per auction at the accounting-period rate, where snapshotted.

### Runner-up

`runner-up to another winner` counts the auctions where a losing bid of the solver's sets
another winner's reference score. `Δother winners' margin over their reference` is how
far those winners' margin over the reference (their reward before caps) moves without
the solver, every bid held fixed. It is counterfactual − actual, over winners that win
the same solutions on both sides, with rises and falls counted apart. Its bps are over
those winners' winning volume and are score bps (surplus under `--mode surplus`), not a
price change. That a winner could shade its bid by its margin is not modelled
([definition and limits](docs/winner-selection.md#runner-up-the-margin-a-losing-bid-leaves-the-winners)).

`Δother winners' rewards (capped)` is what the mechanism would pay those same winners
more without the solver, bids fixed. Their caps are the same on both sides, so the
consistency budget, which pays consistency rewards, lacks exactly that much: it is the
part of the pool the solver's losing bids moved from winners to consistency rewards, and
needs no assumption about how anyone would bid.

`above the reward cap, winners that settled` is the part of each winner's margin beyond
its upper cap, summed over the winners whose batch settled, counterfactual − actual.
Above its cap a winner's reward no longer depends on its bid, so this is score it could
withhold from users at no cost in reward; below the cap a worse bid costs it reward one
for one, and that reward comes out of the same weekly pool as the consistency budget
([why the cap splits it](docs/winner-selection.md#above-the-cap)).

`Δscore (Δsurplus + Δprotocol and partner fees)` is the sum of the two rows: what the
winning bids delivered to users, partners and the protocol, the fee leg over the
auctions whose fees are all priced. With bids fixed it is the solver's own marginal
contribution, the quantity the performance reward pays before caps.

### Rewards paid to the solver (recorded)

Below the net row, `compare` shows what the solver was paid in the window: performance
rewards per auction, and its consistency and quote rewards, each weekly reward split over
the window by the solver's own per-auction metric (relative surplus, quote reward share)
([decomposition](docs/rewards.md#rewards-paid-to-a-solver-recorded)). These are amounts of
record, unsigned, not counterfactual − actual:

- They cover **every auction in the window**, price-suspect and fast-path ones included,
  which Δsurplus excludes; `rewards paid ÷ −Δsurplus` divides one by the other as they
  are.
- They are **gross** of the solver service fee.
- A week not paid out yet has no consistency or quote figure, and an auction without a
  reward row no performance figure: the row says `incomplete: N auctions not yet
  accounted` rather than counting them as zero. On 2026-11-25 the latest paid week was
  2026-11-10..17.
- The consistency and quote pools are budgets tied to protocol revenue. Without the
  solver its share would go to the remaining solvers, so the rows say what the solver
  received, not what the protocol would save. The counterfactual quote reward is not
  evaluated.
- A warning names any week whose int table no longer sums to its mart's own total (one
  was rebuilt after the other); the split then follows the int table.

### Price sanity

An auction whose trades value differently through their two tokens by more than 2x is
**excluded from every statistic** and named; a wrong native price fabricates every number
it touches ([details](docs/analytics-db.md#native-prices-can-be-plain-wrong)). A pair
mispriced by the same factor passes: check valued volumes for absurd figures.

### USD

Display only, each auction at its own stablecoin-implied rate, because one window rate
misstates long windows on volatile chains
([source](docs/analytics-db.md#no-usd-prices--stablecoin-native-prices-imply-the-rate)).

### The quote side

`--quotes` re-runs each linked order's quote competition without the solver and reports
`window_bps` (quote degradation over executed quoted volume), `no_quote` (orders that
would not exist), and the price-improvement fee recharged against the replacement quote.
That recharge is its own figure and is **not** in Δprotocol fee, which uses the recorded
quotes on both sides. Executions and ranking stay as recorded, checked per auction. Every
run reports its pick reproduction and link rates; a drop means a rule change or a missed
tie-break. Quote bps are heavy-tailed: read the median and the ⚠ on a largest-order share
of 30% or more. See [docs/quotes.md](docs/quotes.md).

### Reading a validate run

```
=== 4850 auctions, 88532 solutions ===
winner set matches:         4850/4850
filtered-out set matches:   4850/4850
reference scores (ours):    4850/4850
reference scores (observed): 4850/4850
pick on observed kept set:  4850/4850
valuation failures:         0
filter differs from DB:     0/… multi-pair solutions
multi-pair bracket:         must_filter=…, must_keep=…, undetermined=…
filter verdict by:          score=…, surplus=…
  of those, guesses that could have moved the winners: 0 auctions
```

(mainnet 2026-11-18, filter counts elided.) The `observed` lines re-run one step on the
DB's own kept set and must always be exact. The filter lines are end to end: the per-pair
score split is recomputed, surplus stands in as a lower bound where a pair is dark, and
`filter verdict by` counts the guesses. Causes `proxy`, `model` and `bug` are defined in
[docs/winner-selection.md](docs/winner-selection.md#what-validate-checks-and-how-it-names-a-difference);
a `bug` exits 2, and a `SCORE ANCHOR FAILURES` line means the fee path is wrong.

## Tests, lint, types

`uv run pytest` runs the deterministic simulation suite in `tests/`, which takes a seed;
no DB access is needed. `uv run ruff check .` and `uv run pyright` (strict) lint and
type-check; dev tooling installs with `uv sync --extra dev`.

## Background

- [docs/analytics-db.md](docs/analytics-db.md) — DB connection, tables, join keys,
  prices, fees, settlement, solver names, query cost.
- [docs/winner-selection.md](docs/winner-selection.md) — the algorithm, Rust source map,
  the score-split filter, the fee port, counterfactual rules.
- [docs/rewards.md](docs/rewards.md) — reward and cap formulas, the capped estimate, the
  rewards paid.
- [docs/quotes.md](docs/quotes.md) — the quote competition and the quote side.
