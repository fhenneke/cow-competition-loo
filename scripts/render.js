const M = "−";
/* Every formatter signs the *displayed* rounding, not the raw value: a −0.004 that
   renders as 0.00 is an exact zero to the reader and must not carry a minus. */
const fmtUsd = v => {
  const m = Math.abs(v || 0);
  const shown = m >= 100 ? Math.round(m).toLocaleString("en-US") : m.toFixed(2);
  if (Number(shown.replace(/,/g, "")) === 0) return "$0";
  return `${v < 0 ? M : "+"}$${shown}`;
};
const fmtNat = (v, sym) => {
  const shown = Math.abs(v || 0).toLocaleString("en-US",
    {minimumFractionDigits: 4, maximumFractionDigits: 4});
  if (Number(shown.replace(/,/g, "")) === 0) return `0 ${sym}`;
  return `${v < 0 ? M : "+"}${shown} ${sym}`;
};
/* Recorded amounts, not deltas: no plus sign. Declared up here because `render()` runs
   at load, before anything declared below it with `const` exists. */
const fmtUsdAmt = v => {
  const m = Math.abs(v || 0);
  const shown = m >= 100 ? Math.round(m).toLocaleString("en-US") : m.toFixed(2);
  if (Number(shown.replace(/,/g, "")) === 0) return "$0";
  return `${v < 0 ? M : ""}$${shown}`;
};
const fmtNatAmt = (v, sym) => {
  const shown = Math.abs(v || 0).toLocaleString("en-US",
    {minimumFractionDigits: 4, maximumFractionDigits: 4});
  if (Number(shown.replace(/,/g, "")) === 0) return `0 ${sym}`;
  return `${v < 0 ? M : ""}${shown} ${sym}`;
};
const fmtBps = (v, p = 3) => {
  if (v === null || v === undefined) return "n/a";
  const shown = Math.abs(v).toFixed(p);
  if (Number(shown) === 0) return `${shown} bps`;
  return `${v < 0 ? M : "+"}${shown} bps`;
};
const pct = (v, p = 2) => v === null || v === undefined ? "n/a" : `${(v * 100).toFixed(p)}%`;
/* The ⚠ threshold is applied to the share as displayed (whole percent), so two rows
   that both read "30%" are both flagged or neither is. */
const concentrated = share => share !== null && share !== undefined
  && Math.round(share * 100) >= 30;
const neg = v => v < 0 ? "num-neg" : "num-pos";
/* A solver with no catalogue entry is named by its 40-hex submission address, which on
   its own sets the label column to 348px — a third of the 64rem page. Abbreviate it the
   way `loo compare` does; the full address is in the report file. */
const label = name => /^[0-9a-f]{40}$/.test(name)
  ? `Uncatalogued 0x${name.slice(0, 6)}…` : name;

/* Aggregate one solver across the chains in scope.
   USD sums; native does not (ETH + xDAI is not a number) and neither does a median of
   medians, so both come back null and render as an em dash. */
function aggregate(rows) {
  const sum = k => rows.reduce((a, r) => a + (r[k] || 0), 0);
  const volUsd = rows.reduce((a, r) => a + r.window_volume * r.rate, 0);
  const movedUsd = rows.reduce((a, r) => a + r.pi_delta * r.rate, 0);
  const baseUsd = rows.reduce((a, r) => a + r.pi_volume * r.rate, 0);
  const usd_s = sum("usd_s");
  const biggest = rows.reduce((a, r) =>
    Math.abs(r.largest_usd) > Math.abs(a) ? r.largest_usd : a, 0);
  const biggestFee = rows.reduce((a, r) =>
    Math.abs(r.largest_fee_usd || 0) > Math.abs(a) ? r.largest_fee_usd : a, 0);
  return {
    usd_s, usd_f: sum("usd_f"), usd_pr: sum("usd_pr"), usd_pa: sum("usd_pa"),
    usd_only: sum("usd_only"),
    native_s: null, native_f: null, median: null, sym: null,
    bps: volUsd ? movedUsd * 10000 / volUsd : null,
    still: baseUsd ? movedUsd * 10000 / baseUsd : null,
    moved: sum("moved"), moved_auctions: sum("moved_auctions"),
    orders: sum("orders"), executions: sum("executions"),
    cov: sum("executions") ? sum("orders") / sum("executions") : null,
    won: sum("won"), analysed: sum("analysed"), bid: sum("bid"),
    unpriced: sum("unpriced"),
    share: usd_s ? Math.abs(biggest) / Math.abs(usd_s) : null,
    fee_share: sum("usd_f") ? Math.abs(biggestFee) / Math.abs(sum("usd_f")) : null,
  };
}

