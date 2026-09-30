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

## Start here: run the rubric, don't describe it

**Most of this rubric is a single command.**

```bash
.venv/bin/hermes analyze <run-key>            # the cheap six axes
.venv/bin/hermes analyze <run-key> --json     # same, machine-readable
.venv/bin/hermes analyze <strategy-name>      # no stored run yet? runs it first
.venv/bin/hermes analyze <key> --axes walkforward,baseline   # the expensive two
```

It computes each axis, writes `analysis.json` beside the run, and prints one line per
axis. Each axis carries a `note` (the reading) and `data` (the numbers). An axis that
couldn't run says why in `error` — report that as *not measured*, never as "clean".

Start with that, then interpret. Only reach for the primitives directly when you need
something the axes don't cover; they are all importable from `hermes`
(`python -c "import hermes; print(hermes.__all__)"` is the full inventory). Saying an
edge "may not survive costs" when one command measures it is a non-answer.

Axes, and what each answers: `trades` (is the P&L two lucky trades?), `costs` (does it
survive friction?), `oos` (does it hold out of sample?), `validation` (is it
distinguishable from luck?), `regime` (strategy, or bull market?), `gate` (was the AI
gate even in effect?), plus `walkforward` (fitted params out of sample) and `baseline`
(does the gate beat a weighted coin?) on request.

## The rubric — work every axis, don't stop at the first

1. **Trades, not just the curve** — read the blotter. Is the P&L driven by a handful of
   outlier trades? Clustered in one regime/date range? Mostly one direction?
2. **Drawdowns** — size, duration, and *when*; is the worst one a single event or a slow
   bleed? `result.metrics` carries max drawdown; the curve carries the shape.
3. **Cost sensitivity** — `cost_sensitivity(backtest, multipliers=(0.0, 1.0, 2.0))`
   returns `{multiplier: BacktestResult}`. If the edge evaporates at 1x, or only exists at
   0x, it isn't one.
4. **Sample size / overfitting** — `validate(result)` → `StatValidation`: bootstrap
   confidence intervals, Monte Carlo trade reshuffling, Probabilistic and Deflated Sharpe,
   minimum track-record length, and a sample-quality verdict. Also read
   `metrics.num_params` and `metrics.parameter_adjusted_sharpe` — a big gap between raw and
   adjusted Sharpe is an overfitting warning. Say it plainly.
5. **Look-ahead / repaint** — confirm the logic only used information available at
   decision time (next-open fills, forming-bar values that reflect only up-to-now). Flag
   any indicator that would resolve differently live. For AI-gated runs, check that any
   `ContextEnricher` is keyed on `as_of` and not fetching today's data for a past decision.
6. **Benchmark** — `result.benchmark` for buy-and-hold; `regime_analysis(...)` to separate
   "has an edge" from "was long during a bull market". Beating nothing isn't an edge.
7. **Robustness** — `WalkForward(...)` picks parameters in-sample and scores them
   out-of-sample, stitching the OOS blocks into one curve; `split_isoos` is the cheap
   version. The stitched OOS number is the only one worth quoting.
8. **Was the AI gate doing anything?** — for a run with an advisor, compare against
   `run_random_simulations(build_backtest, n=..., p=<the gate's approval rate>)`. A gate
   that lands inside its own random distribution has no demonstrated skill; it is just
   trading less. Also check `to_dict()["llm_summary"]["fail_open_calls"]` — non-zero means
   the provider errored and trades were approved without a real decision, so the run is
   *less gated* than configured and is not comparable to a clean one.

## Deliver

A verdict, not a metrics restatement: does the edge look real, what's the biggest threat
to it, and the one change most worth trying next — routed back to `/hermes-strategy`.

Completion criterion: every rubric axis addressed (each either a finding or an explicit
"clean"), ending in a plain-language verdict + next step.
