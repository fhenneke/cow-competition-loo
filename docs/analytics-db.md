# Analytics DB — connection, tables, join keys

## Connecting

`.env` holds `ANALYTICS_DB_URL` as `user:password@host:port`: **no scheme, no database
name**, so `psycopg2.connect(url)` fails on it. Use `loo.db.connect(network)`, which parses
it, picks `prod_<network>`, pins the session to UTC and adds the guards below. Ad-hoc
scripts run under `uv run --with psycopg2-binary` (no `psql` on the machine). The user is
`<db-user>`, read-only.

The session timezone is pinned because the quote side compares Postgres
`creation_timestamp` (rendered in the session's zone) with ClickHouse UTC timestamps
inside windows of minutes; an inherited zone or a `PGTZ` would empty every match silently.

### Dead connections and long statements

psycopg2 sets no read timeout and libpq no keepalives, so a query whose connection dies
waits forever (a run sat 7.5 hours that way on 2026-10-22). `connect()` sets:

- **TCP keepalives**: 60 s idle, then 6 probes 10 s apart, so a dead peer raises about two
  minutes after the line goes quiet. A live server answers the probes however long its
  query runs, so slow is never taken for dead. `tcp_user_timeout=120000` covers
  unacknowledged sends on Linux only; macOS accepts and ignores it.
- **`statement_timeout = 30min`** (server default: none). The slowest statement measured
  on the default path is the quote side's `ORDERS_SQL`, 81 s for 310,567 rows over mainnet
  2026-08-01..10-20 (measured 2026-10-22); chunked extraction queries take under a second.

`fetch` and `run` re-raise these as `db.ConnectionLostError` / `db.StatementTimeoutError`
(tested via `conn.closed`, not the message), and the CLI exits 6.

The server drops a client silent for about six minutes (`tcp_keepalives_idle=300`,
`interval=30`, `count=2`) — a sleeping laptop — so such a run is lost either way; the
client keepalives only make it fail fast. `idle_in_transaction_session_timeout=1d`
comfortably covers a read transaction held open through hours of ClickHouse work.

## Databases and schema

One database per network, models in schema `dbt`: `prod_mainnet`, `prod_xdai`,
`prod_base`, `prod_arbitrum-one`, `prod_avalanche`, `prod_polygon`, `prod_bnb`,
`prod_ink`, `prod_linea`, `prod_plasma`, `prod_lens` (stopped 2026-03-03), `prod_sepolia`,
`prod_solana` (from 2026-10-27, its own model names: [Solana](#solana)), each with a
`staging_*` counterpart. Raw backend tables are mirrored in `raw_prod_<network>`.
`prod_mainnet` also has personal dev schemas; use `dbt`.

## Tables

Bids and their executions:

| Table | Grain | Notes |
| --- | --- | --- |
| `stg_backend_data__proposed_solutions` | solution | `auction_id, uid, id, solver, score, is_winner, filtered_out`; indexed on `(auction_id, uid)` only |
| `stg_backend_data__proposed_trade_executions` | solution × order | `auction_id, solution_uid, order_uid, executed_sell, executed_buy` |
| `int_backend_data__proposed_solution_data` | solution × order | the two joined, plus tokens, `bid_score`, `order_surplus_atoms_in_surplus_token`; **lags**, no `filtered_out` ([below](#coverage-and-lag)) |

Auction context:

| Table | Notes |
| --- | --- |
| `stg_backend_data__competition_auctions` | `order_uids` (auction membership), `price_tokens`/`price_values` ([prices](#native-prices-the-competition_auctions-arrays)), `penalty_caps_native` (parallel to `order_uids`), `block_number`, `block_deadline`, `surplus_capturing_jit_order_owners` (empty; not read), `fast_path_quote_id` ([fast path](winner-selection.md#fast-path-auctions-are-excluded)) |
| `stg_backend_data__orders` | `uid, owner, kind, sell_token, buy_token, sell_amount, buy_amount, partially_fillable, app_data, signing_scheme, creation_timestamp, valid_to`. No order-class column |
| `stg_backend_data__app_data` | `contract_app_data → app_code, partner_fee_recipients`, joined on `orders.app_data` |
| `stg_backend_data__fee_policies` | `(auction_id, order_uid, application_order) → kind, factors`; `origin` (`protocol`/`partner`) from mainnet auction 14230909 (2026-11-12), NULL before |
| `stg_backend_data__jit_orders` | JIT orders, recorded only when their batch settles. Not read by `loo` |
| `stg_backend_data__auction_prices` | **dropped 2026-11-05** (cow-dagster sqitch migration 24) |

Outcomes and ground truth:

| Table | Notes |
| --- | --- |
| `stg_backend_data__reference_scores` | `(auction_id, solver) → reference_score`, winners only |
| `int_backend_data__solution_data` | winning solutions only: `score, reference_score, is_settled_in_time, upper_reward_cap, lower_reward_cap` (the *flat* floor, see [rewards](rewards.md#since-2026-09-08-the-lower-cap-is-per-order-cip-87)), `penalty_cap_native, is_excluded, block_deadline` |
| `fct_solver_rewards_per_auction` | `competition_score, observed_score, reference_score, uncapped_reward, batch_reward_native, upper_reward_cap, lower_reward_cap` (NULL on rows built before the column existed) |
| `pre_stg__orders_per_auction_with_at_least_one_bid` | `(auction_id, block_deadline, order_uid)` — the auction universe the reward models use |
| `stg_rpc_data__block_timestamp` | `block_number → time` |
| `dune_data__cow_protocol__solvers` | `address ↔ name, active, environment` ([traps](#resolving-a-solver-name)) |
| `reward_config` | per-network caps, `service_fee_factor`, reward token |

Settlement tables are under [observed outcomes](#observed-outcomes-what-actually-settled).
Auction ids are sparse but monotone.

## Join keys

**`proposed_trade_executions.solution_uid` joins `proposed_solutions.uid`, not `.id`.**
`uid` is a per-auction 0-based index, `id` the solver's own id; they collide for a
minority of rows, so the wrong join returns plausible data for part of an auction and
nothing for the rest (auction 13509867: uid 2 / id 11 matched by coincidence).

Otherwise: `orders.uid = order_uid`; prices on `(auction_id, token)` once unnested;
`reference_scores` on `(auction_id, solver)`. Addresses and uids are `bytea` — compare
with `decode(…, 'hex')` or `\x…` literals.

## `proposed_solutions.uid` encodes the ranking

The autopilot assigns `uid` **best to worst** (`crates/database/src/solver_competition_v2.rs`,
"uids get assigned from best to worst"), so `uid` order is the arbitrator's output order:

```
[winners, by descending score] ++ [non-winners, by descending score] ++ [filtered_out]
```

Verified on 2,531 solutions (auctions 13509000–13509867). It is the only record of the
autopilot's tie-breaks — bids are shuffled before the sort, and 54 auctions in that range
had two equal non-zero scores — so `loo` extracts solutions in `uid` order and every sort
after that is stable. The order is also not plain score order, and reference scores
depend on it ([why](winner-selection.md#ranked-order-is-load-bearing)).

## Native prices: the competition_auctions arrays

A price converts atoms to native wei as `native = amount * price / 1e18` (`price_in_eth`,
`crates/winner-selection/src/primitives.rs`). Prices are per auction and come **only**
from the parallel `price_tokens` / `price_values` arrays of
`stg_backend_data__competition_auctions`, read through `extract.fetch_prices`.

- The staging arrays are populated from **2026-09-15 00:00 UTC** on every network (linea,
  plasma and ink within 90 minutes of it) and NULL before; an auction with bids and NULL
  arrays raises `MissingPricesError`. `raw_prod_<net>.competition_auctions` has them from
  2026-07-30 (measured 2026-11-23); prices before that exist nowhere in the DB.
- No array repeats a token, has a length mismatch or a NULL element. While the legacy
  table existed the two sources agreed to the wei on 780,240 of 780,240 sampled prices.
- The row is wide, so unnesting costs more than an index lookup: +0.25 s per 200-auction
  `PRICES_SQL` chunk, and 57 s for the quote side's window-wide median
  (`quotes.TOKEN_PRICES_SQL`) over a week (mainnet, cold, September 2026).
- **Do not use `int_backend_data__auction_prices`** instead: it holds only tokens of
  *settled* trades, so losing bids, unsettled winners and untraded stablecoins have no
  price there.

## Native prices can be plain wrong

A native price can be off by orders of magnitude, and everything denominated in native —
score, surplus, reference scores, rewards — is fabricated with it. On mainnet
2026-08-01..04 one token was priced ~15,300x too high in nearly every auction (the wrong
value *was* the median), turning a 0.80 ETH trade into a recorded score of ~139 ETH; the
window's five largest winning scores were all that token.

- **Value each trade through both tokens.** `executed_sell × price(sell)` and
  `executed_buy × price(buy)` describe the same trade; healthy winners agree within
  1.04x. `counterfactual.price_suspects` flags a >2x disagreement, and `analyse`
  excludes such an auction **from every statistic** and names it
  (`--include-price-suspects` keeps them). The 2x threshold separates 1.04x from
  14,000x, not signal from noise, and suspect auctions can carry most of a solver's
  apparent Δsurplus (82% of Sector's over 2026-08-01..04, all fabricated).
- **Check the whole solution and each order.** Summed over a solution, a CoW that buys a
  mispriced token for one order and sells it for another cancels the error: polygon
  auction 41425733 priced a token ~3,500x too high and passed, giving one solver a
  −39.5M POL Δsurplus over 2026-10-28..11-24. A fill alone does not prove a price wrong,
  because a balanced CoW can overpay one order at correct prices, often as its only
  fill (mainnet 14273947, xdai 17840083). So an order is suspect when every fill **and
  its own limit** are more than 2x off the same way round; a limit selling 40M POL-worth
  for 902 POL is the price's fault, not the bid's. Over 2026-10-28..11-24 this excluded
  31 more mainnet auctions, 285 base (one recurring order of token `b2000000…`, ~2.9x),
  18 bnb, 2 each on polygon, arbitrum-one and xdai, none elsewhere.
- **A cross-auction median or spike check does not work**: the persistent price is the
  wrong one. Only the counterparty token inside the same trade anchors reality, which is
  why sell-token prices are loaded although valuation converts through the buy token.
- **The check misses a pair whose two tokens are mispriced by the same factor** (bnb
  2026-09-22/23 produced 1e8-BNB deltas that way), and any error under 2x. Scan a run's
  valued volume for absurd figures before quoting it. The quote side has the same hole
  ([quotes](quotes.md#two-price-checks-both-at-the-orders-own-auctions)).

### The accounting path corrects these prices; the scoring path must not

`int_backend_data__price_data` overlays `stg_auction_prices_corrections`
(`coalesce(apc.price, ap.price)`), and everything downstream inherits it: the DB's
`protocol_fee_native` and `partner_fee_native` are **corrected**, while the score the
competition was decided on is not.

- **Scoring, filtering, winners**: raw prices only — they are what the autopilot ran on.
- **Fees in ETH**: the corrected price is the better number. `loo` sidesteps the conflict
  by excluding price-suspect auctions, so it never diffs native fees against the mart —
  cross-checks compare **atoms**.

On 2026-08-30 the table had 20 rows (10–92x corrections), and `price_imbalanced`
independently flagged all 20 auctions. On 2026-11-23 mainnet holds 3 live and **7,184
archived** rows (`auction_prices_corrections_archived`, the live seed rotates weekly);
that agreement has **not** been re-checked on them. The table is curated after the fact,
so a fresh window can hold an uncorrected error; `price_imbalanced` does not depend on
that curation.

## No USD prices — stablecoin native prices imply the rate

The analytics DB has no USD price table and no token-metadata table. USD figures are
**display only** — nothing on the valuation or reward path reads one — and are derived per
auction from the auction's own prices:

```
usd_per_native = 10^(36 − decimals) / price(stablecoin)
```

`extract.load_usd_rates` takes the per-auction median across the network's curated
reference stablecoins (`primitives.USD_REFERENCE_TOKENS`), so no single wrong price sets
the rate, with a window median where an auction priced none of them. A network without a
list skips the USD columns rather than guessing; an external feed would be a second trust
domain for a cosmetic number. On mainnet USDC/USDT/DAI agreed within ~0.1% (2026-08).

The lists were curated in August 2026 by mutual agreement (0.1–0.7%) plus an independent
anchor (mainnet USD/ETH through both chains' COW accounting rates), not by name: polygon's
USDC.e sat ~6% below the others and was rejected; ink and plasma entries are unnamed
dollar tokens (plasma `b8ce59fc…` is USDT0); BNB-chain stables have **18 decimals**.

## Coverage and lag

`int_backend_data__proposed_solution_data` **lags the staging tables badly and
variably** — 7.5 days when measured on 2026-08-11. Measure it rather than assume:

```sql
select (select max(auction_id) from dbt.int_backend_data__proposed_solution_data) as int_max,
       (select max(auction_id) from dbt.stg_backend_data__proposed_solutions)      as stg_max
```

It also lacks `filtered_out`, so the fairness-filter ground truth comes only from
`stg_backend_data__proposed_solutions` joined to `…__proposed_trade_executions`.

The lag is specific to that model: on the same day `stg_backend_data__settlements`,
`int_backend_data__winning_solutions_with_onchain_status`, `int_backend_data__solution_data`
and `int_backend_data__trade_with_tx_hash` were level with staging. Check the model, do not
avoid `int_` as a class.

## Mapping a date window to auctions

```sql
select distinct auction_id, block_deadline
from dbt.pre_stg__orders_per_auction_with_at_least_one_bid
where block_deadline between
      (select min(block_number) from dbt.stg_rpc_data__block_timestamp where time >= :start)
  and (select max(block_number) from dbt.stg_rpc_data__block_timestamp where time <  :end)
```

The same auction universe the dbt reward models use.

## `order_surplus_atoms_in_surplus_token` rounds

The model computes `executed_buy - ceil(executed_sell * buy_amount / sell_amount)` (sell
orders) in Postgres `numeric`, and `/` on two integral numerics picks result scale 0 at
these magnitudes, so the quotient is rounded half-up **before** `ceil` sees it. Where the
true fraction is in `(0, 0.5)` the column is one atom too high (traced on auction
13488353, solution 0). Over mainnet 2026-08-01..04, 624 of 94,565 orders differ, every one
by exactly −1. Use exact integer ceiling division (`-(-a // b)` in Python, `div()` plus a
remainder test in SQL).

## Fee policies cover every order of every kept solution

The autopilot writes policies for the orders of every **non-filtered-out** solution
(`autopilot/src/run_loop.rs`, iterating `ranking.ranked()`):

> An auction order has fee policies for auction `a` **iff** it appears in at least one
> solution of `a` with `filtered_out = false`.

Executed-ness never enters. Verified over auctions 13480599–13517908 with no exception
either way (20,210 kept pairs all covered, none of 1,638 filtered-only pairs). So:

- It is not a test for auction membership — use `competition_auctions.order_uids`.
- Protocol fees can be recomputed for 98.9% of proposed executions and 100% of those in
  kept solutions; `loo/fees.py` reproduces `proposed_solutions.score` from them to the wei.
- A covered order with no rows has genuinely no policies (fee 0).
- Policies are stable across auctions for the same order (1,485 of 1,485 identical), so an
  uncovered order could be backfilled from another auction. `loo` does not need to.
- `application_order` is a **global** serial: only its order within one order is
  meaningful. Sort ascending for the application sequence (`protocol_fees` walks it in
  reverse).

An auction where an *executed* order's policies are missing on either side (possible only
when a recorded-filtered-out solution wins the counterfactual) is dropped from both sides
of the fee sums and counted in `auctions_fees_incomplete`: 0.07–0.17% of auctions per
solver over 2026-08-01..04.

## Partner fees are the trailing policies, and they are most of the total

An order's policy list ends with one entry per partner declared in its app data, passed
straight on. Over 2026-08-01..04 on mainnet (`int_backend_data__fees_per_trade`):
**33.9 ETH partner against 30.8 ETH protocol**, so a "protocol fee" including them roughly
doubles what the protocol keeps. The split is **positional**, not by kind (volume
policies appear on both sides):

> With `L` policies in application order and `n` partner recipients, the **trailing `n`**
> are partner fees and the rest the protocol's — except that `L = 1` is always the
> protocol's.

`n = coalesce(array_length(app_data.partner_fee_recipients, 1), 0)`, joining
`orders.app_data = app_data.contract_app_data`. **`partner_fee_item_count` is NULL on all
888,760 rows** — do not use it. The join is on app data, not on a settlement, so it answers
for orders that never traded, which a counterfactual needs (99.6% of traded user orders
have a row; the rest default to zero partners). `loo.fees.fee_split` implements the rule;
it reproduced the model's split on 16,431 of 16,431 settled trades.

Naming: the DB and `arbitrator.rs` say "protocol fee" for the gross amount and
`protocol_fee_without_partner_fee_native` for the net. `loo` says "protocol fee" for what
the protocol keeps.

### `partner_fee_cut` is NULL on older rows

`int_backend_data__fees_per_trade.partner_fee_cut` (the protocol's share of a partner fee:
per-recipient config, else 0.25) is NULL on rows built before the column existed — below
auction 26,089,387 on bnb and 29,403,168 on base, about 2026-10-06 (measured 2026-10-14).
Fill it as the model does: `coalesce(partner_fee_cut, pc1.partner_fee_cut,
pc2.partner_fee_cut, 0.25)` joining `stg_partner_fees_config` on `processed_fee_recipient`
(with `app_code`, then without). On bnb 2026-10-05..12 the configured cuts averaged 16%.

## Observed outcomes: what actually settled

| Table | Grain | Notes |
| --- | --- | --- |
| `stg_backend_data__settlements` | settlement event | `(block_number, log_index, solver, tx_hash, auction_id, solution_uid)` |
| `int_backend_data__winning_solutions_with_onchain_status` | winning solution | `tx_hash, block_number, is_settled_in_time`; left-joins settlements, so every winner has a row and an unsettled one has a NULL `tx_hash` — a missing row means missing data |
| `int_backend_data__trade_with_tx_hash` | auction × order | on-chain amounts per traded order; filtered to auction orders, so no JIT orders |

### `settled` and `settled_in_time` come apart, and which one you want depends

```sql
is_settled_in_time = coalesce(block_number <= block_deadline and tx_hash is not null, false)
```

`tx_hash is not null` answers "did it execute"; `is_settled_in_time` answers "did the
protocol pay for it". 16 winners over 2026-08-01..04 landed late: users got their fills,
the solver earned nothing. `loo` uses **`is_settled_in_time` for both surplus and
rewards** (`extract.Settlement.counts_as_executed`, consumed by both sides), so the two
views of one auction never disagree about which winners delivered; the real surplus of
late batches is reported as `orders_lost_to_lateness` rather than absorbed.

On 2026-11-03 11:44 UTC mainnet's settle deadline went from two blocks to one, and
not-landed winners rose from 6–13% a day to 20–28%. In-time rates are not comparable
across that date; `inherited` deltas are unaffected by construction.

### Failures are concentrated in the biggest solutions

Over 2026-08-01..04, the 14.9% of winners that never settled carried **50.2%** of winning
score (largest: 143.6 ETH). How a counterfactual treats settlement therefore moves its
headline by several ETH. `loo` attaches settlement to the **slot**: a replacement winner
inherits the recorded outcome of the token pairs it claims (`inherited`), so a batch that
reverted stays reverted whoever replaces it and settlement cancels out of the comparison.
`assume-settled` (everything lands, both sides) is the one alternative kept. Attaching
settlement to the solution — real reverts charged to the baseline, every replacement
credited — was dropped because it is not a counterfactual anyone would defend.

### A settled proposal is exact — the only divergence is settling at all

Over 2026-08-01..04, 9,061 of 9,061 settled order rows have on-chain `sell_amount`/
`buy_amount` equal to the proposed `executed_sell`/`executed_buy`, and no proposed order of
a settled winner is missing from the trades. A solver commits its clearing prices in the
calldata, so the observed execution *is* the proposal. `loo` reads executed amounts from
`proposed_trade_executions` and only the settlement status from chain. `fee_amount` on
the `Trade` event has been 0 since fees moved into the clearing price (June 2026).

## Resolving a solver name

`dune_data__cow_protocol__solvers` is the only address → name bridge; `address` is `bytea`
and joins `proposed_solutions.solver` directly. It is a current-state snapshot with no
time dimension (`active` = latest allow-list event was an add).

1. **Exact matching only.** `Arc` ⊂ `Arctic` and both bid; also `Quasi` ⊂ `Quasilabs`,
   `Sector` ⊂ `Sector_Finance`, `Tsolver` ⊂ `TestSolver1`. Case-insensitive *exact*
   matching is safe.
2. **One name, several addresses** (rotated keys: `Barter`, `Kipseli`, `Rizzolver`,
   `Tsolver`). Neither `environment` nor `active` disambiguates. Same-name addresses never
   bid in the same auction, but a window can straddle a rotation, and then **all of them
   must be removed together** or one half is treated as a rival of the other.
3. **`Uncatalogued` is not a solver**: the model coalesces missing names to it (36
   addresses on mainnet, including `0x0`). Such solvers are named by address.
4. **Never `ltrim(arg, '0x')`** — it strips a character set and eats leading zeros. Use
   `case when arg like '0x%' then substr(arg, 3) else arg end`.

## `int_accounting_period_data__conversion_rates` is per-block and unindexed

The COW→native rate model has **one row per block** of the synced history although the
rate changes once per weekly accounting period (Tuesday 00:00 UTC to Tuesday). dbt
created no index on it, so every predicate scans millions of rows, and
`block_number = any(<thousands of blocks>)` evaluates the array against every row — a
sweep of those drove a DB CPU alert on 2026-08-14. `extract.CONVERSION_RATES_SQL` asks
at the real grain instead: one `between` range scan grouped to
`(first_block, last_block, rate)` per period, blocks mapped client-side, loaded once per
window.

Generally: check a dbt model's materialization before an `= any(array)` probe, and
prefer a range plus client-side filtering on an unindexed table.

## Bound joins against `proposed_solutions` to the window's auction-id range

A join between `proposed_solutions` and a CTE whose bounds are subqueries is planned as a
**sequential scan of the whole table** (13.4M rows on mainnet): the planner cannot size
the CTE (it estimated 13,805 rows for a 3- and a 7-day window alike). Measured 2026-09-15,
mainnet, 7-day window, partly warm:

| query | plan | time |
| --- | --- | --- |
| `ALL_SOLVERS_SQL` / `SOLVER_SQL`, CTE join | seq scan + hash join | 1.6–10 s each |
| with `ps.auction_id between (min) and (max)` of the window | index scan | 0.3–0.6 s |

Cold or on a busy server each scan takes minutes, which was the whole of the
"`--all-solvers` never returns" reports. Both queries carry the range bound now (results
identical row for row). Give any query touching a large staging table an explicit
`auction_id` range or id array, and check `explain` for `Seq Scan on stg_backend_data__…`.

## Who placed an order: app code, owner, token symbols

- **App code**: `stg_backend_data__app_data.app_code` via `orders.app_data`.
- **The protocol's own orders** are owned by the settlement contract (`9008d19f…`, app
  code `CoWFeeModule`, presigned, partially fillable) and convert collected fees. They
  count as coverage like any user order (84 of the 132 orders only Gnosis_0x executed on
  avalanche, 2026-10-01..29). Ethflow orders are owned by `ba3cb449…` (also `40a50cf0…`
  on mainnet).
- **Token symbols** are not in this DB: use ClickHouse
  `raw_quotes.quote_requested.sell_token_symbol` / `buy_token_symbol`, filtered by
  `chain_id` and a day-bounded `event_timestamp`. There is no decimals source; value
  amounts through native prices.

## Reading the dbt model source

Materialised tables carry no SQL in the DB; the models are in `~/Work/Code/cow-dagster`
(`cow_dagster/cow_dbt/models/solver_accounting/{staging,intermediary,marts}/`, seeds in
`cow_dagster/cow_dbt/seeds/solver_accounting/`). `find ~/Work/Code/cow-dagster -name
"<table>.sql"`. Check the checkout against `origin/main` first.

## Solana

`prod_solana`, live since 2026-10-27, asks the same questions of `solana_`-prefixed
models: `extract.SOLANA_SQL` maps each EVM query to its Solana counterpart, which returns
the same columns, and `extract.sql_for` picks by the connection's network
(`db.network_of`). Solana runs the same arbitrator (services `crates/autopilot-svm`
instantiates `winner-selection` for Solana; production `max-winners = 20`). What differs:

- **Tables.** `stg_solana_backend_data__*`, `int_solana_backend_data__*` and
  `fct_solana_*`, with the raw tables in `raw_prod_solana`. Where `loo` reads them, the
  columns are the EVM ones except as below.
- **The window.** `stg_solana_backend_data__competition_auctions` holds only auctions
  with a winner, and its `auction_timestamp` is the time of the deadline slot, so the
  window is read from it directly; on 2026-11-26 every raw auction with a bid was in it.
  There are no blocks: `deadline_slot` stands where `block_deadline` does.
- **Native prices** are lamports per atom × 1e9 (`NATIVE_PRICE_DENOMINATOR` in services
  `crates/chain-types`), not wei × 1e18. `extract.fetch_prices` multiplies them by 1e9 onto
  the 1e18 denominator, which is exact, floor included (`a·(p·1e9)/1e18 = a·p/1e9`), so
  every native amount, score and reward stays in lamports. Read Solana prices only through
  it: a missed scale is a factor of 1e9 that raises nothing.
- **Ids** are 32 bytes: pubkeys for tokens and solvers, the intent hash for orders. An
  order paid out in native SOL buys the System Program id `00…00` (`NATIVE_SOL`, about 30%
  of orders), priced like wSOL at 1e9 and claiming the wSOL pair (`canonical_token`).
- **Solvers** are catalogued in `dune_data__solana_solvers` with base58 addresses, which
  SQL cannot decode: `loo` reads the catalogue and every window bidder's activity and
  matches in Python. `--solver` takes a name, a base58 address or hex; `--all-solvers`
  names an uncatalogued bidder in base58.
- **No fees.** autopilot-svm can apply protocol-fee policies, but production configures
  none (the backend's `solana.fee_policies` is empty, 2026-11-26), and Solana orders carry
  no partner fee. No fee-policy table reaches the analytics DB, so `loo` takes every fee
  as known to be zero. That is exact only while the backend table stays empty; a fee
  would show in `validate` as a recorded score above the recomputed surplus.
- **Not there:** the fast path, JIT owners, `proposed_solution_data` (so no
  `--cross-check-surplus`) and quote data in ClickHouse (so no `--quotes`).
- **Rewards.** `fct_solana_solver_rewards_per_auction` is the EVM formula keyed on
  `deadline_slot`. Its upper cap is `scaling_factor × (protocol fee − partner fee)`, so 0
  until Solana charges protocol fees. `int_solana_backend_data__solution_data.lower_reward_cap`
  is NULL (0 in a no-penalty window): there is no flat floor, so a solver with a win
  lacking a penalty cap has a NULL floor and a NULL reward (`rewards.AuctionCaps`). Every
  capped reward to date is 0, and so is every weekly consistency and quote budget.
- **Not read:** the weekly marts and the COW conversion. Solana's weekly accounting has no
  per-auction quote-share table to split a week by, so a Solana report's recorded rewards
  and COW figures are `null`, not zero.
- **Display:** amounts in SOL, 9 decimals (`aggregate.NativeFormat`); USD per SOL from
  USDC and USDT, applied per 1e9 lamports (`UsdContext.unit`). The artifact page does the
  same: `scripts/artifact_data.py` emits whole SOL and a USD rate per lamport.

Validated 2026-11-26 over 2026-10-28..11-26: `validate` reproduced 3,409 of 3,409
auctions (winners, filter, reference scores) and `validate-rewards` 3,652 of 3,652 mart
rows.
