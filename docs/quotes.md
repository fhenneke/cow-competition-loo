# The quote competition

Durable context for the quote side of the analysis: where the data is, how the winner is
actually picked, what can and cannot be reproduced from the record, and the measurements
behind each of those claims. The concrete work is in
[PLAN.md](../PLAN.md#16-m15--the-quote-competition--done); the argument that shaped it is
in [PROPOSAL-quote-competition.md](../PROPOSAL-quote-competition.md).

## Where the data is

The quote competition is in **ClickHouse**, not the analytics DB. `loo/ch.py` connects
over plain HTTP with `CLICKHOUSE_URL` / `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` from
`.env`; no driver is needed and none was added.

| table | rows | what it holds |
| --- | --- | --- |
| `gold_quotes.fct_quote_requests` | 135.2 M (8.6 GiB) | one row per request: tokens, kind, `in_amount`, `quote_id`, the winning `winner` estimator, `n_est` |
| `gold_quotes.fct_quote_estimates` | 632.8 M (46.7 GiB) | one row per estimator's answer: `out_amount`, `gas`, `estimate_verified`, `is_winner` |

Both are `ReplicatedReplacingMergeTree` fed from `raw_quotes.{quote_requested,
price_estimates, winning_price_estimates, quote_computed}`.

### Coverage started with two networks on 2026-07-22

Measured 2026-09-01: prod requests run **2026-07-22 15:28 .. now**, and only for
**mainnet (chain 1)** and **bnb (chain 56)**. There is no `TTL` in the DDL and the start
has not moved, so this is the backfill boundary rather than a rolling window — but
`ch.Client.coverage` queries it rather than assuming, because it moved once already.

**Base (chain 8453) followed on 2026-09-01 16:00**, and by 2026-10 `gold_quotes` held prod
requests for every chain the protocol runs on. Base is enabled in `NETWORK_CHAIN_IDS` since
2026-10-14. The others are not yet, because nothing has checked their reproduction rate or
match coverage. `raw_quotes.price_estimates` carries base with a `request_id` on every row,
as on the other two. Base's first run, 2026-10-05..12 with `--all-solvers`:

| | base | bnb, same window |
| --- | --- | --- |
| orders linked to a competition | 17,108 of 17,110 (1,588 via the raw layer) | 28,373 of 28,374 (3,114 via the raw layer) |
| pick reproduces the recorded winner | **99.19%** (16,952 / 17,091) | 98.55% (27,599 / 28,004) |
| verified-class rule, mixed requests | 8,554 of 8,554 | 13,355 of 13,355 |
| price-suspect orders | 4 | 325 |

bnb's rate is down from 100.0% on 2026-08-10..16 and not yet explained.

`loo.ch.NETWORK_CHAIN_IDS` is the support test: a network absent from it has no quote
data, and `analyse --quotes` refuses it instead of reporting empty quote columns, which
would read as "this solver quoted nothing" (Q-D10).

A run whose window starts before the coverage does has its **quote window clamped
forward** — an order created before the data begins can never be matched, and leaving it
in the denominator would deflate every quote share by however much of the window predates
the backfill (two thirds of a July window). The report carries both windows and `compare`
prints a warning when they differ.

### Request volume, and why `fast` is excluded

Per day on 2026-08-25, prod:

| network | `optimal` | `verified` | `fast` |
| --- | --- | --- | --- |
| mainnet | 1,183,363 | 33,816 | 217,408 |
| bnb | 246,067 | 847,743 | 478,561 |

Against roughly **1.6 k order-creating requests a day on mainnet** — a ~0.13% conversion
rate, which is why conversion is out of scope rather than proxied.

`fast` requests carry **no `quote_id`** (measured: `countIf(quote_id is not null)` equals
`countIf(quote_id is not null and price_quality != 'fast')` exactly) and are served by an
estimator built `with_early_return`, so their ladders are truncated. They are excluded from
the order↔competition link because **`order_quotes` never holds a fast quote**: the quote
an order is placed from is always a non-fast competition. The Trust Wallet flow is the case
that proves it — its UI polls `fast`, and 765 of its 779 orders on 2026-07-25 share an
amount with a fast request, but the amount the order actually carries reproduces the
winner of a **non-fast** competition to the atom, one the gold tables never received
([below](#the-gold-tables-do-not-hold-every-competition--link-through-the-raw-layer)).
Matching those orders to the fast poll instead leaves a 0.45% gap and a heuristic tier.

The `optimal` path, which is what order-creating requests use, is **one stage with no
early return** (`price-estimation/src/factory.rs:381-392`): every estimator is asked, so a
removal does not change the rest of the ladder. That is what makes the counterfactual
well-defined at all.

### Query shapes that decide the cost

- `fct_quote_estimates` is sorted `(chain_id, environment, estimator, event_timestamp,
  request_id)`. **`estimator = …` is a key prefix; `request_id in (…)` is not** — a
  request lookup scans whatever partitions the time filter leaves.
- `fct_quote_requests` is sorted `(chain_id, environment, app_code, event_timestamp,
  request_id)`, so it has no amount index either.
- Both partition by `(chain_id, environment, toYYYYMMDD(event_timestamp))`, so a
  day-bounded filter prunes to those days. Every query in `loo/quotes.py` carries one, and
  `ch.Client.fetch` always sets `max_execution_time`.
- A day of mainnet holds 1.1 M requests with a `quote_id`, so the candidate query filters
  on `(sell_token, buy_token, in_amount)` **tuples in ClickHouse** rather than pulling the
  day and filtering locally. ~1.6 k orders a day become ~2.3 k distinct amount keys, sent
  in chunks of 400.
- The `_live` views are never used: they splice the last ≤6 hours in from the raw tables,
  so two runs over the same window can disagree.
- Both tables are `ReplacingMergeTree`s, so a row can be present twice until a background
  merge collapses it. Requests are keyed by `request_id` and ladders by
  `(request_id, estimator)` when loaded; without that a duplicated winner row makes
  `recorded_winner` read as ambiguous and silently demotes a corroborated match to
  heuristic. Harmless on a window queried weeks later, wrong on one queried today.
- Ladders are loaded per **request day** too, with the same ±30 min / +90 s bounds as
  the candidate query — an estimate lands seconds after its request — rather than over
  the run's whole window, which would be one full-window partition scan per 2,000 ids.

## How the winner is picked

`services/crates/price-estimation/src/competition/quote.rs`:

```
is_reasonable          (:191)  drop any estimate with gas == 0 or out_amount == 0
compare_quote_result   (:203)  a verified estimate beats an unverified one at any amount
compare_quote          (:226)  otherwise rank on effective_eth_out
effective_eth_out      (:276)  eth_out = out_amount × native_price(out_token)
                               fees    = gas × gas_price
                               sell:  eth_out − fees   → maximise
                               buy:   eth_out + fees   → minimise
```

`PriceRanking::BestBangForBuck` is what the prod `optimal` estimator is built with.

### What the ladder does not record

Verified against the Rust on 2026-09-02 (`services` at d5bf0f6); each of these matters
for reading a reproduction rate:

- **Ties break by arrival order.** `results.sort_by(…)` (`quote.rs:80`) is a stable sort
  over the vector `produce_results` filled as estimators *answered*, so an exact tie goes
  to whichever answered first — not config order, not name order, and not recorded.
  `PickValidation.tied` counts them.
- **Underwater sell quotes all tie at zero.** `effective_eth_out` clamps a negative or
  NaN value to `U256::ZERO` (`quote.rs:285-295`), so every estimate whose gas exceeds
  its value is equal to every other and arrival order decides. `rank_key` keeps them
  ordered by how far under water they are; the difference lands in the `nonpositive`
  leaf, which is excluded from every figure and from the fee path.
- **Errored estimators leave no trace.** `emit_quote_event` fires only on `Ok`
  (`quote.rs:59`), so "the removed solver was the only usable estimator" and "the others
  errored on this request" are the same ladder.
- **Zero-gas and zero-amount estimates are in the ladder but never won.** The event is
  emitted before `is_reasonable` filters (`quote.rs:55-76`), which is why `Estimate.reasonable`
  has to be applied here too.
- **The streaming endpoint is invisible.** `/api/v1/quote/stream` runs the same optimal
  competition but emits no estimate and no winner events while still persisting every
  quote it yields (`estimate_stream`, `quote.rs:104-188`). A request served that way is
  in `fct_quote_requests` with no ladder, and one request can create several `quotes`
  rows. If an integration adopts it, match coverage and the reproduction rate move for a
  reason that is not a rule change — check request counts without a ladder before reading
  a fall in either as one.
- **Prod compares `f64` values.** `f64::from(out_amount)` (`quote.rs:277`) loses precision
  above 2^53 before the comparison happens, so the `Float64` ladder is not a degradation
  relative to what prod computed with.

### The gross ladder is not the competition

The recorded winner is the highest gross `out_amount` in only, on **mainnet 2026-08-25,
prod**:

| `price_quality` | winners | winner is gross rank 1 | share |
| --- | --- | --- | --- |
| `optimal` | 1,172,704 | 605,455 | **51.6%** |
| `verified` | 33,620 | 9,094 | **27.0%** |
| `fast` | 201,528 | 184,999 | 91.8% |

So `fct_quote_estimates.price_rank`, `behind_best_bps` and `behind_winner_bps` are
computed on `out_amount` alone and are **not** "how far behind the winner this estimator
was" in any sense the protocol uses. Diagnostics, not deltas.

### The verified class is a hard first key

The enum's code default is `Unverified` (`configs/src/price_estimation.rs`), so whether the
class rule applies at all is a deployment setting. The data settles it: over the
order-creating requests of **2026-08-10..16** there were **5,041 requests carrying both
verified and unverified estimates** (4,491 mainnet, 41.2%; 550 bnb, 8.6%), and a verified
estimate won **all 5,041**. Not one unverified estimate beat a verified one, at any amount.

Verification coverage differs enormously by chain: 2.9% of mainnet requests had no
verified estimate at all, against 75.7% on bnb.

### The two ranking rules, and why the default is not the protocol's own

Neither `native_price` nor `gas_price` is in ClickHouse, but they enter the comparison only
through a ratio common to every estimator of one request. `loo.quotes.RANKING`:

- **`effective-sell`** (default) — rank on the effective quote netted in the **sell**
  token, at the order's own recorded `gas_price / sell_token_price`. Needs nothing that is
  not on `order_quotes`, and is the same quantity `fees._adjust_quote_to_order_limits`
  charges the price-improvement fee against, so the pick and the fee cannot disagree.
- **`autopilot`** — the protocol's own out-token rule, which needs the **buy** token's
  native price for sell orders (taken from the window's `auction_prices`, median per
  token). The cross-check.

Reproduction of the recorded winner, 2026-08-10..16 order-creating requests:

| rule | mainnet | bnb |
| --- | --- | --- |
| `effective-sell`, verified class first | **10,861 / 10,906 (99.6%)** | **6,395 / 6,397 (100.0%)** |
| `effective-sell`, ignoring the class | 9,225 (84.6%) | 6,162 (96.3%) |
| gross `out_amount` only | 6,707 (61.5%) | 5,773 (90.2%) |

The two rules agree because a quote asserts `in × sell_price ≈ out × buy_price`: the
sell-token netting **scales** the amount by `(1 − gas·gas_price/(in·sell_token_price))`
while the autopilot's **shifts** it by `gas·gas_price/native(out)`, and those coincide to
first order. They diverge only through the quote's own pricing error, which is what makes
the disagreements interesting rather than noise. On a **buy** order the out token *is* the
sell token, so the two rules are identical there by construction.

Every run reports its own reproduction rate (`quote pick reproduced the recorded winner`).
A window where it drops means the rule changed and every quote figure is then suspect.

## Matching an order to its quote: every route, and what each is good for

The backend never guesses. At order placement `find_quote` either looks the quote up by
the `quoteId` the placement supplied — for ethflow orders the `quoteId` travels on-chain
as a field of the order-placement event's auxiliary data, beside the user's `valid_to`
(`ethflow_events.rs:139-146`) — or searches its own live `quotes` table
(`crates/database/src/quotes.rs:111`):

```sql
WHERE sell_token = $1 AND buy_token = $2 AND
      ((order_kind = 'sell' AND sell_amount = $3) OR
       (order_kind = 'sell' AND sell_amount = $4) OR
       (order_kind = 'buy'  AND buy_amount  = $5)) AND
      order_kind = $6 AND expiration_timestamp >= $7 AND quote_kind = $8
ORDER BY verified DESC,
         buy_amount / (sell_amount + gas_amount * gas_price / sell_token_price) DESC
LIMIT 1
```

Neither route survives into the analytics DB: `order_quotes` has eight columns and no id,
its raw `metadata` holds only interactions and JIT orders, and `public.quotes` — which does
have `id` — is a stale 5,522-row snapshot with no `order_uid`. **The association exists only
transiently inside the backend.** So the link has to be reconstructed, and there are six
signals to reconstruct it from. They do different jobs and it matters which is used for
which.

| | signal | strength | job |
| --- | --- | --- | --- |
| 1 | **tokens and kind** | necessary, never sufficient | candidate generation |
| 2 | **amount** — the request's `in_amount` against `order_quotes.sell_amount` (sell) or `buy_amount` (buy) | strong, and the only signal ClickHouse can filter cheaply | candidate generation |
| 3 | **time** — 30 min before creation to 90 s after | weak; quote validity is 60 s offchain / 10 min onchain, so the window is generous by design | candidate generation |
| 4 | **the winner's `out_amount`** against the amount stored on the order | near-exact: bit-identical for 77.9% of matches | confirmation (the match tier), and first choice among an order's candidates; the latest candidate wins otherwise |
| 5 | **the winner's `gas`** against `order_quotes.gas_amount` | near-exact: ratio 1.0000 at every quantile p10–p99, exactly 1 for 78.3% | confirmation, unused so far |
| 6 | **winner consistency** — the winning estimator maps to `order_quotes.solver` | strong when a user re-quotes the same amount | deriving the estimator → address map, from corroborated matches only. **Not** used to choose among candidates: the map is derived from the matches, so choosing by it would be circular |

Because the match also needs the two clocks to agree, `db.connect` pins the Postgres
session to UTC: `creation_timestamp` is rendered in the session's timezone and compared
as a naive string against ClickHouse's UTC `event_timestamp` inside a window of minutes,
so an inherited server default or a `PGTZ` in the environment would empty every match
without an error.

Two more that look available and are not:

- **The rate**, `buy/sell`. It is the signal that survives *scaling*: for a sell order
  `find_quote` calls `with_scaled_sell_amount`, so `order_quotes.sell_amount` is the
  **order's** amount and `buy_amount` is the quote's scaled to it — the amounts move, the
  ratio does not. Tested on unmatched sell orders and it recovered almost nothing, so
  scaling is not what the misses are. It also cannot generate candidates: without an
  amount filter a ten-day mainnet window returns 800 k candidate requests for 38 k orders.
- **`quote_id`**, which `fct_quote_requests` carries and nothing on the Postgres side does.

### Why signals 1–3 must do the candidate generation

`fct_quote_estimates` is sorted `(chain_id, environment, estimator, event_timestamp,
request_id)`, so a request lookup is not a key prefix, and the amount is indexed nowhere.
The affordable shape is therefore: filter requests by tokens/kind/amount in ClickHouse per
creation day, apply the time filter in Python (a `bisect` over amount-sorted buckets —
38 k orders against buckets tens of thousands deep is minutes of linear scanning), and only
then load ladders for what survives. On a two-day window that is 13 k ladder requests
rather than 136 k.

### The gold tables do not hold every competition — link through the raw layer

`gold_quotes.fct_quote_requests` is built from the `quote_requested` event, and a
competition whose request event never arrived is in **neither** gold table: not in the
request table it would have been a row of, and not in `fct_quote_estimates` either,
which is joined off it. Measured on 2026-07-25 mainnet against `raw_quotes`:

| | competitions |
| --- | --- |
| raw `price_estimates` (successful, with a request id) | 1,537,915 |
| of which without a `quote_requested` event | **8,625** (0.6%) |
| of which in either gold table | **0** |

Small as a share of requests, they are the competitions behind **a quarter of the day's
orders**: API callers — Safe and bot addresses, `defibot`, `label`, the no-app-code flow —
whose ladders are complete in the raw layer (7–10 estimators, a `winning_price_estimates`
row, no `quote_computed`). Every field the request row would carry is on each estimate
row (tokens, kind, `in_amount`, as exact decimal strings), so the competition is
reconstructed by grouping `raw_quotes.price_estimates` on `request_id`
(`quotes.load_raw_candidates`), and the winner comes from its own event stream. The raw
tables are sorted `(chain_id, estimator, sell_token, buy_token, nats_seq)` and
partitioned by month, so each lookup is a filtered month scan — affordable because only
the orders the gold layer could not place are asked for, per creation day. `request_id`
is nullable there (49 estimates that day had none) and is filtered.

The `quote_id is not null` half of the old candidate filter is gone: these competitions
have no `quote_computed` either, yet their quotes are on the orders, so the column says
nothing about whether a competition priced an order. `price_quality != 'fast'` stays, for
the reason above.

With that, 2026-07-25 mainnet links **2,935 of 2,937** orders (99.9%): 1,389 through the
gold request table and 1,546 through the raw layer, the two `OneKey CowSwap` orders being
the remainder. The raw-layer matches are corroborated **1,546 of 1,546** — the winner's
`out_amount` reproduces the stored quote exactly, including all 765 Trust Wallet orders —
and the pick reproduces the recorded winner on 99.3% of matched requests, so the recovered
competitions are the same competition, not a look-alike. The raw ladders run 2–10
estimators deep (median 9), which a fast ladder never does.

The paragraphs below record what coverage looked like **before** this, kept because the
per-integration pattern is the diagnostic that led to the raw layer.

### What failed before the raw layer, and why it was not random

Coverage was a property of the **integration**, not of the window. 2026-07-25 mainnet,
gold request table with the old filter:

| app code | orders | matched | |
| --- | --- | --- | --- |
| CoW Swap | 874 | 868 | 99.3% |
| Decentralized CoW | 178 | 178 | 100% |
| Jumper CoW, CoWFeeModule, aave-v3-* | 76 | 76 | 100% |
| Safe Wallet Swaps | 23 | 22 | 95.7% |
| CoW Swap-SafeApp | 55 | 18 | 32.7% |
| (no app code) | 334 | 90 | 26.9% |
| Trust wallet | 779 | 14 | **1.8%** |
| label | 243 | 2 | 0.8% |
| defibot | 184 | 0 | **0%** |

Sampling the unmatched and searching for their exact amount and pair inside the window at
**any** price quality splits them in two:

- **Trust wallet** — 34 of 34 sampled requests found, every one `price_quality='fast'` with
  `quote_id IS NULL` and **exactly 2 estimators**. Its orders *are* created from fast
  quotes, which contradicts the common assumption that fast requests never create orders.
  They are excluded here because the fast estimator runs `with_early_return`
  (`factory.rs:404`): only the first estimators to answer are in the ladder, so removing
  the winner promotes the only other answer when the backend would have waited for a third
  that was never recorded. Including them would raise coverage to roughly 70% and turn the
  figure into an **upper bound on the harm**. Measured and left out; it is a reporting
  class, not a matching problem.
- **defibot, label, no app code** — nothing found at any price quality. Genuinely unquoted
  or unrecorded flow. Nothing recovers these.

`coverage_by_app` on `QuoteWindow` reports this per run. The two failure classes above
resolved differently: the Trust Wallet fast quotes were in the gold table all along and
were being filtered out; `defibot`, `label` and the no-app-code flow were competitions
the gold tables never held, recovered from `raw_quotes` (previous section).

### The clean fix is upstream

Persist `quote_id` on `order_quotes` (or emit `order_uid` into the quotes pipeline). The
whole heuristic collapses to a join, signals 4–6 stop being needed, the match tiers
disappear and coverage goes to ~100% of quoted flow.

### An aside on that `ORDER BY`

The `find` query re-ranks candidate quotes by `buy / (sell + fee)` — the sell-token
netting, agreeing with the competition to first order but not identical to it, and not the
rule the competition itself uses. It is the *attribution* step, not the pick shown to the
user, so it is not authority for the ranking here; read it as a possible inconsistency in
how quotes are attributed to orders rather than as a second definition of the competition.

## The order ↔ request join is a heuristic with an exact corroborator

There is still **no `quote_id` on an order**. `order_quotes` and its staging model carry
amounts, gas and solver but no id; `public.quotes` does have `id` and is a stale 5,522-row
snapshot from 2026-02-04. So the match is quote-redesign's heuristic: same tokens and kind,
the quoted amount (exactly for sell orders, within 0.5% for buy orders, which carry
slippage buffers), and a request from ≤30 min before creation to ≤90 s after.

What is new is that the match can be **corroborated by an identity the matcher never
used**: the winning estimate's `out_amount` should be the amount stored on the order.
Measured over the matched orders of 2026-08-10..16:

| agreement between the winning estimate's `out_amount` and `order_quotes` | mainnet | bnb |
| --- | --- | --- |
| bit-exact | **8,644 / 11,092 (77.9%)** | **5,082 / 6,491 (78.3%)** |
| within 1e-4 relative | 9,537 (86.0%) | 5,488 (84.6%) |

Hence `MatchTier`: `corroborated` or `heuristic`. Both are counted; the **headline figures
and every fee substitution use `corroborated` only**. On a heuristic match the recorded
quote is not the ladder's winner, so a runner-up can beat the baseline and the
"degradation" comes out positive — 2 of 82 wins in a one-day test did exactly that.

`match_orders` reproduces the prior art: on 2026-08-12 it matched **1,852 of 2,497** orders
against quote-redesign's 1,850 of 2,495, and the estimator→address map it derives (from
corroborated matches only, so the derivation is not circular) agrees.

Match rates vary a lot by window — 74% on 2026-08-12, 47% on 2026-07-25 — and the
difference is population, not matcher: an order whose amount matches no request in the
window was not created from a fresh quote (a limit order placed from a stale one, or a
partially fillable child order). Unmatched orders contribute **zero** to every quote
figure and their count is reported, following D23's rule for unmeasured volume.

## What the quote side computes

`loo/quotes.py`, per matched order and removal scenario (`diff_quote`):

| leaf | meaning |
| --- | --- |
| `unaffected` | the removed solver did not win this quote — removing a loser cannot move the winner, and cannot change the winner's verified class either. ~88% of a solver's appearances |
| `no_quote` | it was the only usable estimator: **the order is never created** |
| `class_flip` | it was the only *verified* estimator: the quote survives unverified, so the nominal amount may rise while the quality falls |
| `degraded` | the quote falls to the runner-up's effective amount |
| `unpriced` | the `autopilot` rule had no native price for the out token |
| `nonpositive` | the gas cost swallows the input on one side: no ratio, and no replacement quote for the fee path either, since `fees._adjust_quote_to_order_limits` would refuse the same arithmetic |
| `price_suspect` | the order's two native price feeds disagree past `PRICE_IMBALANCE_THRESHOLD`: out of every figure, weight zeroed |
| `implausible` | the replacement quote is more than `IMPLAUSIBLE_QUOTE_FACTOR` (2x, D14's constant) off the recorded quote's rate in either direction: a broken estimate promoted to winner — an estimator that mis-scaled a token's decimals answers a buy request with a cost of a few atoms, and once the winner is removed that answer *wins*. Out of the bps and fee figures; counted with its volume |

The three guard leaves are reached only **after** the removal is known to have moved the
quote. The `implausible` one was forced by the complete link: with every competition in
reach, one BitgetWallet buy order's replacement cost three atoms and read as +1.16e15 bps,
1.45 billion bps of the window figure. Over July mainnet the guard takes 4–17 wins per
quoting solver — under 0.5% of the degraded volume everywhere but Wraxyn (1.5%) — and
those few were the −9,7xx bps orders that had been 86–99% of the concentrated rows. They are excluded *wins*, and a win the removed solver never had must stay
`unaffected` however suspect its price — the first implementation tested the price guard
first, and every matched price-suspect order (~140 in July) counted as a win of every
solver in the window, most of the displayed figure for the four smallest quoters.

Three figures lead, one per question:

1. **`window_bps`** — Δ effective quote summed in native over the window's whole quoted
   volume, the quote-side analogue of D23. `conditional_bps` — the same movement over the
   volume of the orders whose quote moved — sits beside it, labelled, never instead of it.
2. **`no_quote` and `no_quote_share`** — orders that would not have been created, over
   every order the quote window created.
3. **The fee**, which is *not* a quote-side figure at all: see below.

`class_flip` is deliberately **outside both bps figures** and carries its own count and
volume. It can read as a large improvement while being a degradation in kind: in a one-day
test a single flip (+70 bps on 5 ETH) supplied the entire conditional figure against 81
genuinely degraded orders.

## The fee: one number, in the place it already was

The price-improvement fee is charged against the effective amount of the best quote, and
`fees.py` already computes exactly that — `_adjust_quote_to_order_limits` is
`buy × (sell − fee) / sell` scaled to the order's limits, with
`fee = gas_amount × gas_price / sell_token_price`. A different quote is therefore not a
new fee model; it is the same function with a different `Quote`.

So the quote side hands `QuoteAnalysis.counterfactual_quotes` to `analyse_auction`, which
values the counterfactual executions **twice**: with the recorded quotes — that diff is the
fee delta the report carries, what the removal does through who executes — and with the
replacements, whose difference on the recharged orders is `quote_fee_recharge`, the quote's
own fee effect. The two are reported apart and never added (D28, revised 2026-09-07): a
worse quote lowers the baseline every price-improvement fee is measured against, so the
same executions pay *more*, and folding that into the fee column showed the protocol
gaining from losing its best quoters — Kipseli's fee delta read +$19k while its solve-side
effect was −$8k. `quote_substituted_orders` says how many **distinct executed orders**
the recharge came from — orders the counterfactual side executed with a price-improvement policy
to charge, which is exactly the set through which the effect entered; an order in a
losing bid, or without such a policy, carries a replacement quote and moves nothing.

### In every auction that executed the order, whether or not the solver bid there

The quote winner often does not bid on its own order — `tsolver` was absent from the
solve competition on 52% of the orders it quoted over one August week, `dFusion` on all of
them. The fee reference moved in those auctions all the same, so `analyse_auction`
arbitrates an auction the removed solver never bid in whenever an order in it carries a
replacement quote (D33). Nothing but the fee of the recharged orders can differ between
the two sides there, so only the fee sums and the quote counters see such an auction:
its recharged orders' fees enter the base and counterfactual sums, and it is retained in
`changed` — the per-auction moves are what `compare` converts to USD, and a fee delta
absent from them would be absent from the page. `auctions_quote_only` counts them.

The first implementation returned all-zero deltas for solver-absent auctions, which
silently dropped the quote effect wherever the quoter did not also happen to bid.

### What is held fixed, what is recomputed, and why the user effect is not a surplus

One rule (D32): everything the solvers *did* — bids, executions, ranking — stays as
recorded; everything the mechanism *computes from the quote* — the fee — is recomputed.
The user-price effect of a replaced quote is the shown-price change the quote side
reports, never a surplus recomputed against the limit the user would have signed. Surplus
is measured against the limit, and a worse quote weakens the limit, so recomputing it
would report a user *gain* on the identical execution precisely when the user was shown a
worse price — and would double-count the same movement, once down in the quote figure and
once up in Δsurplus.

Two facts make holding executions and ranking fixed sound rather than merely convenient,
and both are checked per auction rather than assumed (D34, `quotes.shifted_limit`):

- **A worse quote weakens the limit**, so every recorded execution that cleared the real
  limit clears the counterfactual one by construction. The only exception is a class
  flip, whose replacement may quote *more*; `quote_limit_unclearable_orders` counts the
  recharged orders whose execution would not have satisfied the limit the replacement
  implies — the user's slippage wedge (signed limit rate over the recorded effective quote
  rate) applied to the replacement's rate. That wedge is the one model in the check.
- **The ranking channel is second order.** A weaker limit raises the surplus the mechanism
  would have *measured* on every solution holding the order by nearly the same amount, so
  rankings among them barely move. `auctions_quote_shift_would_change_winners` re-arbitrates
  the counterfactual side with every score shifted by exactly that amount and counts the
  auctions where the winner set moves, over `auctions_quote_checked` — every auction where
  some bid's order carried a replacement quote; `quote_shift_auctions` carries their ids so
  a flagged auction can be opened. Scores are never recomputed from a counterfactual
  quote (D1); this is a validation count, and a large one would be a finding about the
  design rather than a number to adjust by. On July 2026 mainnet it fired in 122 of
  Tsolver's 4,841 quote-touched auctions and in 7 elsewhere; where inspectable, those
  were close races with ordinary replacement quotes (median worst −22 bps), not extreme
  ones.

Verified on a 400-auction mainnet slice, Fractal removed:

| | solve only | with the quote side |
| --- | --- | --- |
| Δsurplus | −0.011873 ETH | −0.011873 ETH (identical) |
| Δpartner fees | −0.0000048 ETH | −0.0000048 ETH (identical) |
| Δrewards | −0.001068 ETH | −0.001068 ETH (identical) |
| **ΔDAO fee revenue** | **−0.002879 ETH** | **−0.002073 ETH** |

Surplus, partner fees and rewards are untouched — partner fees are volume policies and do
not see a quote, which is a useful internal check — and the DAO fee delta moves by
+0.000806 ETH: without Fractal the quote reference is worse, so the same executions carry
a larger price-improvement fee.

Two limits ride with this, both deliberate:

- **The quote does not move the ranking.** Solution totals stay the recorded scores (D1),
  so a counterfactual quote changes what the mechanism *charges* on an execution, never
  who won it. Recomputing every score from a counterfactual quote is what D1 exists to
  refuse; the ranking check above says how often that would have mattered.
- **Only corroborated matches may move a fee.** A heuristic match would put a different
  request's runner-up against the recorded winner's amount and book the match error as
  revenue.

A replacement quote the fee path cannot charge against — its fee exceeds its own sell
amount — does not fail the valuation: `value_solution` records that order's fee as unknown.
`analyse_auction` detects an order whose fee is unknown on the counterfactual side and was
not in the baseline, keeps the recorded quote for it alone, and values the auction once
more, so one unusable replacement does not make the whole auction unpriceable. The
`nonpositive` leaf already withholds exactly these quotes, so the path is a backstop.

### The gas convention is the same on both sides, measured

The recorded quote's fee comes from `order_quotes.gas_amount` while a counterfactual
quote's comes from the replacement's ladder `gas`, so the two could in principle be on
different conventions (post- versus pre-verification). Measured over 1,307 corroborated
matches on 2026-07-25, `recorded_gas / ladder_gas` is **exactly 1 for 78.3%** and 1.0000
at every quantile from p10 to p99 (mean 0.9990, p1 = 0.9940). The asymmetry is real and
negligible.

## Precision: the quote side is not integer-exact

Amounts in `gold_quotes` are `Float64`. An 18-decimal amount above ~0.01 tokens is already
past `2^53`, so **nothing derived from the ladder is exact to the atom** — see
`ch.as_float`, and note that ClickHouse's JSON also returns large `Float64` values as
integer-looking numbers, which coerce silently to `int` and then compare unequal to their
own float.

This is confined by design. The recorded quote, its fee, every solve-side number and every
figure the fee path produces come from Postgres and stay exact; the ladder supplies only
the counterfactual (Q-D5). Counterfactual quote amounts are **rounded, not floored** —
flooring would bias every counterfactual quote down by up to an atom and so bias every fee
delta up.

## Out of scope, and named as such

- **Quote rewards (CIP-72).** The counterfactual quote's eligibility is not evaluated, so
  no reward figure includes it. `int_backend_data__orders_with_winning_quotes` carries
  `is_eligible_for_quote_reward` and quote-redesign reproduces it at 100%, so this is a
  milestone, not a wall.
- **Conversion.** A worse quote means some users never place the order, and there is no
  counterfactual for an order nobody created. At a 0.13% conversion rate any elasticity
  proxy would be a number people quote and nobody can defend.
- **The limit price and fillability.** A better quote makes the order harder to fill, so an
  aggressive quoter can price orders into existence that nothing fills. Measuring it means
  re-running the solve counterfactual with perturbed limits.

## Traps

1. **`price_rank` / `behind_winner_bps` are gross.** They disagree with the actual pick on
   48% of a day's mainnet requests. Never use them as a delta.
2. **`request_id in (…)` is not a key prefix.** Bound it by day or it scans.
3. **The `_live` views are non-deterministic.** Use the base tables.
4. **A per-request median gap is meaningless across estimators.** One estimator's 300 k
   tiny UI polls against another's 60 k is not a comparable base; quote figures are
   volume-weighted over matched orders.
5. **`quote_id is not null` is not selective** — 1.1 M of 1.5 M mainnet requests a day have
   one. It marks "a quote was computed", not "an order was created".
6. **Removing the only verified estimator can *improve* the nominal quote.** If a bps
   figure comes out positive, look for a class flip before looking for a bug.
7. **A guard leaf is an excluded win, not a classification of the order.** Test whether
   the removed solver won the quote *before* testing the price guard; the other order of
   tests made every suspect order a win of every solver.
8. **The quote winner need not bid on its order.** Any fee effect wired through
   `analyse_auction` has to survive the solver-absent early return, or it is dropped
   exactly where the quoting and solving solvers differ.
9. **A request with no ladder is not a matching failure.** The streaming endpoint records
   requests without estimates or a winner; count those before reading a coverage or
   reproduction drop as a rule change.
10. **Duplicate rows are normal on fresh data.** `ReplacingMergeTree` collapses them only
    on merge; dedupe by key or `recorded_winner` goes ambiguous.
11. **"Nothing found in `fct_quote_requests`" does not mean nothing was quoted.** The
    gold tables are built off the request event and drop competitions without one —
    0.6% of requests, a quarter of one day's orders. Search `raw_quotes.price_estimates`
    grouped by `request_id` before calling an order unquoted.
12. **A ladder can hold a broken estimate, and removal promotes it.** The pick maximises,
    so a nonsense answer that lost to the winner wins once the winner is gone; read as a
    price change it is millions of basis points. Guard the *replacement* against the
    recorded quote (2x either way), not just the recorded price against a second feed.
13. **Never filter candidates on `quote_id`.** The competitions the gold layer never
    received have no `quote_computed` either, so a null id says nothing about whether the
    competition priced an order. `price_quality != 'fast'` is the one filter that belongs:
    `order_quotes` never holds a fast quote, and a fast request sharing the amount is the
    UI's poll, not the order's quote.
14. **The quote side's price guard is one-sided, and on bnb that breaks every bps
    figure.** `price_suspect_orders` compares the api's `sell_token_price` with the
    auctions' median price for the *same* sell token. When both feeds carry the same
    wrong price, it passes. bnb 2026-10-05..12: 26 orders, all valued above 1,000 BNB
    and most from Trust Wallet (token `0x4078920b…` at 0.1006 BNB in both feeds), carried
    28.68M of the window's 28.70M BNB "executed volume". The solve side's whole week was
    16,988 BNB. They pushed every solver's `window_bps` toward zero (Rizzolver −0.01
    raw vs −11.40 cleaned) and put two of them into Nexroute's numerator (−62.6 bps raw vs
    −3.8 cleaned). The solve side's two-sided rule, applied per order (sold value vs
    bought value at auction medians, 2x), flags 1,444 traded orders (5.2%), holding 99.94%
    of that volume. That leaves 17,492 BNB, close to the solve side's total. Base had
    nothing to flag. Until the guard is two-sided in `loo/`, do not read bnb's quote rows
    from `compare`. Rebuild them instead: reload the window's orders with
    `load_recorded_quotes`, check that the non-suspect sum reproduces
    `window_volume_native` exactly, drop the orders that fail the two-sided rule from
    both the numerator and the denominator, and recompute from the report's per-order
    `diffs`. The fix in `loo/` should use the **per-auction** prices of the auctions an
    order traded in, not window medians, because medians over-exclude tokens whose price
    moved during the window. USDT→`0xfb4f81f5…` (225 bnb orders) failed at medians, while
    its per-auction values matched the trade-implied rate at 1.00x.
