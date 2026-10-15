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

### Coverage: ten chains, each measured before it was enabled

Prod requests begin **2026-07-22 15:28** on mainnet (chain 1) and bnb (56) and **2026-09-01
16:00** on every other chain the protocol runs on (base 8453, arbitrum-one 42161, xdai
100, polygon 137, avalanche 43114, ink 57073, plasma 9745, linea 59144; measured
2026-10-14). There is no `TTL` in the DDL and the starts have not moved since, so these are
backfill boundaries rather than a rolling window — but `ch.Client.coverage` queries them
rather than assuming, because one moved once already. lens and sepolia have no data.

A chain is enabled in `loo.ch.NETWORK_CHAIN_IDS` only after its link and its pick
reproduction were measured, on both links where its data spans the cut-over
(`QUOTE_ID_LINK_START`). Over 2026-10-05..12 (quote id) and 2026-09-22..29 (heuristic):

| network (chain) | quote-id link, 10-05..12 (non-trivial orders) | pick reproduced | heuristic link, 09-22..29 (corroborated) | pick reproduced |
| --- | --- | --- | --- | --- |
| mainnet (1) | 25,881 / 25,881 (100%) | 25,722 / 25,723 (100.00%) | 34,198 / 34,217 (99.9%; 23,341) | 24,277 / 24,319 (99.83%) |
| bnb (56) | 28,102 / 28,102 (100%) | 26,991 / 26,991 (100.00%) | 44,095 / 44,115 (100.0%; 18,424) | 22,275 / 22,276 (100.00%) |
| base (8453) | 17,068 / 17,068 (100%) | 17,057 / 17,057 (100.00%) | 24,524 / 24,528 (100.0%; 22,854) | 23,684 / 23,695 (99.95%), 2 ms-tie |
| arbitrum-one (42161) | 12,097 / 12,097 (100%) | 12,018 / 12,018 (100.00%) | 15,710 / 15,710 (100.0%; 14,035) | 14,934 / 14,939 (99.97%), 2 ms-tie |
| xdai (100) | 9,301 / 9,301 (100%) | 9,298 / 9,298 (100.00%) | 13,013 / 13,013 (100.0%; 12,854) | 12,991 / 13,008 (99.87%), 16 ms-tie |
| polygon (137) | 2,403 / 2,403 (100%) | 2,363 / 2,363 (100.00%) | 2,823 / 2,824 (100.0%; 1,644) | 1,868 / 1,868 (100.00%) |
| avalanche (43114) | 797 / 797 (100%) | 797 / 797 (100.00%) | 933 / 933 (100.0%; 611) | 734 / 735 (99.86%) |
| ink (57073) | 523 / 523 (100%) | 522 / 522 (100.00%) | 517 / 517 (100.0%; 515) | 510 / 510 (100.00%) |
| plasma (9745) | 119 / 119 (100%) | 119 / 119 (100.00%) | 148 / 148 (100.0%; 146) | 148 / 148 (100.00%) |
| linea (59144) | 24 / 24 (100%) | 24 / 24 (100.00%) | 21 / 21 (100.0%; 19) | 19 / 19 (100.00%) |

