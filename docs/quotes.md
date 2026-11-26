# The quote competition

How `analyse --quotes` reads the quote competition: where the data is, how prod picks a
winner, how an order is linked to its competition, and what the quote side computes.
`loo/quotes.py` holds one way of doing each thing; `loo` replays only windows from
2026-10-28 on.

## Where the data is

The quote competition is in **ClickHouse**, not the analytics DB. `loo/ch.py` talks plain
HTTP with `CLICKHOUSE_URL` / `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` from `.env`, and
every query sets `max_execution_time`. `loo` reads **`raw_quotes` only**:

| table | what it holds |
| --- | --- |
| `raw_quotes.quote_requested` | one row per request: tokens, kind, `in_amount`, caller, `price_quality`, token symbols |
| `raw_quotes.price_estimates` | one row per estimator answer: `out_amount`, `gas`, verified flag, `nats_seq` (arrival order), and since 2026-09-29 the answer's own `quote_id` and the estimator's settlement `solver` |
| `raw_quotes.winning_price_estimates` | the recorded winner per request, same id and solver |
| `raw_quotes.quote_computed` | one row per quote persisted — for a streamed request, one per answer the stream sent |

Raw amounts are exact decimal strings. The raw tables are sorted `(chain_id, estimator,
sell_token, buy_token, nats_seq)` and partitioned by month. `gold_quotes.fct_quote_requests`
/ `fct_quote_estimates` exist (`Float64` amounts, request-level `quote_id`) and are not read.

Prod requests begin 2026-07-22 15:28 on mainnet and bnb and 2026-09-01 16:00 on base,
arbitrum-one, xdai, polygon, avalanche, ink, plasma and linea; lens and sepolia have none.
`ch.NETWORK_CHAIN_IDS` is the support test, and each chain in it was measured before it
was enabled. `analyse --quotes` refuses a network missing from it, because empty quote
columns would read as "this solver quoted nothing" rather than "no data".

### What is excluded

- **`fast` requests**: they carry no quote id, are served by an estimator built
  `with_early_return` (truncated ladders), and `order_quotes` never holds one — the quote
  an order is placed from is always a non-fast competition. Trust Wallet's UI polls
  `fast`, but its orders carry the winner of a non-fast competition.
- **The `_live` views**: they splice the last ≤6 hours from raw tables, so two runs over
  one window can disagree.

The `optimal` path order-creating requests use is one stage with no early return
(`price-estimation/src/factory.rs`): every estimator is asked, so a removal does not
change the rest of the ladder, which is what makes the counterfactual well-defined. On
2026-08-25 mainnet saw 1.18 M `optimal` requests against ~1.6 k order-creating ones a day,
a ~0.13% conversion rate.

### Query shapes that decide the cost

- Every query carries a day-bounded `event_timestamp` filter so partitions prune.
  **`request_id in (…)` is not a key prefix**: unbounded, it scans.
- Tables are `ReplacingMergeTree`s, so a row can appear twice until a background merge.
  Requests are deduplicated by `request_id`, ladders by `(request_id, estimator)`;
  without that a duplicated winner reads as ambiguous. Harmless weeks later, wrong today.
- `quote_id is not null` is not selective (1.1 M of 1.5 M mainnet requests a day): it
  marks "a quote was computed", not "an order was created".

### The gold tables do not hold every competition — link through the raw layer

The gold tables are built off the `quote_requested` event, and a competition whose request
event never arrived is in **neither**. On mainnet 2026-07-25 that was 8,625 of 1,537,915
competitions (0.6%), but they were the competitions behind **a quarter of the day's
orders**: API callers (Safe and bot addresses, `defibot`, `label`, no app code) whose
ladders are complete in `raw_quotes.price_estimates`. "Not in `fct_quote_requests`" does
not mean unquoted. `loo` therefore reads the raw layer only.

## How the winner is picked

`services/crates/price-estimation/src/competition/quote.rs`, the rule merged on
**2026-09-02**; prod's `optimal` estimator is built with
`PriceRanking::BestBangForBuck`:

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

What the ladder records, and what it does not (verified against the Rust on 2026-10-14):

