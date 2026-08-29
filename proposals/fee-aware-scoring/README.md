# Prototype for [PROPOSAL-fee-aware-scoring.md](../../PROPOSAL-fee-aware-scoring.md)

Evidence, not production code. Nothing here is imported by `loo/`; these are the scripts
that produced the numbers in the proposal, kept so they can be re-run.

| file | what it produced |
| --- | --- |
| `fees.py` | the port of `arbitrator.rs` `protocol_fees` — surplus / volume / price-improvement policies, reverse application order, `checked_mul_f64` rounding |
| `validate_scores.py` | §4 — recomputed score vs `proposed_solutions.score`, 19,653/19,653 wei-exact |
| `measure_coverage.py` | §2 — execution / solution / token-pair coverage |
| `measure_recoverability.py` | §3 — how many affected solutions close by residual |

Run from the repo root:

```bash
uv run --with psycopg2-binary python proposals/fee-aware-scoring/validate_scores.py 13505000 13509867
```

The scripts take an auction-id range and set `statement_timeout`; they do not accept a
date window. Map dates to auctions with `WINDOW_SQL` in `loo/extract.py`.
