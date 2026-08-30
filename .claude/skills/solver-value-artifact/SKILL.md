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
  dropping a chain overstates concentration.
- **Both outcome rules**, always — they share the extraction pass, so the second is
  nearly free. `inherited` carries the page; `assume-settled` is not a column but the
  settlement caveat needs it to say how far the figures move. Do not add a third
  scenario.
- **Solver names match exactly** — `Arc` is not `Arctic`, and one name can span several
  addresses. A comma-separated `--solver "A,B"` is one *joint* removal, not two runs;
  label such a row as the joint scenario and never attribute it to a member.
- `uv run loo compare out/*.json` across every network is the number source for the
  table. Its row labels are the column labels the page should use.
- A full month per chain takes minutes and re-imports `loo/` on each invocation — do not
  edit `loo/` while a run is in flight, and check `REPORT_FORMAT` (currently 6) matches
  the reports; `compare` refuses stale ones.

## Page structure

Follow [reference/template.html](reference/template.html) section for section: header,
sign note, `Summary`, the solver-chain table, the fee-split section, `Reading each
solver`, `Caveats`, method footnote. The order is fixed so successive months are
diffable against each other.

Table conventions:

- **Two rows per solver-chain, not one.** A `tr.main` carries one value per column and
  nothing else; a `tr.detail` beneath it carries that value's supporting figures in
  smaller muted print, each on its own line, aligned under the column it qualifies. Only
  the detail row draws the rule, so the pair reads as a single entry, and the chain cell
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
  none in the group bands either — the same page has to render a week, a month or a
  quarter. The window is stated once, in bold, in the subtitle, followed by the sentence
  that every table figure is a **total over that window, not a rate**. In prose say "the
  window" rather than "the month" wherever a figure is being described.
- **No positional selectors on cells.** The chain cell spans both rows, so the first
  `td` of a detail row is the Δsurplus detail, not the chain — a `td:first-child` rule
  meant for the chain silently left-aligns one number column instead. Target cells by
  class (`td.chain`, `td.group`, `td.sec`).
- Measure it rather than eyeballing it: the table should come in under the 64rem column
  with no horizontal scroll, and every cell's computed `text-align` should match its
  role. Rendering the table alone in a scratch page, reading `table.scrollWidth`, and
  sweeping `getComputedStyle(cell).textAlign` catches both faults in one pass.
- Group solvers with a `td.group` band carrying that solver's totals; rows within a group
  ordered by Δsurplus magnitude, descending.
- USD on the value row, native amount first on the detail row beneath it.

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
- [ ] No chain a solver bid on is missing from its group.
- [ ] Anything the run measured but the page does not show is named once in the
      footnote, so a reader knows it exists.
- [ ] The register list above was applied to the prose, not just to the headings.
- [ ] `artifact-design` skill loaded before writing the HTML.

Save to `artifacts/<window>-<slug>.html`, commit it, then publish with the Artifact tool
(`favicon` stable across redeploys, `<title>` stable, same file path to redeploy). To
update a page from an earlier session, pass its `url` — a new path claims a new link.