- **Ties break by arrival order, and `nats_seq` records it.** The sort is stable over
  answers in completion order. Ties are common: every underwater sell estimate is worth 0,
  and truncation makes near-equal small amounts equal (195 ties on bnb 2026-10-05..12,
  mostly trivial unwraps and dust). `rank_key` clamps and truncates as prod does, ladders
  load in arrival order, and `pick` keeps the first of equal keys. Neither step changes a
  counterfactual: an underwater replacement lands in `nonpositive` either way, and a tie
  is between estimates prod itself could not tell apart.
- **Errored estimators leave no trace** (`emit_quote_event` fires only on `Ok`), so "the
  removed solver was the only usable estimator" and "the others errored" look the same.
- **Zero-gas and zero-amount estimates are in the ladder but never win**: the event fires
  before `is_reasonable`, so `Estimate.reasonable` applies it again.
- **Prod compares `f64`.** `rank_key` runs the exact integers through the same float
  arithmetic.
- **The verified class is a hard first key** in deployment (the code default is
  `Unverified`): over 2026-08-10..16 a verified estimate won all 5,041 requests that had
  both classes.
- **The gross ladder is not the competition**: the highest `out_amount` won only 51.6% of
  mainnet `optimal` requests on 2026-08-25. Gold's `price_rank` / `behind_winner_bps` are
  gross — diagnostics, never deltas.

`loo.quotes.RANKING`:

- **`effective-sell`** (default) nets gas in the sell token at the order's own recorded
  `gas_price / sell_token_price` — prod's rule since 2026-09-02, from nothing but
  `order_quotes`, and the same quantity the price-improvement fee is charged against, so
  the pick and the fee cannot disagree.
- **`autopilot`** is the out-token rule from before 2026-09-02 (`out × native(out) ∓ gas ×
  gas_price`); it needs the buy token's native price (the window's per-token median of
  auction prices) and serves as the cross-check. The two agree to first order and differ
  only through the quote's own pricing error.

Every run reports its pick reproduction rate. Over 2026-10-05..12 `effective-sell`
reproduced 25,722 of 25,723 on mainnet (the one miss is real: `fractal` nets better at the
recorded gas price, `baseline` won), and 100% on all nine other chains. A drop means the
rule changed or a tie-break is being missed.

## Quote ids link an order to its competition exactly — from 2026-09-29

Two backend changes make the link a join:

- **The order records its quote id**: `order_quotes.quote_id` is the id the order was
  placed with, for API and ethflow orders alike; complete in
  `raw_prod_<chain>.order_quotes` from 2026-09-22. **The staging copy
  (`dbt.stg_backend_data__order_quotes.quote_id`) is NULL on rows built before
  ~2026-10-06** (incremental model, column appended), which is before any window `loo`
  replays; since then it equals the raw mirror (0 of 584k orders created 2026-10-28..11-26
  differ on mainnet, base, arbitrum-one and bnb), so `loo` reads staging and spares a
  first run a cold lookup into the raw table per order.
