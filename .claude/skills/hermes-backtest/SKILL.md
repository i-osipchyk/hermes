---
name: hermes-backtest
description: Run a Hermes Strategy's backtest and report its metrics, trades, and plots. Use when the user wants to run or backtest a strategy, or see how a strategy performs over historical data.
---

# Hermes: run a backtest

Execute a Strategy over history and surface the result. Diagnosis (the *why*) belongs to
`hermes-analyze-results`; this skill runs and reports.

## Steps

1. **Locate the strategy** — a file under `strategies/` (usually just written by
   `hermes-strategy`) or one the user names. If several exist, ask which.
2. **Run it** with the project venv: `.venv/bin/python strategies/<name>.py` when the file
   has a `__main__` Backtest block, or construct and `.run()` a `Backtest` yourself,
   mirroring `examples/sma_crossover_with_ai.py`. Data fetch is Parquet-cached, so reruns
   are fast; the first run may hit the network.
3. **Report** from the `BacktestResult`:
   - headline `metrics` — total return, CAGR, Sharpe/Sortino, max drawdown, win rate,
     profit factor, number of trades;
   - the trade blotter (entries/exits, P&L, costs, exit reason);
   - plots via `hermes.backtest.reporting` — equity+drawdown and trades-on-price.
4. **Flag the obvious** — a zero-trade run (warmup too long / entry never true), a
   suspiciously perfect equity curve, or too few trades to mean anything — and point the
   user at `hermes-analyze-results` for a real diagnosis.

Completion criterion: the user sees the metrics, the trade list, and at least the equity
plot for a run that actually executed (or a clear reason it produced no trades).
