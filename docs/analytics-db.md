# Analytics DB — connection, tables, join keys

Reference context. The concrete work is in [../PLAN.md](../PLAN.md).

## Connecting

`.env` holds `ANALYTICS_DB_URL` in the form `user:password@host:port` — **no URL
scheme and no database name**. `psycopg2.connect(url)` fails on it; parse it and pass
`dbname` explicitly.

There is no `psql` on this machine and `psycopg2` is not in the system Python. Use `uv`:

```bash
uv run --with psycopg2-binary python your_script.py
```

```python
import os, psycopg2
from dotenv import load_dotenv

load_dotenv()
creds, hostport = os.environ["ANALYTICS_DB_URL"].split("@")
user, password = creds.split(":", 1)
host, port = hostport.split(":")

conn = psycopg2.connect(
    host=host, port=port, user=user, password=password,
    dbname="prod_mainnet",       # required — not in the URL
    connect_timeout=20,
)
```

The user is `<db-user>`. Treat the DB as read-only.

## Databases and schema

**One database per network.** Models live in schema `dbt` in each.

`prod_mainnet`, `prod_xdai`, `prod_base`, `prod_arbitrum-one`, `prod_avalanche`,
`prod_polygon`, `prod_bnb`, `prod_ink`, `prod_linea`, `prod_plasma`, `prod_lens`,
`prod_sepolia` — each with a `staging_*` counterpart.

`prod_mainnet` also carries personal dev schemas (`felix`, `bram`, `nitish`, …) and
`dbt_staging`. Use `dbt` unless you have a reason not to.

## Tables

Bids and their executions:

| Table | Grain | Notes |
| --- | --- | --- |
| `stg_backend_data__proposed_solutions` | solution | `auction_id, uid, id, solver, score, is_winner, filtered_out` |
| `stg_backend_data__proposed_trade_executions` | solution × order | `auction_id, solution_uid, order_uid, executed_sell, executed_buy` |
| `int_backend_data__proposed_solution_data` | solution × order | the two above joined, plus `sell_token, buy_token, bid_score, is_winner, winning_score, order_surplus_atoms_in_surplus_token` |

Auction context:

| Table | Notes |
| --- | --- |
| `stg_backend_data__competition_auctions` | `order_uids` (array — auction membership), `surplus_capturing_jit_order_owners`, `block_number`, `block_deadline` |
| `stg_backend_data__auction_prices` | `(auction_id, token) → price` — native prices |
| `stg_backend_data__orders` | `uid, kind` (buy/sell), `sell_amount`, `buy_amount`, `partially_fillable`, `owner`, `class` |
| `stg_backend_data__jit_orders` | same shape for JIT orders |
| `stg_backend_data__fee_policies` | `(auction_id, order_uid, application_order) → kind, factors` |

Outcomes and ground truth:

| Table | Notes |
| --- | --- |
| `stg_backend_data__reference_scores` | `(auction_id, solver) → reference_score` — winners only |
| `int_backend_data__solution_data` | **winning solutions only**: `score, reference_score, is_settled_in_time, upper_reward_cap, lower_reward_cap, is_excluded, block_deadline` |
| `fct_solver_rewards_per_auction` | `competition_score, observed_score, reference_score, uncapped_reward, batch_reward_native, upper_reward_cap` |
| `pre_stg__orders_per_auction_with_at_least_one_bid` | `(auction_id, block_deadline, order_uid)` — the canonical auction list |
| `stg_rpc_data__block_timestamp` | `block_number → time` |
| `dune_data__cow_protocol__solvers` | `address ↔ name`, `active`, `environment` |
| `reward_config` | per-network caps, `service_fee_factor`, reward token |

## Join keys

**`proposed_trade_executions.solution_uid` joins `proposed_solutions.uid`, not `.id`.**

`uid` is a per-auction 0-based index; `id` is the solver's own solution id. They collide
for a minority of rows, so joining on `id` returns plausible data for part of an auction
and nothing for the rest — a silent, partial failure. Verified on auction `13509867`:

```
uid | id      | rows matched via uid | via id
0   | 3295600 | 1                    | 0
2   | 11      | 1                    | 1     <- coincidental collision
```

Other joins are unsurprising: `orders.uid = order_uid`, `auction_prices` on
`(auction_id, token)`, `reference_scores` on `(auction_id, solver)`. Addresses and uids
are `bytea` — compare with `decode(...,'hex')` or `\x…` literals, and render with
`encode(col,'hex')`.