function cells(d, isAgg) {
  const warn = concentrated(d.share) ? " ⚠" : "";
  const dash = '<span class="na" title="not summable across chains">—</span>';
  return {
    main: [
      `<td class="${neg(d.usd_s)}">${fmtUsd(d.usd_s)}</td>`,
      `<td class="${neg(d.bps || 0)}">${fmtBps(d.bps)}</td>`,
      `<td class="${neg(d.usd_f)}">${fmtUsd(d.usd_f)}</td>`,
      `<td>${pct(d.cov)}</td>`,
      `<td class="sec">${d.analysed ? pct(d.won / d.analysed, 1) : "n/a"}</td>`,
      `<td class="sec">${d.share === null ? "n/a" : pct(d.share, 0)}${warn}</td>`,
    ],
    detail: [
      `<td>${isAgg ? dash : fmtNat(d.native_s, d.sym)}</td>`,
      `<td>still-traded ${fmtBps(d.still, 2)}<br>median ${
        isAgg ? dash : fmtBps(d.median, 2)}<br>${d.moved.toLocaleString("en-US")} moved</td>`,
      `<td>${isAgg ? dash : fmtNat(d.native_f, d.sym)}<br>protocol ${fmtUsd(d.usd_pr)}<br>partner ${
        fmtUsd(d.usd_pa)}${d.fee_share !== null && d.fee_share !== undefined
          ? `<br>largest auction ${pct(d.fee_share, 0)}` : ""}${
        d.unpriced ? `<br>${d.unpriced} auctions unpriced` : ""}</td>`,
      `<td>${d.orders.toLocaleString("en-US")} orders<br>${fmtUsd(d.usd_only)} surplus</td>`,
      `<td>${d.won.toLocaleString("en-US")} won<br>bid ${
        d.analysed ? pct(d.bid / d.analysed, 1) : "n/a"}</td>`,
      "<td></td>",
    ],
  };
}

/* ---- state ------------------------------------------------------------- */
const state = { chain: null, open: new Set(), openPaid: new Set(), openQuote: new Set() };
const single = DATA.chains.length === 1;

function scoped(solver) {
  return DATA.chains
    .filter(c => !state.chain || c.id === state.chain)
    .map(c => solver.chains[c.id])
    .filter(Boolean);
}

function render() {
  const body = document.getElementById("rows");
  const out = [];
  const entries = DATA.solvers
    .map(s => ({ s, rows: scoped(s) }))
    .filter(e => e.rows.length)
    .map(e => ({ ...e, agg: aggregate(e.rows) }))
    .sort((a, b) => Math.abs(b.agg.usd_s) - Math.abs(a.agg.usd_s));

  for (const { s, agg } of entries) {
    const chainIds = DATA.chains.filter(c => s.chains[c.id] &&
      (!state.chain || c.id === state.chain)).map(c => c.id);
    // With one chain in scope the aggregate *is* the chain row: render it flat, with
    // that chain's native figures rather than a dash, and no disclosure control.
    const flat = chainIds.length === 1;
    const d = flat ? s.chains[chainIds[0]] : agg;
    const c = cells(d, !flat);
    const open = state.open.has(s.name);
    const shown = label(s.name);
    const chainLabel = flat && !state.chain && !single
      ? `${shown} <span class="chip">${chainIds[0]}</span>` : shown;
    const control = flat ? chainLabel
      : `<button class="disclose" aria-expanded="${open}" data-solver="${s.name}">
           <span class="tri" aria-hidden="true">${open ? "▾" : "▸"}</span>${shown}
           <span class="chip">${chainIds.length} chains</span></button>`;
    out.push(`<tr class="main"><td class="chain" rowspan="2">${control}</td>${c.main.join("")}</tr>`);
    out.push(`<tr class="detail">${c.detail.join("")}</tr>`);

    if (open && !flat) {
      for (const id of chainIds) {
        const cd = cells(s.chains[id], false);
        out.push(`<tr class="main sub"><td class="chain sub" rowspan="2">
          <span class="chip">${id}</span></td>${cd.main.join("")}</tr>`);
        out.push(`<tr class="detail sub">${cd.detail.join("")}</tr>`);
      }
    }
  }
  body.innerHTML = out.join("\n");
  const chain = state.chain || (single ? DATA.chains[0].id : null);
  document.getElementById("scope").textContent = chain
    ? `${entries.length} solvers that bid on ${chain}`
    : `${entries.length} solvers, ${DATA.chains.length} chains`;
  renderQuotes();
  renderPaid();
}

