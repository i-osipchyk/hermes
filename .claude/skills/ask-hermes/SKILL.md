---
name: ask-hermes
description: Which Hermes skill fits your situation — a router over the strategy-building skills in this repo.
disable-model-invocation: true
---

# Ask Hermes

You don't remember every skill, so ask. These skills help you build, test, and grow
trading strategies with the Hermes framework (see `README.md` / `CONTEXT.md`).

A **flow** is a path through the skills. Most work runs the main flow; two supports
feed into it.

## The main flow: idea → validated strategy

The loop most work travels — an idea, made real, then judged.

1. **`/hermes-strategy`** — sharpen a trading idea by interview into a runnable
   Strategy. Start here. It writes `strategies/<name>.py` (a Strategy subclass + a
   Backtest config) and stops — it doesn't run anything.
2. **`hermes-backtest`** (fires on its own) — run that strategy over historical data
   and report headline metrics, the trade blotter, and equity/trade plots.
3. **`hermes-analyze-results`** (fires on its own) — diagnose *why* it wins or loses:
   P&L concentration, cost sensitivity, out-of-sample decay, statistical validation,
   regime split, and whether any AI gate was in effect. Its findings send you back to
   **`/hermes-strategy`** to iterate.

Keep looping 1→2→3 until the edge holds up (or doesn't).

Steps 2 and 3 are `hermes` commands, not bespoke scripts:

```bash
.venv/bin/hermes strategies        # what can I run?
.venv/bin/hermes run <name>        # run it; recorded in the ledger
.venv/bin/hermes runs              # what have I already tried?
.venv/bin/hermes analyze <key>     # execute the rubric; writes analysis.json
.venv/bin/hermes review <key>      # a written verdict over that evidence
```

Add `--json` to any of them for one parseable document on stdout. Runs are cached by
input hash, so repeating a configuration costs nothing and `hermes runs` is a real
experiment log — check it before re-running something you may have already tried.

## Supports

Feed into the main flow rather than sitting on it.

- **`hermes-explore-data`** (fires on its own) — understand an instrument before you
  design for it: fetch candles via a DataSource and plot/analyse them (volatility,
  session gaps, indicator previews). Reach for it when the *data*, not the strategy,
  is the question — usually **before** `/hermes-strategy`.
- **`hermes-extend`** (fires on its own) — when a strategy needs an **Indicator**,
  **DataSource**, or live **ExecutionVenue** the library doesn't ship yet. It scaffolds
  the new subclass against Hermes's interfaces, then you return to the main flow.

## Underneath

The single sources of truth every skill reads instead of duplicating:

- **`CONTEXT.md`** — the ubiquitous language. Two sections: the v1 terms for **one**
  backtest (Instrument, Bar, Timeframe, Forming Bar, Trade, Sizer, Cost Model, AI
  Advisor…), then the terms for **running many** (Universe/Portfolio Backtest,
  Walk-forward, Cost Sensitivity, Regime Analysis, Random Baseline, Statistical
  Validation). Use the same words the code does.
- **`docs/adr/`** — the load-bearing semantics: the forming-bar model (0002), the
  fill model (0004), the AI gate (0005), cTrader alignment (0006), maker/taker costs
  (0009), and the agent-first direction (0010).
- **`CLAUDE.md`** — repo conventions: which gates are authoritative, the strategy-file
  contract, what never to commit.
- **`src/hermes/__init__.py`** — the complete public inventory. Every capability in
  Hermes is importable from `hermes` directly, so
  `python -c "import hermes; print(hermes.__all__)"` is an honest list of what you can
  reach for. If a rubric axis needs a primitive, it is probably already there.
