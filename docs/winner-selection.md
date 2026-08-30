# Winner selection — algorithm reference

Reference context. The concrete work is in [../PLAN.md](../PLAN.md).

## Source of truth

Cloned locally at `~/Work/Code/services`. The algorithm lives in a standalone crate; the
autopilot only wraps it.

| What | Where |
| --- | --- |
| whole mechanism | `crates/winner-selection/src/arbitrator.rs:38` (`arbitrate`) |
| fairness filter | `arbitrator.rs:61` (`partition_unfair_solutions`) |
| baselines | `arbitrator.rs:650` (`compute_baseline_scores`) |
| per-token-pair scoring | `arbitrator.rs:176` (`score_by_token_pair`) |
| per-order score (CIP-38) | `arbitrator.rs:208` (`compute_order_score`) |
| protocol fees | `arbitrator.rs:260` (`protocol_fees`) |
| surplus | `arbitrator.rs:325` (`surplus_over_limit_price`), `:337` (`surplus_over`) |
| clearing prices | `arbitrator.rs:505` (`calculate_custom_prices_from_executed`) |
| winner picking | `arbitrator.rs:570` (`pick_winners`) |
| reference scores | `arbitrator.rs:607` (`compute_reference_scores`) |
| `price_in_eth`, `as_erc20` | `crates/winner-selection/src/primitives.rs:20`, `:9` |
| autopilot wrapper | `crates/autopilot/src/domain/competition/winner_selection.rs` |
| config defaults | `crates/configs/src/autopilot/run_loop.rs:10` |

Parameters: `max_winners = 20`, `max_solutions_per_solver = 3`.

## The algorithm

Given all bids for one auction:

1. **Value each solution per directed token pair.** For each order that
   `contributes_to_score`, add its value to `value[(sell_token, buy_token)]`. Solution
   total = sum over pairs.
2. Drop solutions with total 0. Sort descending by total.
3. **Baselines.** For each directed pair, `baseline[pair]` = the highest total among
   solutions that touch *exactly one* pair.
4. **Fairness filter.** Keep a solution if it touches exactly one pair, **or** if
   `value[pair] >= baseline[pair]` for every pair it touches. Otherwise it is
   `filtered_out`. (The one-pair exemption is deliberate — it avoids reference scores
   collapsing to 0; see `arbitrator.rs:88`.)
5. **Pick winners.** Walk the kept solutions in descending order; a solution wins if its
   directed token pairs are disjoint from every pair already claimed by a winner. Stop at
   `max_winners`.
6. **Reference score** of winner `s` = total of the winners produced by step 5 re-run on
   the ranked set with **all of `s`'s solutions** removed.

