# Winner selection — algorithm reference

## Source of truth

`~/Work/Code/services`; the algorithm is the standalone `winner-selection` crate, which the
autopilot wraps. The local checkout lags: compare `git -C ~/Work/Code/services log -1`
with `git ls-remote origin main` before trusting it.

| What | Where (function names; line numbers drift) |
| --- | --- |
| whole mechanism | `crates/winner-selection/src/arbitrator.rs` `arbitrate` |
| fairness filter | `partition_unfair_solutions` |
| baselines | `compute_baseline_scores` |
| per-token-pair scoring | `score_by_token_pair` |
| per-order score (CIP-38) | `compute_order_score` |
| protocol fees | `protocol_fees` (moved to `crates/protocol-fees`, formula for formula) |
| surplus | `surplus_over_limit_price`, `surplus_over` |
| clearing prices | `calculate_custom_prices_from_executed` |
| winner picking | `pick_winners` |
| reference scores | `compute_reference_scores` |
| `price_in_eth`, `canonical_token` | `crates/winner-selection/src/primitives.rs`, `crates/chain-types` |
| autopilot wrapper | `crates/autopilot/src/domain/competition/winner_selection.rs` |
| config defaults | `crates/configs/src/autopilot/run_loop.rs` |

Parameters: `max_winners = 20`, `max_solutions_per_solver = 3`. Line numbers quoted in
`loo/` docstrings are from the August 2026 revision; the file was reorganised since,
so search by name.

### `loo` replays the current mechanism only

`loo` implements the rules live since **2026-10-28** (`extract.MECHANISM_START`) and
refuses an earlier window (exit 4); it is run at most a month back, so older rules are not
kept beside the current ones. Two changes separate today's rules from older ones:

| Since | Rule now |
| --- | --- |
| v2.376.0, Aug 2026 | a solution is kept iff it trades at least one auction order; a **zero score no longer drops it** |
| v2.385.0, live 2026-10-27 | **JIT orders claim no token pair** in `pick_winners`; from then on two winners can share a directed pair (2–10 mainnet auctions a day) |

`loo validate`, measured 2026-11-23: winner sets, filtered-out sets and reference scores
match the record in every auction, with no valuation failure.

| Window | Auctions compared | Fast-path excluded | Solutions |
| --- | --- | --- | --- |
| mainnet 2026-11-18 | 4,850 of 4,850 | 23 | 88,532 |
| mainnet 2026-10-29 | 4,264 of 4,264 | 0 | 100,225 |
| bnb 2026-11-18 | 5,891 of 5,891 | 19 | 65,256 |
| base 2026-11-18 | 2,746 of 2,746 | 1 | 22,875 |
| arbitrum-one 2026-11-18 | 1,300 of 1,300 | 8 | — |

## The algorithm

Given all bids for one auction:

1. **Value each solution per directed token pair**: each order that
   `contributes_to_score` adds its score to `value[(sell_token, buy_token)]`; the total
   is the sum over pairs.
2. Drop solutions with **no** contributing order (a total of 0 is not enough: an order
   filled exactly at its limit scores 0 and still trades). Sort descending by total.
3. **Baselines**: `baseline[pair]` = the highest total among solutions touching *exactly
   one* pair.
4. **Fairness filter**: keep a solution if it touches one pair, **or** if
   `value[pair] >= baseline[pair]` on every pair it touches; otherwise `filtered_out`.
   (The one-pair exemption keeps reference scores from collapsing to 0.)
5. **Pick winners**: walk the kept solutions in order; a solution wins if the directed
   pairs of its contributing orders are disjoint from every pair already claimed. Stop at
   `max_winners`.
6. **Reference score** of winner `s` = total of the winners step 5 picks on the ranked
   set with **all of `s`'s solutions** removed.

