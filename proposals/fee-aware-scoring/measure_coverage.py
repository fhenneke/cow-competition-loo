import sys

sys.path.insert(0, ".")

from loo.db import connect, run

conn = connect("mainnet")
with conn.cursor() as cur:
    cur.execute("set statement_timeout = '600s'")

P = {"lo": 13480599, "hi": 13517908}

BASE = """
with ex as (
    select pte.auction_id, pte.solution_uid, pte.order_uid,
           ps.filtered_out, ps.is_winner,
           (pte.order_uid = any(ca.order_uids)) as in_auction,
           o.sell_token, o.buy_token
    from dbt.stg_backend_data__proposed_trade_executions pte
    join dbt.stg_backend_data__proposed_solutions ps
      on ps.auction_id = pte.auction_id and ps.uid = pte.solution_uid
    join dbt.stg_backend_data__competition_auctions ca
      on ca.auction_id = pte.auction_id
    left join dbt.stg_backend_data__orders o on o.uid = pte.order_uid
    where pte.auction_id between %(lo)s and %(hi)s
),
cov as (
    select auction_id, order_uid, bool_or(not filtered_out) as in_kept
    from ex group by 1,2
),
tagged as (
    select ex.*, cov.in_kept,
           (ex.in_auction and not cov.in_kept) as fees_unknown
    from ex join cov using (auction_id, order_uid)
)
"""

print("== 1. execution rows ==")
print(run(conn, BASE + """
select filtered_out, fees_unknown, count(*) as rows,
       count(distinct (auction_id, solution_uid)) as solutions
from tagged group by 1,2 order by 1,2
""", P).to_string())

print("\n== 2. solutions with >=1 unknown-fee order ==")
print(run(conn, BASE + """
, per_sol as (
  select auction_id, solution_uid, bool_or(filtered_out) as filtered_out,
         bool_or(fees_unknown) as any_unknown
  from tagged group by 1,2
)
select filtered_out, any_unknown, count(*) as solutions
from per_sol group by 1,2 order by 1,2
""", P).to_string())

print("\n== 3. directed token pairs (raw sell,buy) ==")
print(run(conn, BASE + """
, per_sol_pair as (
  select auction_id, solution_uid, sell_token, buy_token,
         bool_or(filtered_out) as filtered_out,
         bool_or(fees_unknown) as any_unknown
  from tagged where in_auction group by 1,2,3,4
)
select filtered_out, any_unknown,
       count(*) as solution_pairs,
       count(distinct (auction_id, sell_token, buy_token)) as auction_pairs,
       count(distinct (sell_token, buy_token)) as distinct_pairs
from per_sol_pair group by 1,2 order by 1,2
""", P).to_string())

print("\n== 4. auctions with >=1 unknown-fee order ==")
print(run(conn, BASE + """
select count(distinct auction_id) filter (where fees_unknown) as auctions_affected,
       count(distinct auction_id) as auctions_total
from tagged
""", P).to_string())
