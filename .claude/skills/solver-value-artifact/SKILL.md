---
name: solver-value-artifact
description: Write and publish the HTML artifact that reports a leave-one-out run — the value of one or more solvers over a window. Use whenever a run's results are to be written up, published, shared, turned into a page or a report, or when an existing solver-value artifact is to be updated. Covers which figures lead, how they are labelled, and the neutral register the pages are written in.
---

# Solver-value artifacts

An artifact for this repo reports **what a removal scenario changes** — nothing more. It
is read by people deciding what solvers are worth, so it has to be quotable out of
context and boring to read.

**[reference/template.html](reference/template.html) is the shell to start from** — it
carries the stylesheet, the section order, and the caveats as prose with only the figures
left as placeholders. The two pages already in `artifacts/` predate this skill and differ
from it in layout: read them for the subject matter, not for the format.

## Register

The page states facts in digestible form. It does not build to a reveal.

- **No fluff.** Every sentence carries a number, a definition, or a limit. If a sentence
  survives deleting its adjectives unchanged, the adjectives were fluff.
- **No reveal.** Do not withhold a figure to land it later, do not open with the
  surprising row, do not write a headline that poses a question the table answers.
  Section headings name their contents (`Summary`, `Protocol fee and partner fee`), and
  the page title names the solvers and the window.
- **Banned register:** superlatives and comparatives-as-drama (*dwarfs*, *by far*,
  *remarkably*, *strikingly*, *notably*, *surprisingly*, *the real story*, *turns out*),
  narrative framing (*where the story lives*, *the sturdier claim*), and any phrasing
  that ranks a solver's worth rather than reporting its measured delta.
- **Summaries are welcome.** A summary restates magnitudes, rank and scope. It does not
  interpret motive, does not extrapolate beyond the window, and does not recommend
  anything. "X is the largest of the three on every measure: $A of surplus, $B of fee"
  is a summary; "X is indispensable" is not.
- **No recommendations.** The page never says what the protocol should do.
- **Uncertainty is stated flatly** — counts of excluded auctions, unpriced fees, and
  concentrated rows sit in the table and the caveats, not in hedging adverbs.
- Deltas are **counterfactual − actual**, always, and the sign note says so. Never
  reframe a delta as "the value X creates"; that step belongs to the reader.

## What leads and what is deemphasised

Four measures are the focus, in this order. They get full-ink columns, they carry the
summary, and each gets a sentence of its own definition:

1. **Δsurplus, absolute** — USD headline with the native amount beneath it.
2. **Δsurplus, relative** — the price change in basis points spread over all traded
   volume, with the still-traded conditional reading and the moved-order count beneath.
   Both conditionings are labelled; neither substitutes for the other.
3. **Δprotocol and partner fees** — the gross, with the **protocol** and **partner**
   split beneath it. Never present the gross alone and never call it protocol revenue.
4. **Orders no longer executed** — the count and its share of *every* user-order
   execution the chain traded in the window, plus the surplus those orders carried.

**Two muted columns, not a second table.** Win share (with bid rate beneath) and
largest-auction share are the supporting detail that earns a column, in `class="sec"`
to the right of the focus four. Everything else the run produces — Δrewards capped and
uncapped, the `assume-settled` scenario, medians, sign splits, per-auction distributions
— stays off the table. It is in the reports and the `compare` output, and the page says
so once in the footnote. Never promote something into the focus group because a run made
it interesting, and never add a muted column to avoid deciding.

**The outcome rule is prose, not a column header.** Do not stamp `(inherited)` on the
Δsurplus header or the subtitle. State once in the sign note how settlement is handled —
a replacement inherits the recorded winner's on-chain outcome, so settlement cancels out
of Δsurplus and the figures measure the competition's decision — and put the
`assume-settled` comparison in the settlement caveat, quoting the figures that move most
in both directions.