`contributes_to_score` (`auction.rs`): the order is in the auction, or it is a JIT order
whose owner is in `surplus_capturing_jit_order_owners`. In the DB, auction membership is
`competition_auctions.order_uids` — not the `fee_policies` table, whose
[rule](analytics-db.md#fee-policies-cover-every-order-of-every-kept-solution) differs.

**In `loo` an order contributes iff it is in the auction, and JIT orders are not read.**
`surplus_capturing_jit_order_owners` was empty in every auction on all ten chains over
2026-10-24..11-23. A JIT order then earns no score, is ignored by the filter and claims
no pair, so `extract.EXECUTIONS_SQL` returns only auction orders. This is exact only
while that array stays empty; if a surplus-capturing owner returns, it is wrong.

### One asymmetry to preserve

Step 1 keys pairs on the **raw** tokens; step 5 normalises through `canonical_token`,
mapping `0xee…ee` to WETH (and Solana's native SOL, `00…00`, to wSOL). ETH→USDC and WETH→USDC are scored under separate pairs but conflict
when picking winners. Mirror it, do not tidy it.

### Fast-path auctions are excluded

Since 2026-11-08 (mainnet auction 14201960) some `competition_auctions` rows carry a
`fast_path_quote_id`: one order settled straight from its quote competition, the winning
quote stored as a solution at exactly the limit, the losing quotes as zero-score,
`filtered_out` solutions **below** the limit, and `penalty_caps_native` written as 0. The
mart pays no batch reward for them. There is no solver competition to re-run and valuing
the losing quotes fails on negative surplus, so `extract.load_auctions` never yields one
and reports `fast_path_auctions`. About 0.5% of auctions: on 2026-11-18, 23 of 4,873 on
mainnet, 19 of 5,910 on bnb, 1 of 2,747 on base. Anything iterating `competition_auctions`
directly must skip them itself. A solver's role as quoter of such an order belongs to the
quote side.

### `ranked` order is load-bearing

`arbitrate` ends with `sort_by_key(|s| (Reverse(s.is_winner()), Reverse(s.score())))`, so
`ranking.ranked` is **winners by descending score, then non-winners** — not score order.
Scores `[100, 90, 80]` where 90 conflicts with 100 rank `[100, 80, 90]`.
`compute_reference_scores` re-runs the order-dependent `pick_winners` on *that* list, so
re-sorting by score first gives different reference scores whenever a non-winner
conflicts with a lower-scoring winner. The DB records the order in
[`proposed_solutions.uid`](analytics-db.md#proposed_solutionsuid-encodes-the-ranking).

### `max_solutions_per_solver` is applied before `arbitrate`

The autopilot drops each **driver name**'s fourth solution onward while collecting bids
(`autopilot/src/run_loop.rs`), so the recorded set is already truncated: removing a
solver cannot surface a rival's fourth solution. The cap keys on driver name, everything
else on submission address, so a solver running two drivers gets two allowances.

### Reference scores do not re-filter

`compute_reference_scores` re-runs only step 5 on the already-filtered set. A true
leave-one-out re-runs steps 3–6, because removing the solver that set a high baseline can
*un-filter* solutions (re-picking on the kept set alone missed all 14 un-filter wins of
the August calibration window). `loo` keeps both: the cheap variant reproduces
`stg_backend_data__reference_scores`; `counterfactual.leave_one_out` is the full one.

## The filter runs on score

Scores are recorded per solution, so the per-pair split step 4 compares is stored
nowhere. `valuation.pair_score_split` recomputes it — score is user surplus plus protocol
fees, fee policies are recorded for every order of every kept solution, and `loo/fees.py`
reproduces `protocol_fees` to the wei — and **anchors it on the recorded score**:

1. **One pair** — the whole score sits on it; exact from the record alone.
2. **Priced pairs** — `surplus + fee`; where every pair is priced the sum must return the
   recorded score to the wei, or nothing from that solution is trusted.
3. **One unpriced pair** — the residual `score − Σ(priced)` is exact.

A failed check discards the solution's whole split (`PairScores.anchor_failed`), which
`validate` counts separately: it means the fee path is wrong *here*, not that fee
coverage is thin.

**One currency, surplus as a lower bound.** Baselines are never missing — a single-pair
solution puts its whole recorded total on its pair, which is what the Rust reads too — so
only the left side of a comparison can be dark. Fees are non-negative, so a dark pair's
score is at least its surplus: `surplus >= baseline` **proves** the pair fair, and
`surplus < baseline` is the only guess, which filters. A dark pair can make the filter
stricter than the protocol, never looser. `Ranking.proxied_uids` records exactly the
solutions decided by a guess.

**A dark pair must not blind the pairs beside it.** `is_fair` is a conjunction of
independent per-pair comparisons, so a pair needs an exact score only where it has a
baseline it cannot clear on surplus; a dark pair without a baseline decides nothing.
Requiring the whole split before using score kept, in auction 13492893, a solution the
protocol dropped, which then took the auction.

Measured on mainnet under the August 2026 rules: guesses decided 3 of 5,211 multi-pair
solutions over 2026-08-01..04 and 31 of 38,404 over July, with 0 filter decisions
differing from the record; the old surplus-on-both-sides filter got 7 of 5,211 wrong, all
false keeps.

### Counting the guesses that could have mattered

`filter_guess_could_matter` restores **every** guessed verdict at once (a guess is always
a filter, so its opposite is "kept") and re-picks the winners. **`False` is a proof, not
a sample**: if restoring all of them leaves the winners alone, each restored solution is
blocked by a solution that wins in every scenario, so no subset of guesses can move the
winners either. `analyse` reports it as `auctions_filter_guess_could_matter` — the only
handle on the filter's uncertainty in a counterfactual, where no record exists to compare
with. It matters most on the removal side, where a lowered baseline un-filters solutions
(`auctions_filter_relaxed`) and a dark pair failing the lowered baseline might have been
un-filtered too. July 2026, all 26 solvers removed in turn: 76 auctions where a guess
could have moved the winners, against 1,507 genuinely relaxed. Reference scores are not
covered: a guess could move a reward without moving a winner.

The check is structural on purpose. That the fee needed to flip a guess would be absurd
(July's sensitive guess fell short of its baseline 253,000x) is a plausibility judgement
that holds only until some chain's fee policies make it false.

### What `validate` checks, and how it names a difference

The two `observed` lines (`pick on observed kept set`, `reference scores (observed)`)
re-run the pick and the reference scores on the DB's **own** kept set. With the filter
held at the record, a pick bug cannot hide behind a filter difference in the same auction,
and both lines must always be exact.

For a filter difference, `filter_bracket` decides what *any* valid per-pair split would
force, using the recorded score `S`, per-pair surplus `u_i` and score baselines
`b_i` (`u_i <= v_i`, `Σ v_i = S`): a keeping split exists iff
`Σ max(b_i, u_i) <= S` (else `must_filter`); every split keeps iff `u_i >= b_i` on every
pair (`must_keep`); otherwise `undetermined`. `filter_cause` then names each difference:

- `proxy` — the verdict was a guess on an `undetermined` solution;
- `model` — a guess, and keeping a `must_filter` solution is a provable consequence of
  comparing surplus;
- `bug` — anything else: a difference where every pair was decided by a proof, a
  `must_keep` solution dropped, or a DB decision contradicting a decisive bracket. These
  are impossible on either scale because surplus baselines are at most score baselines;
  the run exits 2.

A `SCORE ANCHOR FAILURES` line means the recomputed fees contradict a recorded score: the
fee path is wrong.

## Protocol fees: the port in `loo/fees.py`

`fees.py` ports `protocol_fees` (surplus, volume and price-improvement policies, applied in
reverse application order) and reproduced `surplus + fees` against
`proposed_solutions.score` wei-exactly on 2,512 of 2,512 solutions (auctions
13509000–13509867), and fee atoms plus the partner split against
`int_backend_data__fees_per_trade` on 9,340 of 9,340 (13480599–13500000).

- **`mul_f64` rounds.** `U256::checked_mul_f64` converts `factor * 1e18` to an integer by
  rounding. Truncating reproduced only 99.1% of scores (23 of 2,512 off), and an exact
  rational was wrong too (auction 13509109: −480 and −184 wei against the record): the
  protocol's `f64` detour is part of the fee's definition. `factor / (1 ± factor)` stays
  `f64` as well.
- **A quote's fee** (`gas_amount × gas_price / sell_token_price`) is evaluated over exact
  rationals and floored, as the Rust's `BigRational` does; a float divide rounds the wrong
  way often enough to shift the fee.
- **Fees are converted to native as differences of conversions**
  (`to_native(surplus + fee) − to_native(surplus)`), so `surplus_native + fee_native`
  equals `compute_order_score` to the wei. Converting the fee directly loses up to one
  surplus-token atom per order to the floor inside `to_native` (~5e8 wei on a six-decimal
  token), a systematic gap against the recorded score.

## The counterfactual

`counterfactual.leave_one_out` removes every address of the removed solver (or group) and
re-runs steps 2–6. Rules that are not obvious from the code:

- **Auctions the solver never bid in are skipped but stay in the denominator**, so rates
  describe the window, not the solver's own subset. Window denominators (executed volume,
  order executions) are read from the **record**, not from our re-arbitrated baseline,
  because they must exist for the auctions the counterfactual skips.
- **An auction is retained when a reference score moves**, not only when the winner set
  does — rewards move on a reference score alone.
- **A joint removal is one scenario.** Single removals do not sum to it (removed solvers
  stop replacing each other), and nothing is attributed to members.
- **Settlement attaches to the slot** ([why](analytics-db.md#failures-are-concentrated-in-the-biggest-solutions)).
- **Price-suspect auctions are excluded from every statistic.** The 2x threshold separates
  healthy trades (both-token valuations within 1.04x) from broken prices (14,000x apart);
  it is not a noise filter. Excluded auctions carried 82% of Sector's apparent Δsurplus
  over 2026-08-01..04 ([details](analytics-db.md#native-prices-can-be-plain-wrong)).

### A blocked batch keeps orders unexecuted

`pick_winners` takes a solution only when **every** pair it touches is unclaimed, so a
single-order winner can block a whole multi-order batch, and the batch's other orders go
unfilled even when nobody else bid on them. Auction 13488369: Sector's one-order solution
won the pair, blocking Quasi's and Helixbox's two-order batches, so order `4a9b4669` went
unfilled; without Sector, Quasi's batch fills both. So orders executed **only without the
solver** (`only_without_solver`) are a real coverage effect, not a defect.

### An unexecuted order is lost for its auction only

The counterfactual is per auction. An order only the removed solver filled counts as not
executed with its whole surplus lost; nothing models a fill one auction later, because in
the record the order left the book. The lost surplus is measured against the **limit**, so
for a market order it is roughly the user's slippage tolerance. Whether a later fill was
plausible can be bounded: did another solver ever bid on the order in the window, and did
others win the same pair? (Both queries need the
[auction-id bound](analytics-db.md#bound-joins-against-proposed_solutions-to-the-windows-auction-id-range).)

Under `inherited`, a solver's own failed settlement can be credited to it: on polygon in
October 2026 Gnosis_0x won order `9efd830a…` in auction 40811267 and reverted, then filled
it alone in 40811270. Without Gnosis_0x, Wraxyn wins the first auction and inherits the
revert, and nobody bids in the second — 66% of Gnosis_0x's polygon Δsurplus. Check a
removed solver's largest coverage auction for a failed win of the same order just before.

### Runner-up: the margin a losing bid leaves the winners

A winner `s` that settles earns `winning_score − min(winning_score, reference(s))` before
caps ([rewards](rewards.md)): its **margin** over the reference score of step 6. A solver
whose losing bid sits in another winner's without-`s` pick sets that reference; it is
that winner's **runner-up**. Over the auctions the removed solver bid in, `analyse`
reports:

- `auctions_runner_up`: auctions where a removed solver's **non-winning** solution is in
  the reference pick of a winner outside the removal. It is decided by solution uid
  (`ReferenceScore.setter_uids`), because a solver can win one pair and be runner-up on
  another.
- `delta_reference_margin`: Σ margin without minus Σ margin with, over the **pressed
  winners**. A pressed winner is a solver outside the removal that wins the **same
  solutions** on both sides, and each side uses its own winning and reference scores.
  Settlement plays no part, so the figure is the same under both outcome rules. A
  replacement, or a winner that takes other solutions once the removed solver is gone,
  holds part of a vacated slot. That belongs to Δrewards, not to room left.
- `reference_margin_rises` / `_falls` (auction-winner pairs),
  `auctions_reference_margin_moved`, and `reference_margin_volume`, the received-leg
  volume of the moved winners' winning solutions. `compare` divides the delta by that
  volume; in score mode these are **score** bps, never a price change.

A margin moves only when the winning score or a reference score moves, so every such
auction is in `changed`, and `load_report` checks the total against those rows.

**Margin, not reference.** Sector wins X at 100 and Quasi wins Y at 80, so Quasi's
reference holds Sector's 100. Without Sector, Horadrim takes X at 60. The winning score
and Quasi's reference both fall by 40, so Quasi's margin stays 50
(`tests/sim/pinned.py` `runner_up_elsewhere`). A Δreference sum would credit Sector
with pressing Quasi; the margin does not. If Sector also bids Y at 70, it loses Y to
Quasi but fills Quasi's reference pick. It is then runner-up on Y while it wins X, and
Quasi's margin rises from 10 to 50. Kipseli's removal on mainnet 2026-11-18 moved a
pressed winner's reference without moving its margin in 537 auction-winner pairs.

**A margin can fall.** Without the removed solver, the reference pick can do better than
it did with it:

- **un-filtering**: the removed solver's single-pair bid set a baseline that filtered a
  rival batch, and once kept the batch raises the reference (`margin_falls`, 40 → 25);
- **a vacated claim**: the removed solver's winning batch held a pair that kept another
  batch out of the reference pick, and its replacement claims less (35 → 10);
- **the greedy pick is not monotone**: removing a losing batch can let two smaller bids
  fill more. The fairness filter makes this rare.

Falls are counted apart from rises and summed into the delta, not dropped.

**What it does not claim.** Every bid is held fixed. In theory, a winner bidding against
its reference could shade its bid by its margin and still win, so a strong runner-up
keeps the winners' bids up. `loo` does not model that. The row measures room left with
bids fixed, and is neither a price change nor a bound on one.

Measured 2026-11-25 on mainnet 2026-11-18, first 3,000 auctions (2,951 analysed):

| Removed | Bid in | Runner-up in | Δmargin | Winners moved (rose / fell) | In auctions | Over volume |
| --- | --- | --- | --- | --- | --- | --- |
| Kipseli | 2,301 | 743 | +0.3853 ETH | 786 (786 / 0) | 726 | +0.53 bps of 7,307.55 ETH |
| Baseline | 1,620 | 17 | +0.0022 ETH | 17 (17 / 0) | 17 | +1.51 bps of 14.35 ETH |

In both runs, every auction where a margin moved was a runner-up auction. In 158 of
Kipseli's 726, Kipseli also won another pair. Requiring the same winning solutions on
both sides excluded 62 of the 1,687 Kipseli auction-winner pairs that won on both sides.

Auction 14271972: Rizzolver wins at 0.034049 ETH. Its reference pick holds Cicada's
winning 0.000036 ETH and Kipseli's losing solution (uid 2) at 0.029870 ETH, for a reference
of 0.029907 ETH, equal to the recorded one. No other bid fills Rizzolver's pairs, so
without Kipseli the reference is Cicada's 0.000036 ETH. Rizzolver's margin rises from
0.004179 to 0.034049 ETH: +0.029870 ETH, all of Kipseli's bid.

### Above the cap

The cap decides who a margin matters to. A settled winner is paid
`clamp(margin, lower cap, upper cap)`:

- **Below its upper cap**, a bid lower by `x` lowers the reward by `x`. A worse bid buys
  the winner nothing, so the reference only sets what it is paid, and performance plus
  consistency payouts sum to the upper caps whoever wins ([rewards](rewards.md)): a
  runner-up's absence moves reward between solvers out of a fixed pool.
- **Above its upper cap**, the reward is the cap whatever the winner bids above
  `reference + cap`. That much score it could withhold from users at no cost in reward.

`delta_margin_above_cap` is Σ max(0, margin − upper cap) without minus with, over the
pressed winners whose wins all settled (`observed_score == competition_score`). A batch
that did not land delivered nothing to withhold; its recorded upper cap is 0, so counting
it would put its whole margin above the cap. The cap is the pressed winner's own recorded
one, the same on both sides because it wins the same solutions. Score mode only; an
auction with a pressed winner whose cap is unknown is counted in
`auctions_margin_cap_unknown` and left out.

`delta_pressed_rewards_capped` is the behaviour-free counterpart: Σ capped reward of the
pressed winners without minus with. Their caps do not move, so the consistency budget
moves by exactly the opposite, and the figure is the part of the weekly pool the removed
solver's bids shifted from winners' performance rewards to the consistency budget. It
sits beside consistency rewards for that reason.

The above-cap figure is room with bids fixed, not a scenario. Most pressed winners are
above their cap already with the runner-up present and still bid there: they bid
against what rivals might offer, not against a reference they cannot see. What a strong,
frequent runner-up's absence would do to those bids is the equilibrium effect this row
points at and `loo` does not model.

Measured 2026-11-25 on mainnet 2026-11-17..24 (38,642 auctions analysed, every cap
known):

| Removed | Δmargin | of it on winners that did not settle | above the cap, settled |
| --- | --- | --- | --- |
| Sector | +93.203 ETH | +6.431 ETH | +82.198 ETH |
| Rizzolver | +49.208 ETH | +34.433 ETH | +9.991 ETH |
| Kipseli | +33.130 ETH | +9.863 ETH | +18.236 ETH |

51.554 ETH of Sector's is auction 14289933, where its losing bid set Rizzolver's
reference on a 6,291 ETH order and the whole rise lies above Rizzolver's cap.

### The margin of settled winners

`delta_settled_margin` is the pressed winners' margin over their reference,
counterfactual − actual, uncapped, over the winners whose wins all settled. It is what
consistency rewards are set against.

**Why the margin and not the rewards.** The margin is the rent a runner-up's bid takes
from the winner. Without the runner-up the winner gets it back, and the mechanism only
decides who pays it:

- below the upper cap, the protocol pays it as performance reward, out of the pool that
  would otherwise pay consistency (`delta_pressed_rewards_capped`);
- above the cap, users pay it when the winner bids worse at no cost in reward
  (`delta_margin_above_cap`);
- in a mechanism with no performance rewards and only a consistency budget, users pay
  all of it, and Δrewards would mean nothing.

The amount does not depend on how rewards are paid, so it is a mechanism-agnostic
measure to set a mechanism-specific payment against. Consistency paid ÷ settled Δmargin
reads as the share of that rent the winners would have to take, absent the solver, for
its consistency rewards to be earned.

**Why settled winners only.** A batch that did not land delivered nothing, so there is
no rent to take under any mechanism. Plain `delta_reference_margin` also counts those
winners. Their penalty change is left out too: penalties belong to this mechanism.

**How it fits the capped figures.** A settled winner's uncapped reward is its margin, so
its capped reward is min(margin, upper cap) and its part above the cap is
max(0, margin − upper cap). The two split its margin at the cap, so

```
Δpressed rewards (capped) + Δmargin above cap = settled Δmargin + Δcapped reward of the
                                                pressed winners that did not settle
```

except in an excluded auction, where every capped reward is 0 and only the part above
the cap is left (`tests/sim/runner_up.py` `check_value`). The settled Δmargin covers the
same auctions as the two capped figures: an auction with a pressed winner whose cap is
unknown is left out of all three and counted. Score mode only, since surplus mode has no
reward rows to tell settlement by.

**What it still assumes.** Bids are fixed, as made under today's mechanism, and winners
and references come from today's winner selection: it is agnostic about payments, not
about allocation. Taking the whole margin assumes a winner that knows the second-best
bid, so it is the most rent the runner-up protects, not a user loss. It is heavy-tailed,
hence the largest-auction share beside it.

**It does not add up to the consistency pool.** The pool is every winner's unused cap
plus penalties; a margin counts only what one solver's bids hold back from the winners
that keep their solutions, and single removals do not add up to a joint one. Measured
2026-11-26 on the all-solver reports for 2026-11-03..17 (inherited, every cap known,
`out/consistency-margin-2026-11-03/`), summed over every solver:

| Network | settled Δmargin | all pressed winners' Δmargin | Δpressed rewards (capped) | Σ consistency paid | paid ÷ settled |
| --- | --- | --- | --- | --- | --- |
| mainnet | +185.982 ETH | +359.542 ETH | +30.606 ETH | 90.181 ETH | 0.48 |
| base | +11.768 ETH | +12.991 ETH | +1.315 ETH | 3.454 ETH | 0.29 |
| arbitrum-one | +9.888 ETH | +14.077 ETH | +0.854 ETH | 1.746 ETH | 0.18 |
| bnb | +14.121 BNB | +16.531 BNB | +1.441 BNB | 3.492 BNB | 0.25 |

On mainnet, winners that did not settle carry about half of the plain Δmargin
(`delta_reference_margin`), so leaving them out is first-order there; 20–28% of
mainnet winners have not landed in time since its deadline went to one block on
2026-11-03. The identity above left a penalty term of 2.552,
0.047, 0.012 and 0.019 in the same order. Every other field of these reports equals the
format-18 run of 2026-11-25 exactly, and recorded payouts to within the wei rounding of
the per-auction split.

`Δscore` beside it is Δsurplus + Δprotocol and partner fees, the fee leg over the
auctions whose fees are all priced. With bids fixed it is the solver's own marginal
contribution, the quantity the performance reward pays before caps.

## Score vs. surplus

The autopilot ranks on the CIP-38 score, `user surplus + protocol fees` in the surplus
token (buy token for sell orders, sell token for buy orders), converted to native
(`compute_order_score`):

```
sell order:  score_native = surplus * price[buy_token] / 1e18
buy  order:  surplus_in_buy = surplus * buy_amount / sell_amount   # order limit amounts
             score_native   = surplus_in_buy * price[buy_token] / 1e18
```

`surplus_over` scales limits by the executed amount for partial fills, with **ceiling**
division on the sell side and floor on the buy side — the expected source of off-by-one
differences.

`loo` ranks on the **recorded** `proposed_solutions.score` (`--mode score`, the default);
only the per-pair split is recomputed, and only for the filter. `--mode surplus` ranks on
user surplus and answers "how much user value did X add" self-consistently; it is not an
approximation of the competition, gets no score split, and its rewards would be wrong.

### Measured divergence — surplus is not a stand-in for score

Over 17,640 single-order sell bids (auctions 13505000–13509867), score / surplus_native is
1.01 at p10, **1.08** at the median and **2.70** at p90, and **3.2%** of bids are
fee-dominated (surplus under 1% of score). Auction 13509006 solution 9 fills at the limit
plus one atom and scores 2.2e13 wei, essentially all fee. Ranking by surplus and by score
are not interchangeable, and a mechanism that ranks on score buys fee capture as much as
user surplus.
