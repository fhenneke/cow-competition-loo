# Leave-one-out solver analysis

Counterfactual analysis of CoW Protocol solver competition: remove one solver from past
auctions, re-run winner selection, and measure the impact on user surplus, solver rewards,
and order coverage.

- **[PLAN.md](PLAN.md)** — what to build, milestone by milestone. Start here.
- **[docs/analytics-db.md](docs/analytics-db.md)** — DB connection, tables, join keys,
  coverage, date→auction mapping, reading dbt model source.
- **[docs/winner-selection.md](docs/winner-selection.md)** — the algorithm, Rust source
  map, score/surplus formulas.
- **[docs/rewards.md](docs/rewards.md)** — reward and cap formulas.

Keep the split: `PLAN.md` is the concrete work, `docs/` is durable context. When you learn
something reusable about the data or the protocol, it goes in `docs/`.

## Local repos this analysis depends on

| Path | Use |
| --- | --- |
| `~/Work/Code/services` | autopilot + `winner-selection` crate — the algorithm being replicated |
| `~/Work/Code/cow-dagster` | the dbt project — the only place model SQL can be read |

Both are read-only references here. Do not modify them.

## Gotchas that cost time

1. `ANALYTICS_DB_URL` in `.env` is `user:pass@host:port` — **no scheme, no dbname**.
   `psycopg2.connect(url)` fails on it.
2. `proposed_trade_executions.solution_uid` joins `proposed_solutions.uid`, **not `.id`**.
   The two collide for some rows, so the wrong join half-works silently.
3. `stg_backend_data__fee_policies` only has rows for **executed** orders, so it is not a
   test for auction membership and protocol fees cannot be recomputed for unexecuted orders.
4. Surplus and score are **not** interchangeable — median score/surplus is 1.08, p90 is
   2.70, and 3.2% of bids are fee-dominated. See
   [docs/winner-selection.md](docs/winner-selection.md#measured-divergence--surplus-is-not-a-stand-in-for-score).
5. `int_backend_data__proposed_solution_data` lags the raw tables **badly and variably** —
   7.5 days when measured on 2026-08-11. Query its `max(auction_id)` rather than assuming.
   It also has no `filtered_out` column, so the fairness-filter ground truth only comes
   from `stg_backend_data__proposed_solutions`.
6. `order_surplus_atoms_in_surplus_token` is up to one atom too high: Postgres `numeric`
   division rounds the quotient before the model's `ceil()` sees it. Use exact integer
   ceiling division. See
   [docs/analytics-db.md](docs/analytics-db.md#order_surplus_atoms_in_surplus_token-rounds).
7. `proposed_solutions.uid` is assigned best-to-worst and is the only record of the
   arbitrator's tie-breaks. Reference scores depend on that ordering, which is *not* plain
   score order — see
   [docs/winner-selection.md](docs/winner-selection.md#ranked-order-is-load-bearing).

## Environment

- No `psql`; `psycopg2` is not in the system Python. Use `uv run --with psycopg2-binary`.
- The DB user is `<db-user>` — read-only, and it should stay that way.
- `.env` is gitignored and must stay out of commits.

## Committing

Commits are signed with a YubiKey and need a physical tap. Run `git commit` as a
**background** command and ask for the tap in the very next message — see
[.claude/hardware-key-signing.md](.claude/hardware-key-signing.md).