**Largest-auction share needs its definition carried with it.** It is the concentration
of the **Δsurplus** column and nothing else: the biggest single auction's |Δsurplus| over
the row's signed total, across auctions where surplus moved. Fees, coverage and price
concentrate differently and are not described by it; a row whose deltas offset can exceed
100%. Say in the caveat that it tracks **how few auctions moved, not chain size** — small
chains average higher only because they usually have few moved auctions, and a solver
thin on a busy chain gets the same concentrated row. Frame it as a prompt to open the
auction, not a discount factor: a 100% row is one real auction, not a wrong number.
The fee column gets the same check on its own detail line (`fee_share`: the largest
auction's |Δgross fee| over the row's signed total), unflagged, because a single recharged
order with a price-improvement-kind partner policy can be most of a quoting solver's fee
delta; it never substitutes for the Δsurplus share and is not summed across chains.

## The run comes first

Every figure on the page traces to a report file from the run being published. Do not
carry a number over from an older artifact, from the conversation, or from memory.

```bash
uv run loo analyse --network mainnet \
    --solver Rizzolver --solver Baseline --solver ExtQuasimodo \
    --start 2026-07-01 --end 2026-08-01 \
    --outcome-rule inherited --outcome-rule assume-settled \
    --out "out/mainnet-{solver}-{rule}.json"
```

- **One `analyse` per network**, repeated for every chain each solver bid on — a solver
  with ten chains gets ten rows, including the rows that came back zero. Silently
  dropping a chain overstates concentration. `--all-solvers` derives the list from the
  window instead of naming solvers by hand, which is also one query rather than one per
  solver (D26).
- **Both outcome rules**, always — they share the extraction pass, so only the arbitration
  repeats. Measured on a 26-solver mainnet month: **16.3 min for two rules against 11.9
  for one, 1.38x** — on a fast DB day; the absolute figures move 2x with the DB's
  response time (README, "A month of mainnet…"), the ratio less so. `inherited` carries the page; `assume-settled` is not a column but the
  settlement caveat needs it to say how far the figures move. Do not add a third
  scenario.
- **Solver names match exactly** — `Arc` is not `Arctic`, and one name can span several
  addresses. A comma-separated `--solver "A,B"` is one *joint* removal, not two runs;
  label such a row as the joint scenario and never attribute it to a member.
- `uv run loo compare out/*.json` across every network is the number source for the
  table. Its row labels are the column labels the page should use.
- A full month per chain takes minutes and re-imports `loo/` on each invocation — do not
  edit `loo/` while a run is in flight, and check `REPORT_FORMAT` (currently 13) matches
  the reports; `compare` refuses stale ones.
- **`--quotes` where the chain has quote data** (mainnet, bnb): it removes the solver from
  the quote competition too. It changes the meaning of a column the page already has — see
  "The quote side" below — so a page built from quote-enabled reports and one built without
  are not diffable on the fee column.

## The quote side

`--quotes` adds a second removal: the solver is taken out of the quote competition as well,
and the price-improvement fee of the orders it quoted is recharged against the replacement
quote. Two consequences for the page.

**The fee column does not change meaning.** Δprotocol and partner fees is computed with the
recorded quotes on both sides — what the removal does to fees through who executes. The
quote's own fee effect (a worse quote raises the price-improvement baseline, so unchanged
executions pay more) is **a column of the quote table**, "Δprotocol fee from the quote",
never added to the fee column: folded in, it read as the protocol gaining from losing a
good quoter. Say so where each is defined, and never sum the two on the page.

**The quote side gets its own section, not columns in the focus band.** Three figures, one
row per solver that won a matched quote:

| figure | what it is |
| --- | --- |
| Δquote price | the change in the effective quote, in bps over the **executed** volume of the window's quoted orders — what those orders traded, not what was quoted — with the conditional reading (over the volume that moved) beneath |
| orders that would not have been quoted | the count and its share of every order the quote window created — the coverage analogue |
| Δprotocol fee from the quote | the price-improvement fee the counterfactual executions pay against the replacement quotes minus against the recorded ones — the DAO's part in USD at each auction's rate, with the partners' part, the recharged-order and auction counts (and how many auctions the solver never bid in) and the largest single order's share beneath |

