import sys

sys.path.insert(0, ".")

from loo.db import connect, run

conn = connect("mainnet")
with conn.cursor() as cur:
    cur.execute("set statement_timeout = '600s'")
P = {"lo": 13480599, "hi": 13517908}

BASE = """
with ex as (
    select pte.auction_id, pte.solution_uid, pte.order_uid, ps.filtered_out,
           (pte.order_uid = any(ca.order_uids)) as in_auction,
           o.sell_token, o.buy_token
    from dbt.stg_backend_data__proposed_trade_executions pte
    join dbt.stg_backend_data__proposed_solutions ps
      on ps.auction_id = pte.auction_id and ps.uid = pte.solution_uid
    join dbt.stg_backend_data__competition_auctions ca on ca.auction_id = pte.auction_id
    left join dbt.stg_backend_data__orders o on o.uid = pte.order_uid
    where pte.auction_id between %(lo)s and %(hi)s
),
cov as (
    select auction_id, order_uid, bool_or(not filtered_out) as in_kept
    from ex group by 1,2
),
tagged as (
    select ex.*, (ex.in_auction and not cov.in_kept) as fees_unknown
    from ex join cov using (auction_id, order_uid)
),
per_sol as (
    select auction_id, solution_uid,
           count(distinct (sell_token, buy_token)) filter (where in_auction) as pairs,
           count(distinct (sell_token, buy_token)) filter (where fees_unknown) as unknown_pairs,
           bool_or(fees_unknown) as any_unknown
    from tagged group by 1,2
)
"""

print("== affected filtered-out solutions: how many pairs carry unknown fees ==")
print(run(conn, BASE + """
select pairs, unknown_pairs, count(*) as solutions
from per_sol where any_unknown group by 1,2 order by 1,2
""", P).to_string())

print("\n== recoverability summary ==")
print(run(conn, BASE + """
select
  count(*) filter (where any_unknown) as affected_solutions,
  count(*) filter (where any_unknown and pairs = 1) as single_pair_exempt,
  count(*) filter (where any_unknown and pairs > 1 and unknown_pairs = 1) as residual_exact,
  count(*) filter (where any_unknown and pairs > 1 and unknown_pairs > 1) as needs_bracket
from per_sol
""", P).to_string())