document.getElementById("rows").addEventListener("click", e => {
  const b = e.target.closest(".disclose");
  if (!b) return;
  const n = b.dataset.solver;
  state.open.has(n) ? state.open.delete(n) : state.open.add(n);
  render();
});

document.querySelectorAll(".pill").forEach(p => p.addEventListener("click", () => {
  state.chain = p.dataset.chain || null;
  for (const set of [state.open, state.openPaid, state.openQuote]) set.clear();
  document.querySelectorAll(".pill").forEach(q =>
    q.setAttribute("aria-pressed", String(q === p)));
  render();
}));


/* ---- nested tables: one row per solver, opening to its chains ----------
   The reward and quote tables follow the solver table: removing a solver from each chain
   is an independent scenario, so its chains add up; different solvers' rows never do.
   With one chain in scope a solver's row is that chain's row, rendered flat. */
const dashCell = why => `<span class="na" title="${why}">—</span>`;
const NOT_SUMMABLE = "not summable across chains";

function renderNested(bodyId, open, entries, cellsFor) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  const out = [];
  for (const { s, ids, agg } of entries) {
    const flat = ids.length === 1;
    const d = flat ? s.chains[ids[0]] : agg;
    const c = cellsFor(d, !flat);
    const isOpen = open.has(s.name);
    const shown = label(s.name);
    const control = flat
      ? (!state.chain && !single ? `${shown} <span class="chip">${ids[0]}</span>` : shown)
      : `<button class="disclose" aria-expanded="${isOpen}" data-solver="${s.name}">
           <span class="tri" aria-hidden="true">${isOpen ? "▾" : "▸"}</span>${shown}
           <span class="chip">${ids.length} chains</span></button>`;
    out.push(`<tr class="main"><td class="chain" rowspan="2">${control}</td>${c.main.join("")}</tr>`);
    out.push(`<tr class="detail">${c.detail.join("")}</tr>`);
    if (isOpen && !flat) {
      for (const id of ids) {
        const cd = cellsFor(s.chains[id], false);
        out.push(`<tr class="main sub"><td class="chain sub" rowspan="2">
          <span class="chip">${id}</span></td>${cd.main.join("")}</tr>`);
        out.push(`<tr class="detail sub">${cd.detail.join("")}</tr>`);
      }
    }
  }
  body.innerHTML = out.join("\n");
}

function nestedEntries(keep, aggregateRows, sortKey) {
  return DATA.solvers
    .map(s => ({
      s,
      ids: DATA.chains.map(c => c.id)
        .filter(id => (!state.chain || id === state.chain) && s.chains[id] && keep(s.chains[id])),
    }))
    .filter(e => e.ids.length)
    .map(e => ({ ...e, agg: aggregateRows(e.ids.map(id => e.s.chains[id])) }))
    .sort((a, b) => sortKey(b.agg) - sortKey(a.agg));
}

/* ---- the quote competition ---------------------------------------------
   Its own table, not columns in the focus band: the quote side answers three
   questions and the third of them — what it costs — is already in the Δfee column
   above, because the price-improvement fee is charged against the quote. */
const quoteUsdVolume = q => q.window_volume * q.usd_per_native;

function aggregateQuotes(quotes) {
  const sum = k => quotes.reduce((a, q) => a + (q[k] || 0), 0);
  const delta = sum("delta_usd");
  const volume = quotes.reduce((a, q) => a + quoteUsdVolume(q), 0);
  const fee = sum("fee_recharge_usd");
  // The largest single order's |Δ| is its share of its own chain's total.
  const biggest = Math.max(0, ...quotes.map(q => (q.largest_share || 0) * Math.abs(q.delta_usd)));
  const biggestFee = Math.max(0, ...quotes.map(
    q => (q.fee_recharge_largest_share || 0) * Math.abs(q.fee_recharge_usd)));
  return {
    wins: sum("wins"), delta_usd: delta, window_orders: sum("window_orders"),
    window_bps: volume ? delta * 10000 / volume : null,
    conditional_bps: null, median_bps: null, largest_bps: null,
    degraded: sum("degraded"), class_flips: sum("class_flips"), no_quote: sum("no_quote"),
    no_quote_share: sum("window_orders") ? sum("no_quote") / sum("window_orders") : null,
    largest_share: delta ? biggest / Math.abs(delta) : null,
    fee_recharge_usd: fee, fee_recharge_protocol_usd: sum("fee_recharge_protocol_usd"),
    fee_recharge_partner_usd: sum("fee_recharge_partner_usd"),
    fee_recharge_largest_share: fee ? biggestFee / Math.abs(fee) : null,
    substituted: sum("substituted"), substituted_auctions: sum("substituted_auctions"),
    quote_only_auctions: sum("quote_only_auctions"),
  };
}

