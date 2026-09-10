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
const state = { chain: null, open: new Set() };
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
  state.open.clear();
  document.querySelectorAll(".pill").forEach(q =>
    q.setAttribute("aria-pressed", String(q === p)));
  render();
}));

render();

/* ---- the quote competition ---------------------------------------------
   Its own table, not columns in the focus band: the quote side answers three
   questions and the third of them — what it costs — is already in the Δfee column
   above, because the price-improvement fee is charged against the quote. */
function renderQuotes() {
  const body = document.getElementById("qrows");
  if (!body) return;
  // One row per solver-chain in scope: quote figures are per chain (a quote
  // competition is per network) and are never summed across chains, so a chain
  // filter narrows the rows and a multi-chain page shows the chain beside the name.
  const inScope = DATA.chains.filter(c => !state.chain || c.id === state.chain);
  const rows = [];
  for (const s of DATA.solvers) {
    for (const c of inScope) {
      const chain = s.chains[c.id];
      if (chain && chain.quote && chain.quote.wins > 0) {
        rows.push({ name: s.name, chain: c.id, q: chain.quote });
      }
    }
  }
  rows.sort((a, b) => b.q.wins - a.q.wins);
  const out = [];
  for (const { name, chain, q } of rows) {
    const warn = concentrated(q.largest_share) ? " ⚠" : "";
    const shown = inScope.length > 1
      ? `${label(name)} <span class="chip">${chain}</span>` : label(name);
    out.push(`<tr class="main"><td class="chain" rowspan="2">${shown}</td>` +
      `<td class="${neg(q.window_bps || 0)}">${fmtBps(q.window_bps)}</td>` +
      `<td class="${neg(q.median_bps || 0)}">${fmtBps(q.median_bps, 2)}</td>` +
      `<td>${q.no_quote.toLocaleString("en-US")}</td>` +
      `<td class="sec">${q.largest_share === null ? "n/a"
        : pct(q.largest_share, 0) + warn}</td>` +
      `<td class="${neg(q.fee_recharge_protocol_usd)}">${fmtUsd(q.fee_recharge_protocol_usd)}</td></tr>`);
    out.push(`<tr class="detail">` +
      `<td>${fmtBps(q.conditional_bps, 2)} on the ${
        q.degraded.toLocaleString("en-US")} that moved</td>` +
      `<td>${q.wins_corroborated.toLocaleString("en-US")} of ${
        q.wins.toLocaleString("en-US")} wins used${
        q.class_flips ? `<br>${q.class_flips} lost verification` : ""}</td>` +
      `<td>${q.no_quote_share === null ? "n/a"
        : `${pct(q.no_quote_share, 3)} of ${q.window_orders.toLocaleString("en-US")}`}</td>` +
      `<td class="sec">largest ${fmtBps(q.largest_bps, 0)}</td>` +
      `<td>partner ${fmtUsd(q.fee_recharge_partner_usd)}<br>${
        q.substituted.toLocaleString("en-US")} orders in ${
        q.substituted_auctions.toLocaleString("en-US")} auctions, ${
        q.quote_only_auctions.toLocaleString("en-US")} without its bid<br>largest order ${
        q.fee_recharge_largest_share === null || q.fee_recharge_largest_share === undefined
          ? "n/a" : pct(q.fee_recharge_largest_share, 0)}</td></tr>`);
  }
  body.innerHTML = out.join("\n");
}