`contributes_to_score` (`crates/winner-selection/src/auction.rs:45`): the order is in the
auction, **or** it is a JIT order whose owner is in
`surplus_capturing_jit_order_owners`. In the autopilot the first test is "has an entry in
the fee-policy map", which contains every auction order. In the DB the analogue is
`competition_auctions.order_uids` — *not* the `fee_policies` table, whose own membership
rule is a different one ([storage
rule](analytics-db.md#fee-policies-cover-every-order-of-every-kept-solution)).

### Two asymmetries to preserve

Step 1 keys pairs on the **raw** `sell_token`/`buy_token`. Step 5 normalises them through
`as_erc20`, mapping the native-token sentinel `0xee…ee` to WETH. So a solution trading
ETH→USDC and one trading WETH→USDC are scored under separate pairs but conflict when
picking winners.

Step 1 also skips orders failing `contributes_to_score`, while step 5 claims pairs from
**every** order in the solution (`arbitrator.rs:587` iterates `solution.orders()` with no
filter). A solution can therefore be single-pair for scoring — and so exempt from the
fairness filter — while blocking two pairs when winners are picked.

Both are in the Rust and should be mirrored, not tidied up.

### `ranked` order is load-bearing

`arbitrate` returns after
`sort_by_key(|s| (Reverse(s.is_winner()), Reverse(s.score())))` (`arbitrator.rs:51`), so
`ranking.ranked` is **winners by descending score, then non-winners by descending score**
— not plain score order. A non-winner can outscore a winner that sorts ahead of it:
scores `[100, 90, 80]` where the 90 conflicts with the 100 and the 80 does not ranks as
`[100, 80, 90]`.

`compute_reference_scores` then re-runs `pick_winners` on *that* list
(`arbitrator.rs:624`), and `pick_winners` is order-dependent. Re-sorting by score before
computing reference scores gives different numbers whenever a non-winner conflicts with a
lower-scoring winner. The ordering must be carried through, not recovered.

Conveniently, the DB records it: `proposed_solutions.uid` is assigned in exactly this
order — see
[analytics-db.md](analytics-db.md#proposed_solutionsuid-encodes-the-ranking).

### Reproduction status

Reproduced against three days of mainnet (2026-08-01 to 2026-08-04, 7,745 auctions,
98,669 solutions), score mode, taking `proposed_solutions.score` as each solution's total:

| check | result |
| --- | --- |
| step 5 re-picked on the recorded kept set vs. `is_winner` | **7745 / 7745** |
| step 6 re-run on the recorded ranking vs. `reference_scores` | **7745 / 7745** |
| per-order surplus vs. the dbt model | 93941 / 94565, all 624 differences exactly −1 (the model's rounding, [above](analytics-db.md#order_surplus_atoms_in_surplus_token-rounds)) |
| solutions we could not value at all | **0** |
| end-to-end winner set vs. `is_winner` | **7745 / 7745** |
| end-to-end `filtered_out` | **7745 / 7745** |

The first two hold the DB's own filter decisions fixed and re-run only the step under
test, so no approximation is anywhere in their path — every quantity is either a recorded
score or a token pair read off a trade.

The last two are the whole mechanism run end to end, and since M14 they are exact as well:
step 4 is the one step whose inputs the DB does not record, and recomputing the per-pair
scores closed the last of it. **The competition reproduces with no observed
approximation anywhere.**

### The same, over a full month

The three days above are the calibration window every milestone has used. July 2026 is the
independent check, an order of magnitude larger and covering the whole month rather than a
hand-picked stretch (2026-07-01..08-01, 85,487 auctions, 720 excluded for missing order
data (0.84%, D17), **84,767 analysed, 946,995 solutions**):

| check | result |
| --- | --- |
| winner set vs. `is_winner` | **84767 / 84767** |
| `filtered_out` vs. the record | **84767 / 84767** |
| reference scores, ours and observed | **84767 / 84767** each |
| step 5 re-picked on the recorded kept set | **84767 / 84767** |
| solutions we could not value at all | **0** |
| filter decisions differing from the DB | **0 / 38,404** multi-pair solutions |
| solutions whose recomputed fees contradict the recorded score | **0** of 946,995 |

The last row is the one that was not previously testable. `pair_score_split` refuses a
split whose parts do not add up against `proposed_solutions.score`, and over a month of
data that refusal never fires — so the fee port of M13 holds at a scale 12x its calibration
window, not merely on the window it was tuned against.

Verdicts resting on a guess: **31 of 38,404 (0.081%)**, in 13 auctions where the guess
could have moved the winners. Both rates are a shade higher than the three-day window's
and the same order, so the calibration window was representative rather than lucky.

### The filter runs on score

Scores are stored per solution, so the per-pair split step 4 compares is not recorded
anywhere. It is however **recomputable**, and since M14 it is recomputed rather than
proxied. Score is user surplus plus protocol fees; `stg_backend_data__fee_policies`
records the policies of every auction order of every non-filtered-out solution ([storage
rule](analytics-db.md#fee-policies-cover-every-order-of-every-kept-solution)); and
`loo/fees.py` reproduces `protocol_fees` to the wei. `valuation.pair_score_split` puts the
two together, and **anchors the result on the recorded score** so the split is never a
reconstruction floating free of the record:

1. **One pair in the solution** — the whole score sits on it. Exact from the record alone,
   no fee needed.
2. **A priced pair** — `surplus + fee`, both the protocol's own per-order quantities.
   Where *every* pair is priced the sum must return the recorded score to the wei, or
   nothing from that solution is trusted.
3. **The last unpriced pair** — with exactly one pair dark, the residual
   `score − Σ(priced pairs)` is its score exactly.

Case 2 covers every solution the arbitrator kept, by construction — their orders' policies
are all recorded. Every branch is checked against the record, and a check that fails
discards the whole solution's split rather than trusting part of it: `PairScores.anchor_failed`
says the fee path is wrong *here*, which `validate` counts separately so that a broken fee
port on some new chain cannot be read as that chain having thin fee coverage.

Baselines do not come through this function at all — see below.

#### One currency, and surplus as a lower bound

The filter compares **one** quantity against **one** set of baselines. It is tempting to
think a pair whose score is unrecorded needs a second scale to be compared in — an earlier
cut of M14 built exactly that, two baseline maps and a per-solution choice between them —
but it does not, for two reasons.

**Baselines are never missing.** Only solutions touching exactly one pair set a baseline
(step 3), and such a solution puts its whole value on that pair, so the baseline is simply
its recorded `total` — no decomposition, no fee, nothing to recover. That is what the Rust
does too (`compute_baseline_scores` reads the solution's score). So the right-hand side of
every comparison is exact by construction; only the left can be dark.

**Surplus is not another currency, it is a lower bound.** Protocol fees are non-negative,
so a dark pair's score is at least its user surplus. Substituting the surplus therefore
gives a comparison that can still *prove* fairness and can never fabricate it:

| dark pair | verdict |
| --- | --- |
| `surplus >= baseline` | **proved fair** — the score is at least the surplus, so it clears too. No fee needed. |
| `surplus < baseline` | **unknown** — the missing fee genuinely decides. The filter drops it. |

The asymmetry is the whole design: a dark pair can only ever make the filter *stricter*
than the protocol was, never looser. It cannot keep a solution the protocol dropped for a
reason we cannot see — which is exactly the failure mode the pre-M14 filter had, where all
7 of its differences were false keeps.

That also makes "how approximate is this run" answerable precisely. A comparison is a guess
only when an inexact value fails its baseline; an inexact value that clears it is a proof,
and so is a pair with no baseline at all. `Ranking.proxied_uids` records exactly those
solutions, and nothing else.

#### One dark pair must not blind the pairs beside it

The other half of the same point: `is_fair` is a conjunction of **independent** per-pair
comparisons, so a pair needs an exact score only when it has a baseline it cannot clear on
surplus alone. Knowing three pairs out of four is worth exactly as much as knowing four
when the fourth has no baseline.

The first cut of M14 required the *whole* solution's split before using score at all, and
auction 13492893 is what that cost:

| pair | surplus | fee | score | baseline |
| --- | --- | --- | --- | --- |
| `40e3d1→dac17f` | 156,869,920,027,575 | 189,496,907,610,271 | **346,366,827,637,846** | **388,866,676,327,956** |
| `a0b869→c02aaa` | 49,666,314,933,687 | unrecorded | — | 0 |
| `c02aaa→7f39c5` | 559,190,890 | unrecorded | — | 0 |

The pair that decides the solution is priced and fails its baseline outright; the two dark
pairs have no baseline and cannot affect the verdict however they are valued. An
all-or-nothing rule throws that away, falls back on all three, and keeps a solution the
protocol dropped — which then outscores the recorded winner and takes the auction.

Note also the shape of that first row: the baseline-setter is a *fee-dominated* solution,
surplus 5.3e8 against a score of 3.9e14. The bar the batch has to clear is **4x higher** on
the score scale than a surplus-derived one would be. That is the bias this whole change
removes, in one auction.

#### What it measures out at

Over the three-day window the fallback decides **3 of 5,211 multi-pair solutions
(0.058%)** — every other verdict is a proof, either from a recorded score or from a surplus
that clears its baseline on its own. Measured against the recorded filter:

| filter | decisions differing from the DB | auctions with a different winner set |
| --- | --- | --- |
| **per-pair score, surplus as a lower bound (current)** | **0 / 5211** | **0** |
| per-pair surplus on both sides (until M14) | 7 / 5211 (0.13%) | 5 |
| `scaled` | 196 / 5211 (3.8%) | 76 |
| `raw` | 200 / 5211 (3.8%) | 74 |

The last two are earlier attempts, built and measured in M1 before being removed, and
worth recording because they look like what M14 does and are not. `scaled` split a batch's
score across its pairs in proportion to surplus and left single-pair baselines at full
score; `raw` put surplus on the left and full-score baselines on the right. Both are 30×
further from the recorded filter than the plain surplus comparison they replaced, because
both compare a batch against a baseline inflated by *that baseline solution's* own
protocol fees — a systematic bias no split of the batch's score can correct for. Surplus on
both sides cancelled the bias; recomputing the fees removes it at its source, which is why
it beats both outright rather than landing between them.

#### Counting the guesses that could have mattered

A guess is only a problem where it decides something. Most sit on solutions that lose
either way, so the raw count of guesses overstates the uncertainty — sometimes by all of
it.

`filter_guess_could_matter` (`winner_selection.py`) measures the part that is left. Every
guessed verdict is a *filter* — `proxied` means a pair failed its baseline on an inexact
value, and a failed pair is what filters a solution — so the opposite outcome is always
"kept". The check restores **every** guess at once and re-picks the winners.

**`False` is a proof, not a sample.** If restoring all of them leaves the winner set alone
then none of them won, which means each was blocked by a solution that wins in every
scenario; restoring only some leaves those blockers in place, so no subset can move the
winners either.

This is the number the **counterfactual** needs, and the only one it has. `validate` can
always check its answer against the record; a leave-one-out run cannot, because the auction
it is describing never happened. It matters most on the removal side: taking out the solver
whose solution set a baseline lowers that baseline, which is what un-filters solutions
(`un_filtered`) — and a dark pair that still fails the *lowered* baseline is a solution the
real mechanism might have un-filtered while we keep it out. `auctions_filter_relaxed`
counts the un-filterings found; this counts the auctions where one could have been missed.
`analyse` checks both sides and reports
`auctions_filter_guess_could_matter`; `validate` reports the same over the recorded
arbitration.

Over the three-day window there are **3** guessed verdicts, and in **1** auction the guess
could have moved the winners — auction 13493777, solution 3. Over the whole of July:
**31** guesses, **13** sensitive auctions. That is the honest size of the filter's residual
uncertainty on mainnet.

The counterfactual number is the one to weigh, and it comes from removing each of July's
26 solvers in turn (763,067 solver-auctions, 539,972 multi-pair solutions judged): **217
guessed verdicts, 76 auctions where one could have moved the winners** — against **1,507
auctions where removing a solver genuinely un-filtered a solution**. So the uncertainty
runs at roughly 5% of the effect it perturbs. It also tracks solver size and disappears
entirely for the small ones (Gnosis_BalancerSOR, Rosato, AlgoLabs, Flowdesk, Odos: zero),
which is what it should do — a guess needs a dark pair *and* a baseline it cannot clear.

All three guesses agree with the recorded competition, which filtered all three solutions
too, and none is a close call: the failing pair's surplus falls short of its baseline by

| auction | surplus | baseline | short by | could move winners |
| --- | --- | --- | --- | --- |
| 13493420 | 7.64e13 | 4.65e14 | 6x | no |
| **13493777** | 9.72e8 | 2.46e14 | **253,000x** | **yes** |
| 13496284 | 5.34e8 | 4.89e16 | 91,000,000x | no |

A fee large enough to close a 253,000x gap on a single pair is not a realistic quantity —
the median score/surplus ratio is 1.08 and p90 is
2.70 ([measured](#measured-divergence--surplus-is-not-a-stand-in-for-score)) — so in
practice all three verdicts are safe. The check does not know that: it asks only whether
the *pick* would change if the flip happened, which is a structural question, not a
plausibility one. Keeping the two separate is deliberate. A structural "no" is a proof and
survives any future window; "the fee would have to be absurd" is a judgement that holds
until some chain's fee policies make it false.

Reference scores are deliberately **not** covered: a guess could move a reward without
moving a winner, and the check does not look for that.

#### Bounding what the missing split can cost

The bracket below is what `validate` uses to name a *fallback* difference. There are none
in the calibration window any more, and it is deliberately kept anyway: it applies the same
lower-bound argument the filter does, but **jointly** across a solution's pairs rather than
one at a time, so it can decide cases the per-pair test cannot and remains a valid statement
about the recorded competition either way. Where every pair was decided by a proof the
bracket is strictly weaker than what is known, so `filter_cause` checks that first — a
difference with no fallback in it is a `bug`, with nothing left to excuse it.

The unknown split is not unconstrained. For a solution with recorded score `S` and per-pair
user surplus `u_i`, every valid per-pair score split satisfies

```
u_i  <=  v_i,     Σ v_i  =  S
```

since protocol fees are non-negative. The recorded filter kept the solution iff
`v_i >= baseline_i` on every pair, so a split it could have *kept* exists iff the
requirements fit inside the score: `Σ max(baseline_i, u_i) <= S`. Conversely a filtering
split always exists when some pair has `u_i < baseline_i` (set that pair's `v_i = u_i`).
Against the **exact score baselines** — the best recorded score among single-pair
solutions, which is what the protocol itself used — this decides most solutions outright
whatever the fees turn out to be:

| bracket | multi-pair solutions | meaning |
| --- | --- | --- |
| `must_filter` | 4,882 (94%) | no valid split reaches every baseline: unfair under all of them |
| `must_keep` | 66 (1.3%) | every pair's surplus alone clears its baseline: fair under all of them |
| `undetermined` | 263 (5.0%) | genuinely depends on the fee split — both outcomes are reachable |

(An earlier version tested each pair's interval `[u_i, S - Σ_{j≠i} u_j]` separately, which
cannot see two baselines that are individually reachable but do not fit *together*; it
decided only 79% and its `undetermined` band was 21%. The joint feasibility test is exact:
each verdict now proves what it claims.)

The bracket uses **score** baselines throughout, which is what makes it a statement about
the recorded competition rather than about our model. (In score mode those are also the
baselines the filter itself compares against wherever it runs on the score scale; a
fallback solution meets the surplus baselines instead, and it is exactly for those that the
bracket still has work to do.) A decisive verdict binds the DB absolutely, and binds the
fallback in one direction only:

- Surplus baselines sit at or below score baselines, so keeping a solution the score filter
  drops (`must_filter`) is expected. `validate` reports it as `model`.
- The reverse is impossible. `must_keep` means every pair's surplus already clears its
  *score* baseline, which is at least its surplus baseline, so the surplus filter keeps it
  too. Dropping it anyway means our baselines or valuation are wrong: `bug`.

No difference survives in this window, so no cause is assigned. Worth recording what used
to be here: all seven pre-M14 differences had one shape — the recorded filter dropping a
batched solution the surplus comparison keeps, on a partially fillable order, where surplus
and score diverge most. Two of them (auctions 13488644 and 13490028) the bracket had proven
`must_filter`, so keeping them was a provable consequence of filtering on surplus. The
recomputed split decides all seven the way the protocol did.

### `max_solutions_per_solver` is applied before `arbitrate`

The cap is enforced by the autopilot while collecting bids, not by the arbitrator
(`crates/autopilot/src/run_loop.rs:613-633`): `bids.retain(...)` counts solutions per
**driver name** and drops the fourth onward, then shuffles. `arbitrate` never sees them.

Two consequences for anything counterfactual. The recorded solutions are already truncated
per solver, so removing a solver **cannot** let a rival's fourth solution appear — the
counterfactual is over the set the arbitrator actually saw, and that is the honest scope.
And the cap keys on driver name while everything else keys on submission address, so a
solver running two drivers gets two allowances.

### A blocked batch keeps orders unexecuted

`pick_winners` takes a solution only when **every** pair it touches is unclaimed, so a
single-order winner can block a whole multi-order batch. The orders in the rest of that
batch then go unexecuted even when nobody else bid on them at all. Removing the blocker
lets the batch win and fills all of them.

This is not hypothetical. Auction 13488369:

| uid | solver | score | orders |
| --- | --- | --- | --- |
| 0 | Sector | 1.1543e15 | `091fcb6a` |
| 1 | Quasi | 1.1082e15 | `091fcb6a`, `4a9b4669` |
| 3 | Helixbox | 1.0187e15 | `091fcb6a`, `4a9b4669` |

Solution 0 wins on score and claims the pair, so **both** batches are blocked and order
`4a9b4669` goes unfilled — even though two separate solvers offered to fill it, and the
better of them scored only 4% less. Remove Sector and Quasi's batch wins, filling both
orders.

This is why a leave-one-out run finds orders that execute *only when a solver is removed* —
14 of them over three days for Fractal — and those are a real coverage effect, not a defect.

### A JIT leg can block another solution's execution — and the filter never sees it

The two asymmetries above combine into a mechanism-level quirk. A non-contributing JIT
order is invisible to the fairness filter (step 1 skips it) yet claims its directed pair
in `pick_winners` (step 5 iterates every order). A batch of [user order + the solver's
own JIT counterparty on the reverse direction] therefore passes the filter as a
single-pair solution while claiming **both** directions — and blocks any solution on the
reverse direction outright, without that leg ever having been compared to a baseline.

Measured over 2026-07-12..2026-08-12 on mainnet: 1,010 auctions have a winner claiming a
pair only via a JIT order; in 67 that claim causally changed the pick (verified by
re-running `arbitrate` with JIT-only claims stripped — a kept solution newly wins). 59 of
the 67 are benign: both solutions fill the *same* CoW-AMM order, so the block is
one-fill-per-pair working as intended. In **8**, the blocked solution filled a different
user order, and in all 8 that order was executed by no winner at all — a real coverage
loss to a JIT leg. Six of the eight blockers are one uncatalogued solver (`26a6fdc0…`)
running a JIT-counterparty strategy on USDC↔WETH.

Auction `13472415` is the sharpest case, a full chain: BRRRolver's batch matching user
orders in both WETH/USDC directions was filtered out — its baseline on USDC→WETH was set
by Baseline's single-pair solution (uid 22). Uncatalogued (uid 1) then won WETH→USDC
with a user order plus its own JIT counterparty on USDC→WETH, whose claim blocked uid 22
from winning. User order `8d5b1fc0…` went unfilled with two willing solutions in the
auction — one filtered, one blocked. Flagged to the backend team on 2026-08-13.

### Reference scores do not re-filter

`compute_reference_scores` re-runs only step 5, on the already-filtered set. It does not
recompute baselines. A true leave-one-out re-runs steps 3–5, which can *un-filter*
solutions that were unfair only because the removed solver set a high baseline. Keep both
variants: the cheap one is what reproduces `stg_backend_data__reference_scores`.

## Score vs. surplus

The autopilot's value is the CIP-38 score: `(user surplus + protocol fees)` in the surplus
token, converted to the buy token, then to native.

The **surplus token** is the buy token for sell orders and the sell token for buy orders.
Conversion to native (`compute_order_score`, `arbitrator.rs:208`):

```
sell order:  score_native = surplus * price[buy_token] / 1e18
buy  order:  surplus_in_buy = surplus * buy_amount / sell_amount   # order limit amounts
             score_native   = surplus_in_buy * price[buy_token] / 1e18
```

so buy orders need the order's limit amounts from `stg_backend_data__orders`.

`surplus_over` (`arbitrator.rs:337`) scales limit amounts by the executed amount to support
partial fills and uses **ceiling division** on the sell side. This is the expected source
of small discrepancies when reproducing results.

### Measured divergence — surplus is not a stand-in for score

`int_backend_data__proposed_solution_data.order_surplus_atoms_in_surplus_token` is **raw
user surplus, excluding protocol fees**. Verified over 17,640 single-order sell-side bids
(auctions 13505000–13509867), comparing `bid_score` against
`surplus * price[buy_token] / 1e18`:

| | ratio score / surplus_native |
| --- | --- |
| p10 | 1.01 |
| median | 1.08 |
| p90 | 2.70 |

and **3.2% of bids are fee-dominated** (surplus below 1% of score). Concrete case,
auction 13509006 solution 9: the order is filled at exactly its limit price plus one atom
— surplus of 1 atom — yet it scores 2.2e13, essentially all protocol fee.

Consequence: ranking by surplus and ranking by score are **not** interchangeable. A
surplus-only run is a self-consistent analysis of *user-facing value*, but it is not an
approximation of the real competition, and reward numbers derived from it would be wrong.
See PLAN.md §2 for how the two are separated.