function quoteCells(q, isAgg) {
  const warn = concentrated(q.largest_share) ? " ⚠" : "";
  return {
    main: [
      `<td class="${neg(q.window_bps || 0)}">${fmtBps(q.window_bps)}</td>`,
      `<td class="${neg(q.median_bps || 0)}">${isAgg ? dashCell(NOT_SUMMABLE) : fmtBps(q.median_bps, 2)}</td>`,
      `<td>${q.no_quote.toLocaleString("en-US")}</td>`,
      `<td class="sec">${q.largest_share === null ? "n/a" : pct(q.largest_share, 0) + warn}</td>`,
      `<td class="${neg(q.fee_recharge_protocol_usd)}">${fmtUsd(q.fee_recharge_protocol_usd)}</td>`,
    ],
    detail: [
      `<td>${isAgg ? `${fmtUsd(q.delta_usd)} over ${q.degraded.toLocaleString("en-US")} that moved`
        : `${fmtBps(q.conditional_bps, 2)} on the ${q.degraded.toLocaleString("en-US")} that moved`}</td>`,
      `<td>${q.wins.toLocaleString("en-US")} wins${
        q.class_flips ? `<br>${q.class_flips} lost verification` : ""}</td>`,
      `<td>${q.no_quote_share === null ? "n/a"
        : `${pct(q.no_quote_share, 3)} of ${q.window_orders.toLocaleString("en-US")}`}</td>`,
      `<td class="sec">${isAgg ? "" : `largest ${fmtBps(q.largest_bps, 0)}`}</td>`,
      `<td>partner ${fmtUsd(q.fee_recharge_partner_usd)}<br>${
        q.substituted.toLocaleString("en-US")} orders in ${
        q.substituted_auctions.toLocaleString("en-US")} auctions, ${
        q.quote_only_auctions.toLocaleString("en-US")} without its bid<br>largest order ${
        q.fee_recharge_largest_share === null || q.fee_recharge_largest_share === undefined
          ? "n/a" : pct(q.fee_recharge_largest_share, 0)}</td>`,
    ],
  };
}

function renderQuotes() {
  const withQuote = d => d.quote && d.quote.wins > 0;
  // A chain row carries its quote; the cell builder sees the quote object either way.
  const entries = nestedEntries(withQuote, rows => aggregateQuotes(rows.map(chainQuote)),
    a => a.wins);
  renderNested("qrows", state.openQuote,
    entries.map(e => ({ ...e, s: { ...e.s, chains: Object.fromEntries(
      e.ids.map(id => [id, chainQuote(e.s.chains[id])])) } })),
    quoteCells);
}

/* The USD rate travels with the quote so the summed bps can be recomputed. */
const chainQuote = d => ({ ...d.quote, usd_per_native: d.rate * 1e18 });

/* ---- contribution and reward, side by side ------------------------------
   Its own table, not columns in the focus band: three groups (winning, second-best bids,
   quotes), each a contribution with bids fixed, then the reward that pays for it. Reward
   columns are recorded amounts, not deltas, so they are unsigned and uncoloured. Six
   columns after the solver: Δscore | performance | Δother winners' margin, settled
   winners, uncapped (capped Δrewards beneath) | consistency (÷ margin beneath) | Δquote |
   quote. */
const COMPONENTS = ["performance", "consistency", "quote"];

