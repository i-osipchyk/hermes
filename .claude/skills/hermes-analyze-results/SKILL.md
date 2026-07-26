---
name: hermes-analyze-results
description: Diagnose a Hermes backtest result — why a strategy wins or loses, its drawdowns, cost sensitivity, and overfitting/look-ahead smells. Use when the user asks why a strategy performed the way it did, wants to interpret results, or sanity-check a backtest before trusting it.
---

# Hermes: analyse results

Interrogate a `BacktestResult` for whether its edge is *real* — not just whether the line
went up. Assume optimism until proven otherwise.

## Read the semantics from the ADRs

The honest-backtest guarantees live in `docs/adr/`: fills and SL/TP-clash (0004), the
forming-bar repaint/parity rules (0002), the AI-decision cache (0005). Cite them when a
finding hinges on how the engine actually behaves.

## The rubric — work every axis, don't stop at the first

1. **Trades, not just the curve** — read the blotter. Is the P&L driven by a handful of
   outlier trades? Clustered in one regime/date range? Mostly one direction?
2. **Drawdowns** — size, duration, and *when*; is the worst one a single event or a slow
   bleed?
3. **Cost sensitivity** — re-run with the Cost Model zeroed vs default (and heavier). If
   the edge evaporates under realistic commission/spread/slippage/financing, it isn't one.
4. **Sample size / overfitting** — too few trades to trust? Many tuned parameters vs
   observations? Say so plainly.
5. **Look-ahead / repaint** — confirm the logic only used information available at
   decision time (next-open fills, forming-bar values that reflect only up-to-now). Flag
   any indicator that would resolve differently live.
6. **Benchmark** — compare to buy-and-hold and to a trivial baseline over the same window;
   beating nothing isn't an edge.
7. **Robustness (light)** — does the result survive small shifts in parameters, the date
   window, or the timeframe? Fragility here is a red flag even when the headline is good.

## Deliver

A verdict, not a metrics restatement: does the edge look real, what's the biggest threat
to it, and the one change most worth trying next — routed back to `/hermes-strategy`.

Completion criterion: every rubric axis addressed (each either a finding or an explicit
"clean"), ending in a plain-language verdict + next step.