- **Every estimate carries its own id and solver** (one id is drawn per
  estimator before quoting and published with the estimator's settlement address).
  Ids appear on every chain at the same deploy, **2026-09-29 17:16 UTC**.

**A quote id names one solver's answer, not a competition.** The competition is the
`request_id`; `order_quotes` holds the id of the answer the order used. So the link is
`order_quotes.quote_id` → the `price_estimates` row with that id → its `request_id` →
every estimate of the request. Each estimator has exactly one settlement address per
chain, so `solver` maps estimators to addresses directly. Only trivial ETH/WETH estimates
lack an id (0.18%).

`link_by_quote_id` resolves each creation day's ids to requests within that day's window
(creation −30 min / +90 s), and keeps a link only when the linked estimate **is** the
recorded winner **and** reproduces the order's stored amount **to the atom**. A query
covers several days, each a *unit* with its own window and ids: a column `units` lists
the units whose condition a row meets, and the rows of a unit are exactly what a query for
that day alone returns (a row near midnight can belong to two). One query per day read
every ClickHouse part its day falls in, and a part holds about a week. An order
that fails gets a second pass ([the re-quote](#an-ethflow-orders-stored-quote-can-come-from-the-backends-own-re-quote))
or the [stream rule](#streamed-quotes-the-order-takes-what-the-stream-showed-not-the-winner);
reasons for the rest are reported.

**`order_quotes.solver = 0x0` is a trivial unwrap** (wrapped native → native, every
estimator answering identically): no competition, so it is linked by nothing, stays in
the population and contributes zero like any unmoved order.

Over 2026-10-05..12 (re-run 2026-10-14), every non-trivial order on every chain was
linked:

| chain | orders | unwraps | one-shot | via a stream (of them before it finished) | re-quote |
| --- | --- | --- | --- | --- | --- |
| mainnet | 26,061 | 180 | 21,457 | 4,406 (174) | 18 |
| bnb | 28,386 | 284 | 15,836 | 12,265 (308) | 1 |
| base | 17,115 | 47 | 16,550 | 513 (30) | 5 |
| arbitrum-one | 12,140 | 43 | 11,851 | 246 (93) | — |
| xdai | 9,303 | 2 | 9,301 | — | — |
| polygon | 2,408 | 5 | 2,044 | 358 (17) | 1 |
| avalanche | 798 | 1 | 762 | 35 (0) | — |
| ink | 523 | — | 523 | — | — |
| plasma | 122 | 3 | 119 | — | — |
| linea | 24 | — | 24 | — | — |

Small chains say the rule holds, not much more (linea: 24 orders, two estimators). Order
counts grow for a day or so after a window ends. The heuristic amount-and-time match the
id link replaced put 4.8–5.7% of even its corroborated matches on the wrong competition;
do not reintroduce it.

### Streamed quotes: the order takes what the stream showed, not the winner

`/api/v1/quote/stream` runs the same competition but forwards each answer that beats the
best sent so far. Since 2026-09-23 each forwarded answer is persisted as a
`quote_computed` row and the final best is recorded as the winner; the user commits to
whatever was on screen, and the order carries that answer's id. On bnb 2026-10-05..12,
44% of orders came through a stream (nearly all Trust Wallet); 308 took an answer before
the final one, and 72 arbitrum `minimal-2` orders came from streams the client dropped
before any winner was recorded.

`replay_stream` re-runs the stream over the arrival-ordered ladder and must reproduce its
`quote_computed` sequence. It does on every multi-row stream of bnb, base, polygon,
arbitrum-one and avalanche, and 4,343 of 4,344 on mainnet, once one rule is mirrored: a
first answer whose gas exceeds the sell amount still sets the bar but is dropped by the
quoter (`SellAmountDoesNotCoverFee`) and never sent.

An order is streamed when its request has two or more `quote_computed` rows, its answer is
not the recorded winner, or its stream has no winner. Its counterfactual
(`StreamTake`, `stream_replacement`):

- **Removing anyone but the estimator whose answer it took leaves it unaffected**: every
  answer not sent before the commit did not beat the one on screen.
- **Removing that estimator** gives the best remaining answer that had arrived by the
  commit, on the same verified-first key. The commit is unrecorded, so it is placed at
  the **latest moment the record allows**: just before the next answer the stream sent,
  or the order's creation. If nothing else had arrived, the user waits for the next answer
  the stream would send; in a dropped stream that answer is unrecorded — the `unrecorded`
  leaf, counted and out of every figure.
- **`commit_dependent`** counts wins whose replacement would differ had the user
  committed the moment their answer arrived. It says how often later answers could have
  mattered, not an uncertainty in the figure: on bnb, orders that took the final answer
  were created 2.2 s (p1) to 24 s (p90) after the whole stream had arrived.

**A removal can improve a streamed order's quote.** If the user took a fast, poor first
answer, removing its solver brings forward a better one (base order `676200aa…`,
2026-10-09: baseline's 175.6 M answer taken, horadrim's 241.7 M arrived 19 ms later). A
positive quote delta for a solver that answers first and badly is real, not a sign error.

### An ethflow order's stored quote can come from the backend's own re-quote

An ethflow order is placed on chain with the UI quote's id, or `0`. When that quote cannot
be used, the backend re-quotes while indexing the order and stores the result, while
`order_quotes.quote_id` keeps the UI's id. The re-quote is a full competition with **no
`request_id`** and `caller = 0x0`, its estimates carrying ids from one block drawn
together, plus a request-less winner. `_link_requotes` finds the estimate by the order's
request key and exact stored amount among request-less estimates, requires it to be the
recorded winner, and takes the request-less estimates within 5 s and 64 ids as its
competition. The order links to *that* competition, because the stored quote is the
baseline of every figure and the fee. All 25 such orders of 2026-10-05..12 linked this
way (mostly OneKey).

### The order-side ladders in the analytics DB

cow-dagster syncs ladders into `dbt.stg_quote_data__competition_estimates` (one row per
order × solver, from 2026-09-29), but only for **traded** orders and without estimator
names or trivial estimates. Enough for executed-volume figures; not for `no_quote`, which
needs every created order.

## What the quote side computes

Per linked order and removal scenario (`diff_quote`):

| leaf | meaning |
| --- | --- |
| `unaffected` | the removed solver did not win this quote, so the winner and its class cannot move |
| `no_quote` | it was the only usable estimator: **the order is never created** |
| `class_flip` | it was the only *verified* estimator: the quote survives unverified |
| `degraded` | the quote falls to the runner-up's effective amount |
| `unpriced` | the `autopilot` rule had no native price for the out token |
| `nonpositive` | gas swallows the input on one side: no ratio, no replacement for the fee path |
| `unrecorded` | a dropped stream's next answer was never recorded |
| `price_suspect` | the order failed a [price check](#two-price-checks-both-at-the-orders-own-auctions): out of every figure, weight zeroed |
| `implausible` | the replacement is more than 2x (`IMPLAUSIBLE_QUOTE_FACTOR`, the solve side's threshold) off the recorded quote's rate either way: a broken estimate promoted to winner. Out of the bps and fee figures, counted with its volume |

The guard leaves are **excluded wins**, so they are tested only **after** the removal is
known to have moved the quote; tested first, every suspect order becomes a win of every
solver. `implausible` exists because removal promotes nonsense: one buy order's
replacement cost three atoms and read as +1.16e15 bps.

The figures:

1. **`window_bps`** — Δ effective quote summed in native over the window's whole quoted
   volume; `conditional_bps`, over the volume of moved orders only, sits beside it.
   Weights are **executed** volume (`volume_native`, `window_volume_native`), because
   quoted-never-traded probe orders (a 10 M WETH quote, say) would otherwise be nearly all
   of it. A per-order `delta_bps` is the change in rate, not a cost ratio, which is
   unbounded: a replacement at 30x the cost would carry 30x the order's volume.
2. **`no_quote` / `no_quote_share`** — orders that would not have been created, over every
   order the window created.
3. **`class_flip`** is outside both bps figures with its own count and volume: the
   replacement's nominal amount can rise while the quote is worse in kind (one flip once
   supplied a whole conditional figure).

Quote bps are heavy-tailed, so `compare` prints the median moved order and the largest
order's share beside each figure, and marks a share of 30% or more with ⚠.

### Two price checks, both at the order's own auctions

A wrong price sets both an order's weight and its gas netting, so an order whose prices
cannot be trusted leaves every figure. Two checks, 2x either way, each blind to what the
other catches:

- **One-sided** (`price_suspect_orders`): the api's `sell_token_price` against the
  auction's price of the same token, weighted over the auctions the order traded in
  (`traded_sell_prices`); the window median only for an untraded order, which weighs
  nothing. Catches a broken api price (bnb order `49e8f493…`, 2.95e11 BNB).
- **Two-sided** (`imbalanced_orders`): sold against bought value, trade by trade, at the
  prices of the auction each trade settled in — `price_imbalanced` per order, but exact.
  Catches a token both feeds price wrong by the same factor: on bnb 2026-10-05..12, 26
  such orders carried 28.68 M of 28.70 M BNB of executed volume and pushed every
  `window_bps` to zero.

**A side worth under one wei is not unpriced.** The two-sided check compares the exact
1e18-scaled products `price × amount`; floored to whole wei, as `price_in_eth` does, a
sub-wei side reads as zero, and a zero side is skipped as if unpriced. bnb order
`c0ae1e94…` (Trust Wallet, 2026-10-30) passed both checks that way: it sold 1.22e28 atoms
of `044f7ac6…` for 64 USDT atoms. Both feeds priced the token at 1.01e-4 BNB per atom,
so the one-sided check saw agreement, and the 64 USDT atoms are worth 0.09 wei, so the
floored two-sided check saw nothing. Its 1,236,312 BNB were 94% of the executed volume of
bnb 2026-10-29..11-26 and made every bnb `window_bps` about 17 times too small. Valued
exactly, it is 1.3e25x imbalanced; on bnb 2026-10-30 it was the only order the change
added, and none dropped out. Only a side with no price or a zero amount is left
unjudged, so an untraded order and an order of an unpriced token still pass. A trade too
small for its atoms to express its rate within 2x is now judged and can fail; it weighs
nothing either way. The solve side's `counterfactual.price_suspects` still floors and
passed the same order (auction 26814584)
([analytics-db](analytics-db.md#native-prices-can-be-plain-wrong)).

Each trade's auction is the first settlement after it in its block (the rule of
`int_backend_data__trade_with_tx_hash`); the native token is priced as its wrapped ERC20.
Both are per auction because a window median fails honest trades of any token that moved:
on that bnb week median checks excluded 225 + 238 orders whose per-auction ratios were
1.00x. Per auction, the two checks excluded 1,111 of 28,377 orders, leaving 20,283 BNB.

**Neither catches a pair whose two tokens are both mispriced by the same factor**, in
both feeds; the solve side has the same hole. bnb order `5d8534d8…` (2026-09-23) alone was
1.34e12 BNB that way. Closing it needs a reference neither feed provides (a per-order
value cap or a native-price ceiling), on both sides at once; it is open. Look at the
window's executed volume before quoting a bps figure: an absurd denominator shows there.

## The fee: recharged separately, never added to the fee delta

The price-improvement fee is charged against the effective amount of the best quote, which
`fees._adjust_quote_to_order_limits` already computes; a different quote is the same
function with a different `Quote`. `analyse_auction` values the counterfactual executions
twice:

- with the **recorded quotes on both sides** — that is `delta_protocol_fees`, what the
  removal does through who executes;
- with the **replacement quotes** — the difference on the recharged orders is
  `quote_fee_recharge`, the quote's own fee effect, **reported apart and never added**.
  A worse quote lowers the fee baseline, so the same executions pay *more*; folded in, it
  showed the protocol gaining from losing its best quoters.

`quote_substituted_orders` counts the distinct executed orders with a price-improvement
policy the recharge came from.

### In every auction that executed the order, whether or not the solver bid there

The quote winner often does not bid on its own order (`tsolver` was absent from the solve
side on 52% of the orders it quoted over one August week). So `analyse_auction` also
arbitrates an auction the removed solver never bid in whenever one of its orders carries a
replacement quote, routed past the solver-absent early return; it enters only the fee sums
and quote counters, and stays in `changed` so `compare` converts it to USD
(`auctions_quote_only`).

### What is held fixed, what is recomputed, and why the user effect is not a surplus

Everything the solvers *did* — bids, executions, ranking — stays as recorded; everything
the mechanism *computes from the quote* — the fee — is recomputed. The user-price effect of
a replaced quote is the shown-price change above, never a surplus recomputed against the
limit the user would have signed: a worse quote weakens the limit, so a recomputed surplus
would book a user *gain* exactly when the user was shown a worse price, and count the same
movement twice with opposite signs. Scores are never recomputed from a counterfactual
quote; the quote moves what is charged, never who won.

Holding executions and ranking fixed is **checked per auction**:

- A worse quote weakens the limit, so a recorded execution clears the counterfactual limit
  by construction — except on a class flip. `quote_limit_unclearable_orders` counts
  recharged orders that would not clear the limit the replacement implies (the user's
  slippage wedge applied to the replacement's rate — the one model in the check).
- A weaker limit raises every competing solution's measured surplus by nearly the same
  amount. `auctions_quote_shift_would_change_winners` re-arbitrates with each score
  shifted by that amount and counts winner-set changes over `auctions_quote_checked`
  (ids in `quote_shift_auctions`). On July 2026 mainnet: 122 of Tsolver's 4,841 auctions
  and 7 elsewhere, all close races. A large count would be a finding about the design,
  not a correction.

A replacement whose fee exceeds its own sell amount is recorded as an unknown fee; the
auction keeps the recorded quote for that order alone and is valued again.

## Precision

The recorded quote, its fee and every solve-side number come from Postgres and stay exact;
ClickHouse supplies **only** the counterfactual ladder. Raw ladder amounts are exact
integers (`quotes._exact`), so the link is an integer comparison; the ranking still runs
in floats, as prod's does. Counterfactual quote amounts are **rounded, not floored** —
flooring would bias every quote down and every fee delta up. The recorded quote's gas
(`order_quotes.gas_amount`) and the ladder's are on the same convention: ratio exactly 1
for 78.3% and 1.0000 at every quantile p10–p99 (2026-07-25).

## Out of scope, and named as such

- **Quote rewards.** The counterfactual quote's eligibility is not evaluated. Since
  2026-10-12 (CIP-88) they are a budget share of protocol revenue split
  by `int_quote_data__quote_reward_shares`; nothing here is checked against those models.
  The quote rewards recorded as paid to the removed solver are reported by `analyse`
  ([rewards](rewards.md#rewards-paid-to-a-solver-recorded)).
- **Conversion.** No counterfactual exists for an order nobody created, and at a 0.13%
  conversion rate an elasticity proxy would be indefensible.
- **The limit price and fillability.** A better quote makes an order harder to fill;
  measuring that needs the solve counterfactual re-run with perturbed limits.

## Traps

1. **A quote id is an answer, not a competition.** The competition is the `request_id`.
2. **An order's quote id can name a non-winning estimate** (streamed quotes). Decide such
   an order by the answer it took, against what the stream had sent by the commit.
3. **`order_quotes.solver = 0x0` is an unwrap**, not a competition.
4. **An estimate with no `request_id` can be the order's quote** (the ethflow re-quote).
   Look for request-less estimates reproducing the stored amount before calling an
   ethflow order unlinkable.
5. **`stg_backend_data__order_quotes.quote_id` is NULL before ~2026-10-06.** Before that,
   read `raw_prod_<chain>.order_quotes`.
6. **"Nothing in `fct_quote_requests`" does not mean unquoted**; the gold tables drop
   competitions without a request event.
7. **`request_id in (…)` scans unless bounded by day.** **The `_live` views are
   non-deterministic.** **Duplicate rows are normal on fresh data.**
8. **Ties decide real winners.** Reproduce them with prod's clamp, truncation and
   `nats_seq`, not by guessing. A reproduction drop concentrated on one estimator
   answering first is this, not a rule change.
9. **Removing the only verified estimator can *improve* the nominal quote.** A positive
   bps figure: look for a class flip, or a streamed order that took a poor first answer,
   before looking for a bug.
10. **A guard leaf is an excluded win.** Test whether the removed solver won the quote
    *before* testing a price guard.
11. **The quote winner need not bid on its order.** Any fee effect wired through
    `analyse_auction` must get past the solver-absent early return, or it is dropped
    exactly where quoting and solving solvers differ.
12. **A one-sided price guard passes a token both feeds price wrong**, and both guards
    pass a pair mispriced by one factor. Check the executed-volume denominator.
13. **A request with no ladder is not a link failure**: a stream dropped early leaves no
    winner. Count those before reading a coverage drop as a rule change.
14. **A per-request median gap is meaningless across estimators.** Quote figures are
    volume-weighted over linked orders.
15. **Never floor a value a guard compares.** A side worth under one wei floors to zero,
    and a zero side looks unpriced. Compare exact products.
