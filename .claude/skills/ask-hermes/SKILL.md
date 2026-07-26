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
   drawdowns, trade patterns, cost sensitivity, look-ahead/overfitting smells,
   benchmark. Its findings send you back to **`/hermes-strategy`** to iterate.

Keep looping 1→2→3 until the edge holds up (or doesn't).

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

- **`CONTEXT.md`** — the ubiquitous language (Instrument, Bar, Timeframe, Forming
  Bar, Trade, Sizer, Cost Model, AI Advisor…). Use the same words the code does.
- **`docs/adr/`** — the load-bearing semantics: the forming-bar model (0002), the
  fill model (0004), the AI gate (0005), cTrader alignment (0006).