`ms-tie` counts ties the gold layer's millisecond timestamps resolved to an equivalent
estimate other than the recorded one — not failures. The remaining disagreements (none to
42 per chain-week; mainnet's heuristic week is the lowest rate, 99.83%) were inspected only
on the quote-id weeks of bnb and mainnet, where all of bnb's were ties and mainnet's one is
real (`fractal` nets better at the recorded gas price; `baseline` won).

Read the small chains for what they are: linea quoted 24 orders that week with two
estimators, plasma 119 — a 100% rate there says the rule holds, not much more. The
heuristic's corroborated share is the number to watch on a pre-cut-over window: on
polygon it is 58%, and the headline figures are over the corroborated tier only (D29).

`NETWORK_CHAIN_IDS` is the support test: a network absent from it has no quote data, and
`analyse --quotes` refuses it instead of reporting empty quote columns, which would read
as "this solver quoted nothing" (Q-D10).

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

`services/crates/price-estimation/src/competition/quote.rs`, since **services#4846**
("rank quotes using native sell token price", merged 2026-09-02):

```
is_reasonable          drop any estimate with gas == 0 or out_amount == 0
compare_quote_result   a verified estimate beats an unverified one at any amount
compare_quote          otherwise rank on effective_out_amount:
effective_out_amount   cost = gas × gas_price / native_price(sell_token)   (sell atoms)
                       sell:  out − cost × out/in   → maximise
                       buy:   out + cost            → minimise
                       negative or NaN → 0; otherwise truncated to an integer
results.sort_by        stable, over the answers in completion order
```

Before #4846 the comparison was `effective_eth_out`: `out_amount × native_price(out_token)
∓ gas × gas_price`, in wei, clamped and truncated the same way. `PriceRanking::BestBangForBuck`
is what the prod `optimal` estimator is built with.

### What the ladder records, and what it does not

Verified against the Rust on 2026-09-02 (`services` at d5bf0f6) and again on 2026-10-14
(a77ae6461, with `origin/main` at a6ae72eb6 for what landed since); each of these matters
for reading a reproduction rate:

- **Ties break by arrival order, and arrival order is recorded.** `results.sort_by(…)` is
  a stable sort over the answers in the order estimators *completed*, so an exact tie goes
  to whichever answered first. The raw layer's `nats_seq` is that order: on the five bnb
  ties inspected the recorded winner was the lowest `nats_seq` every time, and the
  millisecond `event_timestamp` agreed. The gold layer has only `estimate_timestamp`, to
  the millisecond, and a trivial unwrap's twenty answers share one. Ladders are loaded in
  arrival order (`QuoteRequest.estimates`) and `pick` keeps the first of equal keys.
- **Ties are common, because of the clamp and the truncation.** Every sell estimate whose
  gas exceeds its value is worth zero, and every value is truncated to an integer before
  comparing — atoms of the out token since #4846, so on an out amount of 35 atoms, 32.79
  and 32.24 effective atoms are both 32. `rank_key` mirrors both. Without them, a dust
  request is decided by the gas difference prod could not see; with them, arrival order
  decides as it did in prod (195 ties on bnb 2026-10-05..12).
- **Errored estimators leave no trace.** `emit_quote_event` fires only on `Ok`, so "the
  removed solver was the only usable estimator" and "the others errored on this request"
  are the same ladder.
- **Zero-gas and zero-amount estimates are in the ladder but never won.** The event is
  emitted before `is_reasonable` filters, which is why `Estimate.reasonable` has to be
  applied here too.
- **The streaming endpoint is recorded since services#4957 (2026-09-23), and its orders
  can carry a non-winning estimate.** `/api/v1/quote/stream` runs the same competition,
  forwards each answer that beats the best one sent so far, and — since #4957 — emits a
  `priceEstimate` per answer and, once every estimator has answered, the final best as
  the `winningPriceEstimate`. A client that takes a forwarded quote before the stream
  ends places its order from an *earlier* answer; a client that drops the stream early
  leaves the competition without a winner. Before #4957 a streamed request was in
  `fct_quote_requests` with no ladder at all. See
  [streamed quotes](#streamed-quotes-the-order-takes-what-the-stream-showed-not-the-winner).
- **Prod compares `f64` values.** `f64::from(out_amount)` loses precision above 2^53 before
  the comparison happens, so a `Float64` gold ladder is not a degradation relative to
  what prod computed with. The raw ladder's exact integers are compared through the same
  float arithmetic in `rank_key`.

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

### The two ranking rules, and how the default became the protocol's own

`loo.quotes.RANKING`:

- **`effective-sell`** (default) — rank on the effective quote netted in the **sell**
  token, at the order's own recorded `gas_price / sell_token_price`, truncated to whole
  atoms. Since #4846 this *is* the protocol's rule, in the same order of operations; it
  needs nothing that is not on `order_quotes`, and is the same quantity
  `fees._adjust_quote_to_order_limits` charges the price-improvement fee against, so the
  pick and the fee cannot disagree.
- **`autopilot`** — the out-token rule from before #4846, which needs the **buy** token's
  native price for sell orders (taken from the window's auction prices, median per
  token, and divided down from the feed's 1e18 scaling — before M16 it was not, so gas
  never counted on a sell order under this rule). The cross-check.

Reproduction of the recorded winner, order-creating requests:

| rule, window | mainnet | bnb | base |
| --- | --- | --- | --- |
| `effective-sell`, 2026-08-10..16 (heuristic link) | **10,861 / 10,906 (99.6%)** | **6,395 / 6,397 (100.0%)** | — |
| `effective-sell`, ignoring the class, 2026-08-10..16 | 9,225 (84.6%) | 6,162 (96.3%) | — |
| gross `out_amount` only, 2026-08-10..16 | 6,707 (61.5%) | 5,773 (90.2%) | — |
| `effective-sell`, 2026-10-05..12, M15 (heuristic link, no clamp or truncation) | — | 27,599 / 28,004 (98.55%) | 16,952 / 17,091 (99.19%) |
| `effective-sell`, 2026-10-05..12, M16 (quote-id link, prod's ties) | **25,722 / 25,723 (99.996%)**, 185 ties | **26,991 / 26,991 (100.0%)**, 196 ties | **17,057 / 17,057 (100.0%)**, 113 ties |

The two rules agree because a quote asserts `in × sell_price ≈ out × buy_price`: the
sell-token netting **scales** the amount by `(1 − gas·gas_price/(in·sell_token_price))`
while the out-token one **shifts** it by `gas·gas_price/native(out)`, and those coincide to
first order. They diverge only through the quote's own pricing error. On a **buy** order
the out token *is* the sell token, so the two rules differ there only in the unit they
truncate in.

Every run reports its own reproduction rate (`quote pick reproduced the recorded winner`).
A window where it drops means the rule changed and every quote figure is then suspect —
or, as the next section shows, that a tie-break the record holds was not being read.

### Why bnb's reproduction fell to 98.55%, and what it was

The 2026-10-05..12 bnb run reproduced 27,599 of 28,004 recorded winners, against 100.0% on
2026-08-10..16. It was neither a link error nor a rule change. Of the 20 disagreements
the report lists, **15 were trivial unwraps** — WBNB→BNB, where all twenty estimators
answer the identical amount at the identical gas and the winner is the first to answer
(`baseline` in every one listed; the heuristic had picked whichever estimator ClickHouse
returned first) — and **5 were dust requests**, four of them selling 0.0005–0.006 USDT,
where every estimate was under water, so prod clamped them all to zero and took the first
answer (`nexroute` or `wraxyn`, by `nats_seq`) while our unclamped key preferred the
least-negative one. bnb unwraps went from 5 on 2026-08-12 to 284 in the week of
2026-10-05, which is why the rate fell now.

Three changes take it to **26,991 of 26,991** (re-run with every M16 change): trivial unwraps are no longer linked at
all (`TRIVIAL_QUOTE_SOLVER`); `rank_key` clamps and truncates as prod does; and ladders
are loaded in arrival order. The truncation alone accounted for the last six: equal out
amounts of 35–5,974 atoms whose gas-netted values truncate to the same integer, decided
in prod by who answered first. On the quote-id link the ladder comes from the raw layer,
so arrival order is exact; on the heuristic link's gold ladders it is to the millisecond,
and `PickValidation.tie_mismatches` counts the ties that went to an equivalent estimate
other than the recorded one.

## Quote ids link an order to its competition exactly — from 2026-09-29

Everything from the next section to the end of [the heuristic join](#the-order--request-join-is-a-heuristic-with-an-exact-corroborator)
is how the link has to be reconstructed for **older windows**. From 2026-09-29 it is a
join (`quotes.link_by_quote_id`, M16), because two backend changes landed:

- **The order records its quote id.** `order_quotes.quote_id` is the `quotes.id` the order
  was placed with — for API orders (`orderbook.rs`, `add_order`) and ethflow orders (the
  on-chain `quoteId`) alike, not only fast-path ones as the V125 migration comment says.
  Populated from **2026-09-22** (about half that day, then every row).
- **Every estimate carries its own id and solver.** Since `services#4925` the orderbook
  draws one id per estimator *before* quoting and stores the winner under its own id;
  `services#4966` publishes that id and the estimator's settlement address with each
  estimate: `raw_quotes.price_estimates.{quote_id, solver}` and the same pair on
  `winning_price_estimates`. Populated from **2026-09-29** (390 k of 1.32 M mainnet winners
  that day, ≥ 99.8% from 2026-09-30).

**A quote id names one solver's answer, not a competition.** Before #4925 an id was
allocated once per request, *after* the answers were evaluated, so it did name the
competition's result. Since #4925 the orderbook draws N ids for a request with N
estimators before quoting, hands each estimator its own, and stores a quote under the id
of the answer it came from. So the six answers to one bnb request carry six ids
(256304375–396, interleaved with ids drawn for a concurrent request). The competition
itself is the `request_id`. `order_quotes` holds exactly one id, the one of the answer the
order used. Every other answer of the competition, the winner included where it is a
different one, is reached only through the request.

So the link is `order_quotes.quote_id` → the `price_estimates` row carrying that id →
its `request_id` → every estimate of that request. Measured on mainnet 2026-10-07:

- every successful estimate's id is distinct, and the winner event's id equals its
  estimate's in 87,214 of 87,214 joinable cases (12:00–13:00);
- each estimator has exactly one settlement address per chain (15 on mainnet, 16 on bnb),
  so `solver` replaces the estimator → address map `match_orders` derives — and maps every
  estimator, including ones that never won a corroborated match;
- only trivial ETH/WETH estimates lack an id and a solver (0.18% of estimates);
- the raw amounts are exact decimal strings, so the ladder no longer needs `Float64`.

Per-estimate ids appear on **every chain at the same instant**, 2026-09-29 17:16 UTC — one
deploy. Measured from `raw_quotes.winning_price_estimates` per chain and day: no id on
2026-09-28 anywhere; the first at 17:16:14–15 on 2026-09-29 on mainnet, bnb, xdai, base
and arbitrum-one, and within seconds to two and a half minutes on the quieter chains
(linea last, 17:18:46); from 2026-09-30, 98.3% of avalanche's winners carry one and ≥ 99.1%
on every other chain. The order
side's link window is the creation day's, −30 min / +90 s, and on bnb 2026-10-08 the
linked estimate preceded its order by 2.3 s at the median and 119 s at most. So
`QUOTE_ID_LINK_START` is **2026-09-29 17:20 UTC**: an order created from then on is linked
by id, an earlier one by the heuristic, and a window straddling the instant uses both and
reports each (`quote_window.links`, the `order↔competition link` row of `compare`).

`gold_quotes.fct_quote_estimates.quote_id` is **not** the same column: it is request-level
(one id shared by every estimate of the request in 12,915 of 12,960 requests), and the gold
layer still lacks the request-less competitions. Link through the raw table.

| | mainnet 2026-10-07 | bnb 2026-10-08 |
| --- | --- | --- |
| orders (`load_recorded_quotes`) | 3,783 | 2,972 |
| linked by quote id | **3,753 (99.2%)** | **2,950 (99.3%)** |
| … whose id's estimate is the competition winner and reproduces the stored amount to the atom | 3,753 | 2,949 |
| not linkable: a trivial unwrap (WETH→ETH, WBNB→BNB), solver `0x0`, no competition | 29 | 22 |
| not linkable: ethflow order with `quote_id = 0` | 1 | 0 |
| heuristic chose the same competition | 3,264 | 2,471 |
| heuristic `corroborated`, **different** competition | 198 (5.7% of corroborated) | 126 (4.8%) |
| heuristic `heuristic`, different competition | 291 (all of the tier) | 352 (all) |

On mainnet the linked estimate's `solver` and `gas` also equal `order_quotes.solver` and
`gas_amount` in 3,753 of 3,753.

### What the heuristic got wrong, measured against the ids

- **The whole `heuristic` tier is the placement-time re-quote.** The order's competition
  ran 0–4 s before creation (median 1 s) and exists only in the raw layer, but
  `load_raw_candidates` is asked only for orders with *no* gold candidate, so an order
  with an earlier UI request never reaches it. These were excluded from the headline
  figures; the id link brings them in.
- **A `corroborated` match can be the wrong competition** — this one reached the headline
  figures. The order was placed from a quote 0–111 s before creation (median 31 s) while
  the UI kept polling; a later poll returned the same winning amount and "latest
  corroborated" took it. The two ladders differ in 187 of 198 cases. Requiring the
  winner's `gas` to equal `order_quotes.gas_amount` as well would have demoted 68 of 198
  (mainnet) and 29 of 126 (bnb) with no false rejection — every right match passes — so
  it is a free improvement for windows before the ids, not a fix.
- **Trivial unwraps were counted as competitions.** Every order with
  `order_quotes.solver = 0x0` on the two days is an unwrap (29 WETH→ETH, 22 WBNB→BNB);
  the heuristic matched each to a ladder of identical answers and credited whichever
  estimator answered first. With the ids they are recognisable as what they are — no
  competition, so no removal can move them.

One day's effect on the per-solver `window_bps`, mainnet: Rizzolver −1.56 → −2.56,
Kipseli −0.39 → −0.42, Wraxyn −3.85 → −3.90, the rest within 0.002. On bnb the largest
moves are Wraxyn −0.08 → −0.12 and Rizzolver −3.04 → −3.13. A direction, not a revised
figure.

### What the link reads, and what it refuses

`link_by_quote_id` resolves every order's id to its request inside one ClickHouse round
trip per creation day and table (a `request_id in (select request_id … where quote_id in
(…))` subquery), loads the ladder from `price_estimates` with `nats_seq` and the winner
from `winning_price_estimates`, and keeps a link only when the linked estimate **is** the
recorded winner **and** reproduces the order's stored amount to the atom. Everything it
links is therefore `corroborated`. An order that fails because its id names the wrong
quote, or none, gets a second pass ([the re-quote](#an-ethflow-orders-stored-quote-can-come-from-the-backends-own-re-quote));
the reasons the rest were not linked are reported.

### Every order accounted for

2026-10-05..12, every chain with quote data, re-run 2026-10-14. Each order created with a
recorded quote is exactly one of: an unwrap (no competition), linked by its quote id to a
one-shot competition, linked through a stream, or linked to the backend's re-quote.
**Every non-trivial order is linked, on every chain.**

| chain | orders | unwraps (solver `0x0`) | linked, one-shot | linked through a stream (of them before it finished) | linked to the re-quote | link rate, unwraps aside |
| --- | --- | --- | --- | --- | --- | --- |
| mainnet | 26,061 | 180 | 21,457 | 4,406 (174) | 18 | 100% |
| bnb | 28,386 | 284 | 15,836 | 12,265 (308) | 1 | 100% |
| base | 17,115 | 47 | 16,550 | 513 (30) | 5 | 100% |
| arbitrum-one | 12,140 | 43 | 11,851 | 246 (93) | — | 100% |
| xdai | 9,303 | 2 | 9,301 | — | — | 100% |
| polygon | 2,408 | 5 | 2,044 | 358 (17) | 1 | 100% |
| avalanche | 798 | 1 | 762 | 35 (0) | — | 100% |
| ink | 523 | — | 523 | — | — | 100% |
| plasma | 122 | 3 | 119 | — | — | 100% |
| linea | 24 | — | 24 | — | — | 100% |

"Before it finished" counts two kinds of order. Some took an answer before the stream's
final one: Trust Wallet's, API and ethflow alike. Others came from a stream dropped
before any winner was recorded: arbitrum's 72 `minimal-2` orders. Ethflow orders are owned
by the CoW ethflow contract (`0xba3cb449…` on every chain, also `0x40a50cf0…` on mainnet).
Order counts are a few higher than in earlier runs of the same week: orders created in a
window keep arriving for a day or so.

Against M15's heuristic over the same week, the link went from *matched* to *exact*.
M15 matched nearly every order (bnb 28,373 of 28,374, base 17,108 of 17,110), but only
corroborated matches may feed a figure. That was 92.0% of bnb's orders and 73.1% of
base's, and 4.8–5.7% of those were the wrong competition. M16 links 99.0% and 99.7% of
all orders: every one but the unwraps, every link exact.

The quote solver recorded on the order equals the linked winner's `solver` on every
linked order, and the estimator map read from the ladders covers every estimator. That
mattered for `baseline` on bnb: it wins 141 linked orders that week, each carrying
Baseline's address as quote solver, but the M15 run reported Baseline with **no** quote
wins. The heuristic derived each estimator's address from its corroborated wins, and
baseline's were mostly unwraps, which it answers first and whose quote solver is the zero
address.

### Streamed quotes: the order takes what the stream showed, not the winner

`/api/v1/quote/stream` runs the same competition as the one-shot endpoint but sends the
client every answer that beats the best one so far, as it arrives. Since services#4957
(2026-09-23) each answer it sends is stored under its own quote id and recorded as a
`quote_computed` row, and the final best is recorded as the `winningPriceEstimate`. The
user commits to whichever answer is on screen, and the order carries that answer's id.

**What a streamed request records.** One bnb example, order `1c54ce30…` (Trust Wallet,
2026-10-05), request `24948449…`, every row there is:

| event (raw_quotes) | time | what it says |
| --- | --- | --- |
| `quote_requested` | 00:44:54.985 | the request: tokens, kind, `in_amount`, caller, `optimal` |
| `price_estimates`, `fractal` | 55.044 | first answer, id 256304394 |
| `quote_computed` | 55.051 | id 256304394 — fractal's answer forwarded and persisted |
| `price_estimates`, `fibrous`, `bitget`, `wraxyn`, `kipseli` | 55.252–55.339 | none nets better than fractal; not forwarded |
| `price_estimates`, `nexroute` | 55.466 | id 256304387: same amount to the 15th digit, less gas |
| `quote_computed` | 55.472 | id 256304387 — forwarded and persisted |
| `winning_price_estimates` | 55.472 | nexroute, id 256304387 — the stream's final best |
| order created | 00:45:01.778 | 6.3 s later |

The order's `order_quotes` row is **fractal's quote** in every field: `quote_id`
256304394, fractal's settlement address as `solver`, fractal's `buy_amount` and
`gas_amount` to the unit, and fractal's route in `metadata`. So nothing is missing from
the record: the stored quote is a ladder row and a `quote_computed` row. Only the winner
flag points elsewhere. In the gold layer, `fct_quote_requests.quote_id` was fractal's id —
in this example, the request's *first* computed quote — and `winner` nexroute.

`quote_computed` is therefore the stream's own log: one row per answer sent, in order,
where a one-shot request has a single row. On bnb that week, 12,265 of 28,098 orders with
a quote id (44%) came from requests with more than one row, nearly all of them Trust
Wallet's. 11,957 took the last answer sent, which is the recorded winner. 308 took an
earlier one: each a strict improvement on everything before it, arriving before the
winner, with the order created 0.4–85 s after the winner arrived. On arbitrum, 72
`minimal-2` orders come from streams the client dropped 0.3 s after taking an answer,
before any winner was recorded (#4957: dropping the stream cancels the outstanding
estimators).

**The replay reproduces what the stream sent.** `replay_stream` re-runs the stream over the
arrival-ordered ladder and must reproduce the request's `quote_computed` sequence
exactly. It did on 11,508 of bnb's 11,523 streams with two or more rows before one more
rule was added. In all 15 misses, the stream's first answer had a gas cost of 1.02–1.70×
the sell amount. `estimate_stream` still forwards such an answer, and it sets the bar
the next must beat. The quoter then drops it (`SellAmountDoesNotCoverFee`), so it is never
stored, sent or recorded. With that mirrored, the replay reproduces every stream with two
or more rows on bnb (11,523 of 11,523), base (512 of 512), polygon (354), arbitrum-one
(227) and avalanche (35), and 4,343 of 4,344 on mainnet. The mainnet miss is the request
whose pick also disagrees (`fractal` nets better at the recorded gas price; `baseline` was
sent last and won).

**The counterfactual follows the stream** (`StreamTake`, `stream_replacement`, D41). An
order counts as streamed when its request has two or more `quote_computed` rows, its
answer is not the recorded winner, or its stream has no recorded winner. For such an
order:

- **Removing anyone other than the estimator whose answer the order took leaves it
  unaffected, exactly.** Every answer that arrived before the user committed and was
  not sent did not beat the one on screen, and a removal only takes candidates away.
- **Removing that estimator** gives the best of the remaining answers that had arrived
  by the time the user committed, on the same verified-first key. The commit is not
  recorded, so it is placed at the **latest moment the record allows**: just before the
  next answer the stream sent arrived (the user did not take that one), or, if theirs
  was the last, before the order was created. If no other answer had arrived by then,
  the user waits for the next one the stream would send. In a finished stream that
  answer is in the ladder. In a dropped one it was cancelled unrecorded, which is the
  `unrecorded` leaf: counted, and out of every figure.
- **The one assumption is counted.** `commit_dependent` counts the wins whose replacement
  would differ had the user committed the moment their answer was sent rather than at
  the latest moment. It is a check on how much the commit time matters, not a second
  scenario.

**A removal can improve a streamed order's quote.** If the answer the user took was a
fast, poor one, removing its solver brings forward a better answer that would otherwise
have arrived a moment later. On base, order `676200aa…` (2026-10-09) carries baseline's
answer, the stream's first at 14:56:14.472: 175.6 M out. Horadrim's arrived 19 ms later
at 241.7 M (+37.6%), and three more after it, the last of them the winner. The order was
created 7 s after the first answer. The client took the stream's first answer, which
some clients evidently do. Without baseline, the stream's first answer would have been
horadrim's. So Baseline's base figure is positive (+32.3 bps over 239 moved orders, 92%
from that order). That is not a sign error: under the stream rule a solver that answers
first and badly makes early-committing users worse off, and removing it shows that.

**How much the commit time matters, measured.** On bnb, every one of the 11,957 orders
that took the stream's final answer was created after the *whole* stream had arrived.
That was 2.2 s after its last answer at the 1st percentile, 12 s at the median and 24 s
at the 90th (3,000 sampled). The tail of a stream after its final best is about half a
second. A commit inside that tail would have needed the user to sign within a fraction
of a second of the last improvement, so for these orders the latest commit is close to
certain, and the rule is the full-ladder rule they had before. `commit_dependent` is
nonetheless large for solvers that answer early and well (bnb Nexroute: 2,395 of 3,650
streamed wins), because their answer is often the final one and five or more answers
arrive after it. Read it as how often answers after theirs could have mattered, not as an
uncertainty in the figure. For the 308 orders that took an earlier answer, the window is
narrow: no other answer arrived between theirs and the next one sent on 146, one on 51.

### An ethflow order's stored quote can come from the backend's own re-quote

An ethflow order is placed on chain with the UI quote's id, or with `0`. The backend
indexing it re-quotes when that quote cannot be used, and stores the result on the order,
while `order_quotes.quote_id` keeps the UI's id. The re-quote is a full competition run
internally:

- every estimate has **no `request_id`** and `caller = 0x0`, so it is in neither gold
  table and invisible to the request join;
- the estimates carry quote ids from one block the orderbook drew together (1608345417–437
  for one 11-estimate mainnet competition) and arrive within a second;
- a request-less `winningPriceEstimate` names the winner under its own quote id.

All 25 of that week's ethflow orders the id join missed had one, with a winner
reproducing the stored amount to the atom, 1–3 s after the order's block time. The 25
were 18 on mainnet (13 whose id named the UI quote, 5 placed with `quote_id = 0`), 5 on
base, 1 on bnb and 1 on polygon, mostly OneKey. The stored amount differed from the UI
quote's by −0.2% to +0.4%; on the polygon order it differed by +9.8%.

`_link_requotes` finds the estimate by the order's request key and exact stored amount
among request-less estimates. It requires that estimate to be the recorded winner, and
takes as its competition the request-less estimates within 5 s and 64 quote ids of it.
The order is linked to *that* competition, not the UI's. The recorded quote is the
baseline of every figure and the fee is charged against it, so its counterfactual has to
come from the competition that produced it, as on every other link. The quote the user
saw is the UI's, seconds older and within a fraction of a percent of the re-quote here.
The pick reproduces on these ladders as on the rest.

### Two traps on the order side

- **`dbt.stg_backend_data__order_quotes.quote_id` is NULL for most rows before
  2026-10-06.** The model is incremental with `append_new_columns`, so rows built before
  the column existed were never refilled: ~1% non-null 2026-09-22..10-05, 100% after.
  `raw_prod_<chain>.order_quotes.quote_id` is complete from 2026-09-22. Read the raw
  mirror, or have the staging model fully refreshed.
- **An ethflow order can carry the UI quote's id while its stored quote was recomputed**
  when the order was indexed (cow-dagster, `int_quote_data__orders_without_competition`).
  Its id's estimate then does not reproduce the stored amount: 1 of 47 linked ethflow
  orders on bnb 2026-10-08, 0 of 203 on mainnet 2026-10-07. The recomputation is a
  request-less competition of its own, and the order links to it
  ([above](#an-ethflow-orders-stored-quote-can-come-from-the-backends-own-re-quote)).

cow-dagster syncs these ladders into the analytics DB (`raw_prod_<chain>.quote_competition_estimates`
→ `dbt.stg_quote_data__competition_estimates`, one row per order × solver, `numeric`
amounts, from 2026-09-29), but only for **traded** orders and without estimator names or
trivial estimates. Enough for executed-volume figures without ClickHouse; the `no_quote`
count, which is over every created order, needs the untraded ones too.

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

Before 2026-09-22 neither route survives into the analytics DB: `order_quotes` has no id,
its raw `metadata` holds only interactions and JIT orders, and `public.quotes` — which does
have `id` — is a stale 5,522-row snapshot with no `order_uid`. **The association exists only
transiently inside the backend** (from 2026-09-29 it is recorded on both sides —
[above](#quote-ids-link-an-order-to-its-competition-exactly--from-2026-09-29)). So for older
windows the link has to be reconstructed, and there are six signals to reconstruct it from. They do different jobs and it matters which is used for
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

### The clean fix was upstream, and it landed

`order_quotes.quote_id` from 2026-09-22 and per-estimate ids from 2026-09-29 turn the
heuristic into a join for windows after that, at 99.2% / 99.3% of orders, the rest being
trivial unwraps with no competition and one ethflow order without an id — see
[above](#quote-ids-link-an-order-to-its-competition-exactly--from-2026-09-29).

### An aside on that `ORDER BY`

The `find` query re-ranks candidate quotes by `buy / (sell + fee)` — the sell-token
netting, agreeing with the competition to first order but not identical to it, and not the
rule the competition itself uses. It is the *attribution* step, not the pick shown to the
user, so it is not authority for the ranking here; read it as a possible inconsistency in
how quotes are attributed to orders rather than as a second definition of the competition.

## The order ↔ request join is a heuristic with an exact corroborator

Before 2026-09-22 there is **no `quote_id` on an order** (after it, see
[the quote-id link](#quote-ids-link-an-order-to-its-competition-exactly--from-2026-09-29)).
`order_quotes` and its staging model carried amounts, gas and solver but no id;
`public.quotes` does have `id` and is a stale 5,522-row snapshot from 2026-02-04. So the
match is quote-redesign's heuristic: same tokens and kind,
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
| `price_suspect` | the order failed a [price check](#two-price-checks-both-at-the-orders-own-auctions) — its two feeds for the sell token, or its sold and bought values, disagree past `PRICE_IMBALANCE_THRESHOLD`: out of every figure, weight zeroed |
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

### Two price checks, both at the order's own auctions

The same wrong price would set an order's weight (`executed_sell × sell_token_price`) and
its gas netting, so an order whose prices cannot be trusted is taken out of every figure,
numerator and denominator alike. Two checks, 2x either way like D14, each blind to what the
other catches:

- **One-sided** (`price_suspect_orders`): the api's `sell_token_price` against the
  auction's price for the same token — in the auctions the order traded in, weighted by
  what it sold there (`traded_sell_prices`), and the window's median only for an order
  that never traded and so weighs nothing. Catches a broken api price; passes a token
  both feeds price wrong by the same factor.
- **Two-sided** (`imbalanced_orders`): what the order sold against what it bought, each
  trade valued at the prices of the auction it settled in — `counterfactual.price_imbalanced`
  per order. Catches the same-factor case.

**Neither catches a pair whose two tokens are both mispriced by the same factor**, in both
feeds — the solve side's check has the same hole. It is not hypothetical: in bnb's
2026-09-22..29 window, order `5d8534d8…` (Trust Wallet, 2026-09-23 12:38) sold
`0x223a20e1…`, priced at 1.07e8 wei *per atom* by the api and the auctions alike, for an
equally mispriced `0x6ced5c6d…`; it alone is 1.34e12 BNB, 97.9% of the volume both checks
pass, and with a second such order (2.9e10 BNB) it reduces every bnb `window_bps` of that
week to zero. That week's bnb quote rows are not to be read. Closing the hole needs a
reference neither feed provides — a per-order value cap, or a native-price ceiling — and
belongs on both sides at once; it is open. Until then, look at the window row's executed
volume before quoting a bps figure: an absurd denominator is visible there.

Each trade's auction is the first settlement after it in its block (the rule
`int_backend_data__trade_with_tx_hash` applies), read directly from staging; the native
token is priced as its wrapped ERC20, as the solve side prices it.

Both are per auction because a window median is wrong for a token that moved. On bnb
2026-10-05..12, 0xfb4f81f5… roughly tripled: a median two-sided check flagged 225
USDT→0xfb4f81f5 orders whose per-auction values match the trade-implied rate at 1.00x, and
the median one-sided check (M15's) excluded 238 orders whose api price matched the
per-auction one at ~1.00x — 63 BNB of honest volume. Measured on that week:

| bnb 2026-10-05..12, orders with a recorded quote: 28,377 | orders excluded | executed volume left, BNB at the api's price |
| --- | --- | --- |
| nothing excluded | 0 | 295,050,220,754 |
| M15: one-sided, window median | 325 | 28,696,669 |
| hand-cleaned: M15 + two-sided at window medians | 1,700 | 17,492 |
| **M16: one-sided per auction** | **11** | |
| **M16: two-sided per auction** | **1,106** | |
| **M16: either** | **1,111** | **20,283** |

The ~2,800 BNB between the hand-cleaned figure and M16's is the median checks'
over-exclusion — volatile tokens' honest trades, the 0xfb4f81f5… orders among them. The
largest order M16 keeps is 3,744 BNB of USDT→BNB, 18% of the week.

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

## Precision: the gold ladder is not integer-exact, the raw one is

Amounts in `gold_quotes` are `Float64`. An 18-decimal amount above ~0.01 tokens is already
past `2^53`, so **nothing derived from a gold ladder is exact to the atom** — see
`ch.as_float`, and note that ClickHouse's JSON also returns large `Float64` values as
integer-looking numbers, which coerce silently to `int` and then compare unequal to their
own float. The raw layer stores amounts as decimal strings, and the quote-id link reads
only the raw layer, so its ladders hold exact ints (`quotes._exact`) and its corroboration
is an integer comparison. The ranking itself still runs in floats, as prod's does.

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
9. **A request with no ladder is not a matching failure.** Before services#4957
   (2026-09-23) the streaming endpoint recorded requests without estimates or a winner,
   and a stream the client drops early still leaves no winner; count those before
   reading a coverage or reproduction drop as a rule change.
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
13. **Never filter heuristic candidates on the request's `quote_id`.** The competitions the
    gold layer never received have no `quote_computed` either, so a null id says nothing
    about whether the competition priced an order. (The per-estimate
    `raw_quotes.price_estimates.quote_id` is a different column and, from 2026-09-29, the
    exact link.) `price_quality != 'fast'` is the one filter that belongs:
    `order_quotes` never holds a fast quote, and a fast request sharing the amount is the
    UI's poll, not the order's quote.
14. **A one-sided price guard passes a token both feeds price wrong.** M15's guard
    compared the api's `sell_token_price` with the auctions' median for the *same* sell
    token. bnb 2026-10-05..12: 26 orders, all valued above 1,000 BNB and most from Trust
    Wallet (token `0x4078920b…` at 0.1006 BNB in both feeds), carried 28.68 M of the
    window's 28.70 M BNB of "executed volume" — the solve side's whole week was 16,988
    BNB — and pushed every solver's `window_bps` toward zero (Rizzolver −0.01 against
    −11.40 cleaned by hand; Nexroute −62.6 against −3.80). The two-sided check (sold
    against bought value, per auction) is what catches them; a format-13 report's bnb
    quote rows are not to be read. The one-sided check still earns its place: the same
    window's largest order, `49e8f493…` at 2.95e11 BNB, has a broken *api* price
    (7.7e9 wei per atom against the auctions' 1.5e-3) and is caught by it.
15. **Ties decide real winners, and arrival order is recorded.** Prod clamps an underwater
    sell quote to zero and truncates every effective amount to an integer before
    comparing, then keeps the first answer among equals. Dust requests and small out
    amounts tie routinely; reproduce them with `nats_seq`, not by guessing. A fall in the
    reproduction rate concentrated on one estimator answering first is this, not a rule
    change.
16. **An order's quote id can name a non-winning estimate.** Since services#4957 a streamed
    competition is recorded with its final best as the winner, but the client may have
    placed the order from an earlier answer it was sent (308 bnb Trust Wallet orders in a
    week), or dropped the stream before any winner was recorded (72 arbitrum `minimal-2`
    orders). The id link is right and the competition is right; the winner is not who
    priced the order. Decide such an order by the answer it took, against what the stream
    had sent by the commit — `quote_computed` says what that was — never by the winner.
17. **`order_quotes.solver = 0x0` is an unwrap, not a competition.** Every one seen is a
    wrapped-native→native sell that all estimators answer identically; under the
    heuristic each counted as a quote win of whoever answered first and taught the
    estimator map the zero address.
18. **An estimate with no `request_id` can be the order's quote.** The backend re-quotes an
    ethflow order when it indexes it, internally — no request id, no gold row, and the
    order's `quote_id` still naming the UI quote. Look for request-less estimates of the
    order's key reproducing its stored amount before calling an ethflow order unlinkable.
19. **A quote id is an answer, not a competition.** Since services#4925 each estimator's
    answer has its own id; the competition is the `request_id`. Two orders from one
    request can carry different ids, and the winner's id is on an order only if the order
    took the winner.
20. **A positive quote delta on a streamed order can be real.** A client that commits to
    the stream's first answer is hurt by a solver that answers first and badly; removing
    that solver improves its quote. Check the stream order before calling it a bug.
