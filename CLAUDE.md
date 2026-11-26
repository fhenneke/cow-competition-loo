# Leave-one-out solver analysis

Remove one solver, or several jointly, from past CoW Protocol auctions, re-run winner
selection, and measure the impact on user surplus, solver rewards, fees and coverage.
`loo` replays only the mechanism live since `extract.MECHANISM_START` (2026-10-28).

Facts about the data and the protocol that the code cannot show live in `docs/`:
[analytics-db](docs/analytics-db.md), [winner-selection](docs/winner-selection.md),
[rewards](docs/rewards.md), [quotes](docs/quotes.md). Commands: [README](README.md).
Results pages: the [solver-value-artifact skill](.claude/skills/solver-value-artifact/SKILL.md).

## Traps that silently give wrong answers

1. `proposed_trade_executions.solution_uid` joins `proposed_solutions.uid`, not `.id`.
2. `proposed_solutions.uid` is assigned best-to-worst and is the arbitrator's only
   tie-break record. It is not plain score order.
3. Surplus and score are not interchangeable, so never stand one in for the other.
4. Auction membership comes from `competition_auctions.order_uids`. Fee policies are
   not membership.
5. `int_backend_data__proposed_solution_data` lags staging by days, so query its
   `max(auction_id)`. It has no `filtered_out` column. The window's block times and
   auction model lag too, which `loo` refuses with exit 5; ad-hoc window queries do not.
6. `order_surplus_atoms_in_surplus_token` can be one atom high. Use exact integer ceiling
   division.
7. Executed (`tx_hash is not null`) is not settled in time (`is_settled_in_time`). `loo`
   uses `is_settled_in_time` for both surplus and reward.
8. Match solver names exactly (`Arc` is a substring of `Arctic`). One name can cover
   several addresses, and all of them go together.
9. Partner fees are the trailing `n` fee policies, with
   `n = array_length(app_data.partner_fee_recipients, 1)`. `partner_fee_item_count` is
   always NULL.
10. The mart's native fees use corrected prices, but scoring did not, so diff atoms, not
    native values.
11. Native prices come only from the `competition_auctions` price arrays. Go through
    `extract.fetch_prices`, which also puts Solana's prices (lamports ×1e9) onto 1e18.
12. JIT orders are not read. That is exact only while `surplus_capturing_jit_order_owners`
    is empty.
13. Fast-path auctions (`fast_path_quote_id` non-NULL) are excluded, not arbitrated.
14. `int_backend_data__solution_data.lower_reward_cap` is not the floor, so read it through
    `rewards.lower_reward_cap`.
15. Quotes: link an order to its competition by `order_quotes.quote_id` (staging's copy is
    NULL only before ~2026-10-06, raw's before 2026-09-22), and read only `raw_quotes`,
    never `gold_quotes`. The quote winner need not have bid on its order, and quote-side
    guards run after the did-the-solver-win test.
16. A price check that compares two feeds for the same token passes a token both feeds
    get wrong. Instead, compare what was sold with what was bought, at that auction's
    prices.
17. Bound any join against `proposed_solutions` to the window's `auction_id` min/max, or
    it becomes a sequential scan of the whole table.
18. The reference checkouts `~/Work/Code/services` and `~/Work/Code/cow-dagster` are
    read-only and go stale. Compare them against `git ls-remote origin main` before
    trusting either.

## Working here

- Check with `uv run ruff check .`, `uv run pyright` (strict) and `uv run pytest`. Dev
  tooling installs with `uv sync --extra dev`.
- **No unit tests.** `tests/` is a deterministic simulation: seeded synthetic worlds run
  through the real pipeline and are checked by invariants and by a naive winner-selection
  oracle. When you find a bug, extend the generator or an invariant until a seed finds
  it, then pin that seed. Check against production with `loo validate` and
  `loo validate-rewards`.
- Run ad-hoc queries with `uv run python` and `loo.db.connect(network)`. The DB user is
  read-only. Bound every query with an `auction_id` range or a `LIMIT`, and add
  `set statement_timeout = '120s'`.
- Runs wait on the DB, and their runtimes drift by 2x or more from day to day. Quote a
  range with the date it was measured. Launch long runs as `caffeinate -i uv run loo …`.
  Exit 6 means the network failed, not the data, so rerun unchanged.
- A rerun of a past window replays its snapshot and does not touch the DB
  ([README](README.md#snapshots-a-rerun-of-a-past-window-needs-no-db)). Delete the
  window's snapshot directory to read the DB afresh.
- Do not edit `loo/` while a run is in flight, because each run re-imports from disk.
- `.env` stays out of commits.

## Committing

Each milestone is a branch that lands on `main` as one squashed commit, and the branch
is kept: `git merge --squash <branch>`, then one message, then check that
`git diff main <branch>` is empty. How commits are signed is up to each developer.
