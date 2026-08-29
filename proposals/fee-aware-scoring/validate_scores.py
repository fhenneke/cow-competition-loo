"""Recompute each solution's CIP-38 score from the analytics DB and compare it against
the recorded `proposed_solutions.score`.

Evidence for PROPOSAL-fee-aware-scoring.md section 4. Run from the repo root:

    uv run --with psycopg2-binary python proposals/fee-aware-scoring/validate_scores.py \
        13505000 13509867
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).parent))

from fees import (
    MathError,
    Order,
    Policy,
    Quote,
    order_score_native,
    quote_fee,
)

from loo.db import as_int, connect, fetch

LO, HI = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (13509000, 13509867)

conn = connect("mainnet")
with conn.cursor() as cur:
    cur.execute("set statement_timeout = '600s'")
params = {"lo": LO, "hi": HI}

solutions = fetch(conn, """
select auction_id, uid, score, filtered_out
from dbt.stg_backend_data__proposed_solutions
where auction_id between %(lo)s and %(hi)s
""", params)

executions = fetch(conn, """
select pte.auction_id, pte.solution_uid,
       encode(pte.order_uid,'hex') as order_uid,
       pte.executed_sell, pte.executed_buy,
       encode(coalesce(o.buy_token, j.buy_token),'hex') as buy_token,
       coalesce(o.sell_amount, j.sell_amount) as sell_amount,
       coalesce(o.buy_amount, j.buy_amount)   as buy_amount,
       coalesce(o.kind::text, j.kind::text)   as kind,
       (pte.order_uid = any(ca.order_uids))   as in_auction
from dbt.stg_backend_data__proposed_trade_executions pte
join dbt.stg_backend_data__competition_auctions ca on ca.auction_id = pte.auction_id
left join dbt.stg_backend_data__orders o on o.uid = pte.order_uid
left join dbt.stg_backend_data__jit_orders j on j.uid = pte.order_uid
where pte.auction_id between %(lo)s and %(hi)s
""", params)

price = {
    (r["auction_id"], r["token"]): as_int(r["price"])
    for r in fetch(conn, """
    select auction_id, encode(token,'hex') as token, price
    from dbt.stg_backend_data__auction_prices
    where auction_id between %(lo)s and %(hi)s
    """, params)
}

# `application_order` is a global serial, so ascending order gives the application
# sequence within one order — which `protocol_fees` then walks in reverse.
policies: dict[tuple[int, str], list[Policy]] = collections.defaultdict(list)
for r in fetch(conn, """
select auction_id, encode(order_uid,'hex') as order_uid, kind::text as kind,
       surplus_factor, surplus_max_volume_factor, volume_factor,
       price_improvement_factor, price_improvement_max_volume_factor
from dbt.stg_backend_data__fee_policies
where auction_id between %(lo)s and %(hi)s
order by auction_id, order_uid, application_order
""", params):
    policies[(r["auction_id"], r["order_uid"])].append(Policy(
        kind=r["kind"],
        surplus_factor=r["surplus_factor"],
        surplus_max_volume_factor=r["surplus_max_volume_factor"],
        volume_factor=r["volume_factor"],
        pi_factor=r["price_improvement_factor"],
        pi_max_volume_factor=r["price_improvement_max_volume_factor"],
    ))

uids = sorted({r["order_uid"] for r in executions})
quotes: dict[str, Quote] = {}
unusable_quotes = 0
for start in range(0, len(uids), 5000):
    chunk = [bytes.fromhex(u) for u in uids[start:start + 5000]]
    for r in fetch(conn, """
    select encode(order_uid,'hex') as order_uid, sell_amount, buy_amount,
           gas_amount, gas_price, sell_token_price
    from dbt.stg_backend_data__order_quotes where order_uid = any(%(ids)s)
    """, {"ids": chunk}):
        try:
            fee = quote_fee(r["gas_amount"], r["gas_price"], r["sell_token_price"])
        except MathError:
            unusable_quotes += 1
            continue
        quotes[r["order_uid"]] = Quote(
            as_int(r["sell_amount"]), as_int(r["buy_amount"]), fee
        )

by_solution: dict[tuple[int, int], list[dict[str, object]]] = collections.defaultdict(list)
for e in executions:
    by_solution[(e["auction_id"], e["solution_uid"])].append(e)

recorded_score = {(s["auction_id"], s["uid"]): as_int(s["score"]) for s in solutions}

stats: collections.Counter[str] = collections.Counter()
differences: list[tuple[tuple[int, int], int, int]] = []

for key, rows in by_solution.items():
    recorded = recorded_score.get(key)
    if recorded is None:
        stats["no recorded score"] += 1
        continue

    total = 0
    reason = ""
    for e in rows:
        # JIT orders carry no fee policies; they contribute only when surplus-capturing,
        # which this check does not cover (see the proposal, section 2).
        if not e["in_auction"]:
            continue
        if e["buy_token"] is None:
            reason = "missing order row"
            break
        native_price = price.get((e["auction_id"], e["buy_token"]))
        if native_price is None:
            reason = "missing native price"
            break
        order_policies = policies.get((e["auction_id"], e["order_uid"]))
        if order_policies is None:
            reason = "no fee policies stored"
            break
        order = Order(
            uid=e["order_uid"],
            side=e["kind"],
            sell_amount=as_int(e["sell_amount"]),
            buy_amount=as_int(e["buy_amount"]),
            executed_sell=as_int(e["executed_sell"]),
            executed_buy=as_int(e["executed_buy"]),
        )
        try:
            score, _ = order_score_native(
                order, order_policies, quotes.get(e["order_uid"]), native_price
            )
        except MathError as err:
            reason = f"math: {err}"
            break
        total += score

    if reason:
        stats[f"skipped: {reason}"] += 1
        continue

    stats["compared"] += 1
    if total == recorded:
        stats["exact"] += 1
    else:
        stats["differs"] += 1
        differences.append((key, recorded, total))

print(f"window {LO}-{HI}: {len(solutions)} solutions, {len(executions)} executions, "
      f"{len(quotes)} quotes ({unusable_quotes} unusable)")
for name, count in stats.most_common():
    print(f"  {name:40s} {count}")

if differences:
    relative = sorted(abs(got - rec) / max(rec, 1) for _, rec, got in differences)
    print(f"\n  differences: {len(differences)}; "
          f"|rel| p50={relative[len(relative) // 2]:.3e} max={relative[-1]:.3e}")
    for key, rec, got in differences[:10]:
        print(f"    {key}  recorded={rec}  recomputed={got}  diff={got - rec}")