## `proposed_solutions.uid` encodes the ranking

`uid` is not an arbitrary index. The autopilot assigns it **best to worst**
(`services/crates/database/src/solver_competition_v2.rs:147`, "uids get assigned from
best to worst"), so ordering by `uid` reproduces the arbitrator's own output order:

```
[winners, by descending score] ++ [non-winners, by descending score] ++ [filtered_out]
```

Verified over 2,531 solutions (auctions 13509000–13509867): `uid` order matches that
ordering for every row, and filtered-out solutions come after every kept one in all 299
auctions that had any.

Two consequences worth having:

- The recorded order is the only surviving record of the autopilot's tie-breaks. It
  shuffles bids before sorting by score (`run_loop.rs:636`), so equal scores are split
  randomly; feeding solutions in `uid` order is the only way to reproduce it. 54 auctions
  in that same range had two solutions with identical non-zero scores.
- The `ranked` ordering is *not* plain score order, and reference scores depend on it —
  see [winner-selection.md](winner-selection.md#ranked-order-is-load-bearing).

Also note that solutions dropped by the arbitrator never reach the table: there are no
rows with `score = 0` or `score is null` (checked over auctions 13505000–13509867), which
is consistent with `arbitrator.rs:72` discarding zero-score solutions before ranking.
Every stored row therefore had a *successful* score computation — so a valuation that
fails while reproducing one is a bug in the reproduction, not a property of the data.

## Native prices

A price converts a token amount to the native token as

```
native_amount = token_amount * price / 1e18
```

matching `price_in_eth` (`services/crates/winner-selection/src/primitives.rs:20`). Prices
are per auction; there is no global price table.

## Coverage and lag

- `int_backend_data__proposed_solution_data` starts at auction `12709602` and **lags the
  raw staging tables badly and by a varying amount** — measured at **7.5 days** on
  2026-08-11: its frontier is auction `13509867`, block deadline timestamp
  2026-08-03 23:59 UTC, while `stg_backend_data__proposed_solutions` reaches `13563642`
  and block timestamps run to 2026-08-11 12:32 UTC. Measure the gap, do not assume it:

  ```sql
  select (select max(auction_id) from dbt.int_backend_data__proposed_solution_data) as int_max,
         (select max(auction_id) from dbt.stg_backend_data__proposed_solutions)      as stg_max
  ```

  For anything that must cover recent auctions, join
  `stg_backend_data__proposed_solutions` × `…__proposed_trade_executions` directly. The
  intermediate model also lacks `filtered_out`, so the staging join is the only route to
  the fairness-filter ground truth regardless of the window.

  **The lag is specific to that one model — it is not a property of `int_` models.**
  Measured on 2026-08-11, every other model on the path is level with staging:

  | model | `max(auction_id)` |
  | --- | --- |
  | `stg_backend_data__proposed_solutions` | 13565433 |
  | `stg_backend_data__settlements` | 13565433 |
  | `int_backend_data__winning_solutions_with_onchain_status` | 13565433 |
  | `int_backend_data__solution_data` | 13565433 |
  | `int_backend_data__trade_with_tx_hash` | 13565433 |
  | `int_backend_data__proposed_solution_data` | **13509867** |

  So check the specific model rather than avoiding intermediate models as a class.
- `stg_rpc_data__block_timestamp` covers block `24178030` onward (2026-01-06). Earlier
  windows need `stg_dune_data__block_timestamp`.
- Auction ids are sparse — do not assume consecutive integers.

## Mapping a date window to auctions

```sql
select distinct auction_id, block_deadline
from dbt.pre_stg__orders_per_auction_with_at_least_one_bid
where block_deadline between
      (select min(block_number) from dbt.stg_rpc_data__block_timestamp where time >= :start)
  and (select max(block_number) from dbt.stg_rpc_data__block_timestamp where time <  :end)
```

This is the same auction universe the dbt reward models use.

## `order_surplus_atoms_in_surplus_token` rounds

The surplus column of `int_backend_data__proposed_solution_data` uses the same formula as
the Rust — `executed_buy - ceil(executed_sell * buy_amount / sell_amount)` for sell orders
— but computes it in Postgres `numeric`, and **`/` on two integral numerics rounds the
quotient before `ceil()` ever sees it**. Postgres picks a result scale from the operands;
for amounts of this magnitude it comes out as 0, so the quotient is rounded half-up to an
integer and `ceil` becomes a no-op. Where the true fraction is in `(0, 0.5)` the model
lands one atom above the exact value.

Traced on auction `13488353`, solution `0`:

```
executed_sell 90888722591939552      buy_amount   105000028207312463683085
executed_buy  954600229483096975804  sell_amount  10000000000000000000

exact numerator      9543318435880250535850911543951604877920   (40 digits)
exact floor          954331843588025053585   remainder 911543951604877920  (frac ≈ 0.09)
exact ceil           954331843588025053586
postgres quotient    954331843588025053585   <- scale 0, already rounded down
postgres ceil        954331843588025053585   <- no-op

model surplus  268385895071922219      exact surplus  268385895071922218
```

Over three days of mainnet (2026-08-01 to 2026-08-04, 94,565 orders) **624 orders differ
and every single difference is exactly −1** — the exact value is always the smaller one,
and nothing else diverges at all. So the column is
fine for aggregate work and wrong by up to one atom per order for anything that has to
match the protocol. Reproduce integer ceiling division exactly (`-(-a // b)` in Python,
`div()` plus a remainder test in SQL) rather than `ceil(a / b)`.

## `competition_auctions` price arrays are empty

`price_tokens` and `price_values` on `stg_backend_data__competition_auctions` are `NULL`
in recent data, despite looking like a convenient denormalised copy. Use
`stg_backend_data__auction_prices`, which had 917 rows for auction `13509867` while the
array columns were null.

## Fee policies are incomplete

`stg_backend_data__fee_policies` **only has rows for orders that were executed**, even
though essentially every order carries one or more policies (only some JIT orders are
exempt). Consequences:

- It is not a test for "is this an auction order" — use `competition_auctions.order_uids`.
- Protocol fees cannot be recomputed for orders that were never executed. This is why
  solution scores are read from `proposed_solutions.score` rather than recomputed.
- Where policies are genuinely needed for a non-executed order, backfill from another
  auction in which the same `order_uid` was executed — policies derive from order class,
  app data, and the quote, so they are stable across auctions for a given order.

## Observed outcomes: what actually settled

Everything above describes what solvers *proposed*. Three tables carry what happened on
chain:

| Table | Grain | Notes |
| --- | --- | --- |
| `stg_backend_data__settlements` | settlement event | `(block_number, log_index, solver, tx_hash, auction_id, solution_uid)`. No row means no settlement. |
| `int_backend_data__winning_solutions_with_onchain_status` | **winning solution** | `(auction_id, solution_uid, solver, score, block_deadline, block_number, tx_hash, is_settled_in_time)` |
| `int_backend_data__trade_with_tx_hash` | auction × order | the actual on-chain `sell_amount`, `buy_amount`, `fee_amount` per traded order, plus `solution_uid` |

`…winning_solutions_with_onchain_status` is the one to use for "did this winner settle". It
**left-joins** settlements onto the winners, so every winning solution has a row and a
winner that never settled is a row with a null `tx_hash` rather than a missing row. That
distinction is what lets a *missing* row mean "no data" and fail loudly. Verified over
2026-08-01..2026-08-04:

```
winners                       10301
rows in the status model      10301   <- exact 1:1, no gaps
settled at all                 8768   (85.1%)
settled in time                8752
duplicate settlement rows         0
status rows for non-winners       0
```

### `settled` and `settled_in_time` come apart, and which one you want depends

```sql
is_settled_in_time = coalesce(block_number <= block_deadline and tx_hash is not null, false)
```

**16 winners in that window settled late.** Their orders *did* trade — all 16 have rows in
`int_backend_data__trade_with_tx_hash` — so the users got their fills, but the solver earns
no reward (`observed_score` is 0 without `is_settled_in_time`, see
[rewards.md](rewards.md)). So:

- a **surplus** question wants `tx_hash is not null` — did it execute at all;
- a **reward** question wants `is_settled_in_time`.

Using the reward flag for a surplus question silently deletes 16 auctions' worth of real
user surplus.

### Failures are concentrated in the biggest solutions

The 14.9% failure rate is not spread evenly, and anything that treats settlement as a small
correction will be badly wrong:

| | winners | Σ score | share of score |
| --- | --- | --- | --- |
| never settled | 1,533 (14.9%) | 439.6 ETH | **50.2%** |
| settled | 8,768 (85.1%) | 435.5 ETH | 49.8% |

**One seventh of the winners carry half the winning score and none of it executed.** The
largest single unsettled winner scored 143.6 ETH. So a counterfactual that assumes winners
settle roughly doubles the surplus on both sides of its comparison, and how it treats a
*replacement* winner — for which no settlement was ever recorded — moves the headline by
several ETH. The leave-one-out analysis therefore attaches settlement to the token pair
rather than to the solver, and reports two bounds either side of it
([PLAN.md §5](../PLAN.md#5-m2--loo-ranking-and-surplus-deltas)).

### A settled proposal is exact — the only divergence is settling at all

Over the same window, comparing `int_backend_data__trade_with_tx_hash` against
`stg_backend_data__proposed_trade_executions` on `(auction_id, solution_uid, order_uid)`:

```
order rows compared                                          9061
sell_amount = executed_sell and buy_amount = executed_buy    9061   <- all of them
trades with a non-zero fee_amount                               0
proposed orders of a settled winner missing from the trades      0
```

A solver commits its clearing prices in the settlement calldata, so **when a batch settles
the proposed execution *is* the observed one**, to the atom. The entire content of
"observed versus proposed" is the binary did-it-settle. A counterfactual therefore needs
only the settlement flag plus the proposed amounts — there is no separate observed-amount
lookup to build.

Two caveats on `trade_with_tx_hash`: it filters trades to orders in
`pre_stg__orders_per_auction_with_at_least_one_bid`, so **JIT orders are absent from it**;
and `fee_amount` on the `Trade` event has been 0 since fees moved into the clearing price
(`observed_fee` is marked `DEPRECATED after June 23, 2026` in
`int_backend_data__trade_data_unprocessed`).

## Resolving a solver name

`dune_data__cow_protocol__solvers` is the only bridge from a submission address to a human
name. `address` is **`bytea`**, so it joins `proposed_solutions.solver` directly with no
`encode`. 267 rows on mainnet, one per address, 67 distinct names. Per network: xdai 89,
base 98, only 10 addresses shared with mainnet.

Four traps, all of them live in the 2026-08-01..2026-08-04 window:

1. **Exact matching only.** `Arc` is a substring of `Arctic` and **both bid in the
   window**; `Quasi` ⊂ `Quasilabs`, `Sector` ⊂ `Sector_Finance`, `Tsolver` ⊂ `TestSolver1`.
   Any `ilike '%…%'` silently removes two competitors. Case-insensitive *exact* match is
   safe — there are no `lower(name)` collisions.
2. **One name, several addresses.** `Barter`, `Kipseli`, `Rizzolver` and `Tsolver` have
   rotated submission keys, and neither `environment = 'prod'` nor `active` disambiguates
   (Kipseli has three prod+active addresses). The ranges never overlap — no two same-name
   addresses ever bid in the same auction — so intersecting with "addresses that actually
   bid in the window" collapses it to one in practice. When a window straddles a rotation
   it genuinely is one competitor under two addresses and **all of them must be removed
   together**, or `compute_reference_scores` treats one half of the rotation as a rival of
   the other.
3. **`Uncatalogued` is not a solver.** The model does `coalesce(name, 'Uncatalogued')` and
   `coalesce(environment, 'new')` for addresses missing from the Dune seed
   (`dune_dbt/macros/general/solvers.sql`), so 36 addresses share that name, including
   `0x000…0000`. Three of them really did bid at some point; those must be given by
   address.
4. **Do not write `ltrim(arg, '0x')`** to strip an address prefix — `ltrim` strips a
   character *set*, so `0x0000…` loses every leading zero and mismatches silently. Use
   `case when arg like '0x%' then substr(arg, 3) else arg end`.

The table is a current-state snapshot with **no time dimension**: `active` means "the
latest allow-list event for this address was an add". It cannot answer "was X active on
2026-08-02".

## Reading the dbt model source

The models are defined in a separate repo, cloned locally at `~/Work/Code/cow-dagster`:

```
cow_dagster/cow_dbt/models/solver_accounting/marts/         # fct_*
cow_dagster/cow_dbt/models/solver_accounting/intermediary/  # int_*
cow_dagster/cow_dbt/models/solver_accounting/staging/       # stg_*
cow_dagster/cow_dbt/seeds/solver_accounting/                # reward_config.csv etc.
```

To find how any table is built:

```bash
find ~/Work/Code/cow-dagster -name "<table_name>.sql"
```

Materialised tables carry no SQL in the database, so this repo is the only way to read
their definitions.