function aggregatePaid(rows) {
  const sum = (xs, k) => xs.reduce((a, x) => a + (x[k] || 0), 0);
  const paid = rows.map(r => r.paid), ru = rows.map(r => r.runner_up);
  const quotes = rows.filter(r => r.quote).map(chainQuote);
  const unaccounted = Object.fromEntries(COMPONENTS.map(c => [c, sum(paid.map(p => p.unaccounted), c)]));
  const settledKnown = ru.every(r => r.settled_usd !== null);
  const settled = sum(ru, "settled_usd"), consistency = sum(paid, "consistency_usd");
  return {
    usd_score: sum(rows, "usd_score"), usd_s: sum(rows, "usd_s"), usd_f: sum(rows, "usd_f"),
    native_score: null, sym: null, bid: sum(rows, "bid"),
    paid: {
      ...Object.fromEntries(COMPONENTS.map(c => [c + "_usd", sum(paid, c + "_usd")])),
      unaccounted, auctions: sum(paid, "auctions"),
    },
    runner_up: {
      settled_usd: settledKnown ? settled : null, settled: null,
      rewards_usd: ru.every(r => r.rewards_usd !== null) ? sum(ru, "rewards_usd") : null,
      auctions: sum(ru, "auctions"), cap_unknown: sum(ru, "cap_unknown"),
      // Native ratios do not add across chains, so the summed row's ratio is in USD.
      ratio: settledKnown && settled > 0 ? consistency / settled : null,
    },
    quote: quotes.length ? aggregateQuotes(quotes) : null,
  };
}

function paidCell(p, component, sym, isAgg) {
  const missing = p.unaccounted[component];
  if (missing && missing >= p.auctions) {
    return { main: "<td>pending</td>",
      detail: `<td>${missing.toLocaleString("en-US")} auctions not yet accounted</td>` };
  }
  return { main: `<td>${fmtUsdAmt(p[component + "_usd"])}</td>`,
    detail: `<td>${isAgg ? dashCell(NOT_SUMMABLE) : fmtNatAmt(p[component], sym)}${missing
      ? `<br>${missing.toLocaleString("en-US")} auctions not yet accounted` : ""}</td>` };
}

function paidCells(d, isAgg) {
  const p = d.paid, r = d.runner_up, q = d.quote;
  const none = why => `<td class="group-start">${dashCell(why)}</td>`;
  const [perf, cons, quote] = COMPONENTS.map(c => paidCell(p, c, d.sym, isAgg));
  const native = v => isAgg ? dashCell(NOT_SUMMABLE) : fmtNat(v, d.sym);
  const settled = r.settled_usd === null ? none("no settlement records in surplus mode")
    : `<td class="group-start ${neg(r.settled_usd)}">${fmtUsd(r.settled_usd)}</td>`;
  const quoted = !q ? none("the run had no quote pass")
    : `<td class="group-start ${neg(q.delta_usd)}">${fmtUsd(q.delta_usd)}</td>`;
  return {
    main: [
      `<td class="group-start ${neg(d.usd_score)}">${fmtUsd(d.usd_score)}</td>`, perf.main,
      settled, cons.main, quoted, quote.main,
    ],
    detail: [
      `<td class="group-start">${native(d.native_score)}<br>Δsurplus ${fmtUsd(d.usd_s)}<br>Δfees ${
        fmtUsd(d.usd_f)}</td>`,
      perf.detail,
      `<td class="group-start">${r.settled_usd === null ? "" : native(r.settled) + "<br>"}${
        r.rewards_usd === null ? "" : `capped Δrewards ${fmtUsd(r.rewards_usd)}<br>`}runner-up in ${
        r.auctions.toLocaleString("en-US")}<br>${
        d.bid ? pct(r.auctions / d.bid, 1) : "n/a"} of its bids${
        r.cap_unknown ? `<br>${r.cap_unknown} auctions cap unknown` : ""}</td>`,
      cons.detail.replace("</td>", `<br>÷ margin ${
        r.ratio === null || r.ratio === undefined ? "n/a" : r.ratio.toFixed(2)}${
        isAgg && r.ratio !== null ? " (USD)" : ""}</td>`),
      q ? `<td class="group-start">${fmtBps(q.window_bps, 2)} of executed volume<br>${
        q.no_quote.toLocaleString("en-US")} orders not quoted<br>${
        q.wins.toLocaleString("en-US")} quote wins</td>` : '<td class="group-start"></td>',
      quote.detail,
    ],
  };
}

function renderPaid() {
  const keep = d => d.paid && d.runner_up;
  // Same order as the solver table: by |Δsurplus| over the chains in scope.
  renderNested("prows", state.openPaid, nestedEntries(keep, aggregatePaid, a => Math.abs(a.usd_s)),
    paidCells);
}

for (const [id, key] of [["prows", "openPaid"], ["qrows", "openQuote"]]) {
  const body = document.getElementById(id);
  if (!body) continue;
  body.addEventListener("click", e => {
    const b = e.target.closest(".disclose");
    if (!b) return;
    const set = state[key], n = b.dataset.solver;
    set.has(n) ? set.delete(n) : set.add(n);
    render();
  });
}

// Last, once every constant above exists.
render();
