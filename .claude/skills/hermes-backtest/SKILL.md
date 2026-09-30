---
name: hermes-backtest
description: Run a Hermes Strategy's backtest and report its metrics, trades, and plots. Use when the user wants to run or backtest a strategy, or see how a strategy performs over historical data.
---

# Hermes: run a backtest

Execute a Strategy over history and surface the result. Diagnosis (the *why*) belongs to
`hermes-analyze-results`; this skill runs and reports.

## Use the CLI

`hermes` runs the loop; don't hand-roll a script unless the question needs one.
Every command takes `--json` (one JSON document on stdout, progress on stderr) and
exits 0 = fine, 2 = ran but produced nothing usable, 1 = bad request.

```bash
.venv/bin/hermes strategies                    # what's runnable (+ anything broken)
.venv/bin/hermes run <name>                    # run it; records it in the ledger
.venv/bin/hermes run <name> --symbol AAPL --start 2021-01-01 -p fast=10
.venv/bin/hermes runs                          # what has already been tried
.venv/bin/hermes show <key> --trades           # a stored run + its blotter
```

**Runs are ledger-cached by input hash**, so re-running an identical configuration is
free and returns `"from_cache": true`. Editing the strategy file changes the hash, so
you never get a stale result for changed code. `--no-cache` forces a re-run.

Before reporting "there's no such strategy", check `hermes strategies` — a file that
fails to import is listed as broken with its error, and that's usually the real problem.

## Steps

1. **Locate the strategy** — `hermes strategies`. If several fit and the user was vague,
   ask which.
2. **Run it** — `hermes run <name>` with any overrides. For a question the CLI can't
   express, construct a `Backtest` yourself, mirroring
   `examples/sma_crossover_with_ai.py`.
3. **Report** from the result:
   - headline metrics — total return, CAGR, Sharpe/Sortino, max drawdown, win rate,
     profit factor, number of trades;
   - the trade blotter (`hermes show <key> --trades`);
   - plots via `hermes.backtest.reporting` — equity+drawdown and trades-on-price.
4. **Flag the obvious** — a zero-trade run (warmup too long / entry never true), a
   suspiciously perfect equity curve, or too few trades to mean anything — and point the
   user at `hermes-analyze-results` for a real diagnosis. For an AI-gated run, check
   `result.to_dict()["llm_summary"]["fail_open_calls"]`: non-zero means the provider
   errored and trades were approved without a real decision, so the metrics describe a
   less-gated strategy than the one configured.

To enumerate what is runnable, `hermes.webui.discovery.discover_all(...)` returns
`(working, broken)` — a strategy file that fails to import shows up as a `BrokenStrategy`
with its traceback instead of taking the whole scan down. Fix a broken file before
reporting that a strategy is "missing".

Completion criterion: the user sees the metrics, the trade list, and at least the equity
plot for a run that actually executed (or a clear reason it produced no trades).