Two checks ride with the fee column and are named in the caveats with their counts:
recharged orders whose execution would not have cleared the limit the replacement quote
implies, and auctions where shifting every score by that limit change would move the
winner set. Both should be near zero; a large one is a finding about the method and the
page says so rather than adjusting by it.

The section states three things about the window before the table, because they bound every
figure in it: the **pick reproduction rate** (a window where it falls is a window where the
protocol's rule changed), the **match coverage** (unmatched orders contribute zero), and
the **quote window**, which is shorter than the run's whenever the quote data begins later.
Class flips and heuristic-tier matches are excluded from the bps figures and their counts
are named — see docs/quotes.md. State how the orders were linked: the gold request table
plus the raw estimates for the competitions it never held, with the per-source match count
the report carries.

## Page structure

Follow [reference/template.html](reference/template.html) section for section: header,
sign note, `Summary`, the solver table, the fee-split section, `Reading each solver`,
`Caveats`, method footnote. The order is fixed so successive months are diffable against
each other.

### Solvers nest, chains filter

**One row per solver, expandable to that solver's chains.** Chains are not a level of the
hierarchy and not a second table; they are a filter over one table.

The asymmetry that fixes this is which sums are scenarios. Removing a solver from mainnet
and removing it from base are **independent counterfactuals**, so adding a solver's chains
together is a real scenario — that solver absent everywhere. Adding *different solvers'*
rows on one chain is not a scenario at all: they are mutually exclusive removals, and the
field cannot lose all of them at once and still have anyone left to replace them. So a
solver total is a number and a chain total is not. Never put a per-chain band carrying
summed solver figures on the page; if a caveat is needed to disclaim a total, the total
should not be there.

- **A chain filter, not a chain axis.** A pill row selects one chain and flattens the
  table to that chain's solvers, **re-sorted by that chain's Δsurplus** — otherwise one
  chain's numbers are read in another's ranking. That is the "which solver matters here"
  view, and it is the same table, not a new one.
- **The single-chain case must degenerate exactly.** With one chain in scope the aggregate
  *is* the chain row: render it flat, with native figures rather than dashes, and no
  disclosure control. A one-chain run and a one-chain filter must both produce precisely
  the table a chain-less page would.

### What aggregates across chains, and what does not

A solver row is not a row of sums; two of its cells cannot exist. Render those as an em
dash with a `title` saying why — a blank cell reads as a missing figure, and a zero would
be a lie.

| quantity | aggregate | why |
| --- | --- | --- |
| USD deltas, order and auction counts | sum | the reason USD leads the format |
| coverage share, win share | ratio of the sums | Σ orders / Σ executions |
| largest-auction share | recompute | biggest single auction's \|Δsurplus\| in USD over the USD total |
| Δprice, both conditionings | recompute | USD-converted moved Δsurplus over USD-converted volume, so each chain's own ratio stays exact and only the cross-chain blend is weighted at median rates |
| **native amounts** | **never** | ETH + xDAI is not a quantity |
| **median moved order** | **never** | a median of medians is not a median |

- **Two rows per entry, not one**, at both levels. A `tr.main` carries one value per column
  and nothing else; a `tr.detail` beneath it carries that value's supporting figures in
  smaller muted print, each on its own line, aligned under the column it qualifies. Only
  the detail row draws the rule, so the pair reads as a single entry, and the first cell
  spans both with `rowspan="2"`. This is what keeps the table narrow: a cell holding
  `−$184,150` sets a ~130px column, while the same cell holding the whole `·`-joined
  sub-line sets a ~300px one.
- **Width lives in three places, and all three need the same treatment.** Values stay
  `nowrap`; detail lines stay `nowrap` (wrapping them halves the width but doubles the
  row height, which is the worse trade); headers must be `white-space: normal` with a
  `max-width`, or a label like "Orders no longer executed, share of all" sets the column
  on its own. Detail cells take `vertical-align: top` so a one-line detail sits under its
  value instead of centring against the tallest cell in the row.
- **Headers name the quantity, never the period.** No `/ month` in a column label, and
  none in the chain pills either — the same page has to render a week, a month or a
  quarter. The window is stated once, in bold, in the subtitle, followed by the sentence
  that every table figure is a **total over that window, not a rate**. In prose say "the
  window" rather than "the month" wherever a figure is being described.
- **No positional selectors on cells.** The first cell spans both rows, so the first
  `td` of a detail row is the Δsurplus detail, not the label — a `td:first-child` rule
  meant for the label silently left-aligns one number column instead. Target cells by
  class (`td.chain`, `td.sec`).
- Measure it rather than eyeballing it: the table should come in under the 64rem column
  with no horizontal scroll, and every cell's computed `text-align` should match its
  role. Rendering the table alone in a scratch page, reading `table.scrollWidth`, and
  sweeping `getComputedStyle(cell).textAlign` catches both faults in one pass.
- Solver rows ordered by Δsurplus magnitude, descending; a solver's chain rows in the
  same order beneath it. The order re-sorts when a chain filter is applied.
- USD on the value row, native amount first on the detail row beneath it.
- The disclosure control is a real `<button>` inside the label cell with `aria-expanded` —
  a `<tr>` is not focusable, and a table nobody can open from the keyboard is a table with
  a hidden half.

**Colour is the accounting convention, not a verdict.** Negative deltas take `--neg`;
positive deltas take plain `--ink`; there is **no green in the palette** and no `--pos`
token to reach for. The reason is the register: a positive delta means the removal would
have improved things — an oddity to notice, not a result to celebrate — and a good/bad
palette states a judgement the page otherwise refuses to make. Red-for-negative is a
numeric convention and survives that objection; green-for-positive does not.

Emphasis on the four focus values therefore comes from **weight, not hue**:
`font-weight: 600` on the value row's focus cells, `400` on the muted ones. A share (like
the coverage column) has no sign and takes no colour, but keeps the weight — so the focus
group reads as one band whatever the signs in it happen to be.
- `−` is U+2212, signs are explicit on every non-zero figure, `$0` for an exact zero
  (never `−$0.00`).
- `num-neg` / `num-pos` colour only the focus columns; muted cells stay muted.
- ⚠ on any largest-auction share of **30% or more**, and those rows are listed in the
  caveats.
- Every fee cell states its unpriced-auction count when the run reports one.

## Before publishing

- [ ] Every figure in the prose appears in a report or `compare` output for this run.
- [ ] Signs read as counterfactual − actual, and the sign note is present.
- [ ] Gross and net fees are labelled distinctly everywhere they appear.
- [ ] Excluded auctions (price-suspect, missing-JIT, fee-unpriced) are counted on the page.
- [ ] No chain a solver bid on is missing from its rows, including the ones that came back zero.
- [ ] Anything the run measured but the page does not show is named once in the
      footnote, so a reader knows it exists.
- [ ] The register list above was applied to the prose, not just to the headings.
- [ ] `artifact-design` skill loaded before writing the HTML.
- [ ] No total sums across solvers anywhere on the page.
- [ ] Solver rows show an em dash — not a blank, not a zero — for native amounts and the
      median moved order, and single-chain solvers render flat with real native figures.
- [ ] Every chain filter was exercised, including the one that leaves a single chain, and
      the row counts came out at twice the solver count.

The page's script cannot be checked by opening the file: a local preview renders it as a
static snapshot with the table empty. Run the render logic under JavaScriptCore
(`/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc`) against the
real data with a small `document` stub, asserting the row counts per filter state, that a
single-chain solver's aggregate equals its own chain figures, and that a hand-summed
multi-chain solver matches. That catches an empty table before publishing, which a
screenshot of a snapshot never will.

Save to `artifacts/<window>-<slug>.html`, commit it, then publish with the Artifact tool
(`favicon` stable across redeploys, `<title>` stable, same file path to redeploy). To
update a page from an earlier session, pass its `url` — a new path claims a new link.
