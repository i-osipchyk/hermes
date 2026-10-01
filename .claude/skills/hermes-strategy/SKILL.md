---
name: hermes-strategy
description: Turn a trading idea into a runnable Hermes Strategy file plus a Backtest config — by interview when a person is driving, or directly from an already-pinned hypothesis when called from a research loop.
---

# Hermes: create a strategy

Turn a trading idea into a working Hermes `Strategy`, then write it to
`strategies/<name>.py`.

**Two modes, and they behave differently:**

- **A person is driving** (they invoked `/hermes-strategy`, or described an idea in
  conversation): run the interview in §2, then **stop at the file**. Running it is
  `hermes-backtest`'s job; diagnosing it is `hermes-analyze-results`'. Don't run it
  yourself — they may want to change something first.
- **You are inside `hermes-research`**: the hypothesis is already pinned, so **skip the
  interview entirely**. Fill in §2's list from the hypothesis, pick defensible defaults
  for the rest, record the choices you made in the file's docstring, and return to the
  loop — which *will* run it. Do not stop, and do not ask questions the hypothesis
  already answers.

## 1. Ground yourself in the living docs

Before asking anything, read — don't reproduce from memory:

- `CONTEXT.md` — the vocabulary (Instrument/Bar/Timeframe/Forming Bar/Trade/Sizer/
  Cost Model/AI Advisor). Speak it back to the user.
- `examples/sma_crossover_with_ai.py` — the canonical Strategy shape (`setup`,
  `on_bar`, `self.use(...)`, `self.indicator_value(...)`, `self.buy(...)`, the AI gate).
- `docs/adr/0002` (forming bars) and `0004` (fills) for anything about timeframes or
  order semantics.

Completion criterion: you can name the exact API the generated file will use, from the
current code — not from this skill.

## 2. Quantify — hunt the ambiguity first

This is the step that decides whether the rest is possible. An idea arrives in English
and English hides decisions. Take the example:

> "Short when large orders hit the bid after an extension move."

Every reader nods, and nothing in it is testable. **What is an extension?** Measured from
the open, from VWAP, from yesterday's close? In percent, in ATR multiples, in bars? Over
what window — 09:40 to 10:00, or any time? On what timeframe candle? **What is a large
order?** Absolute size, a multiple of median size, a percentile of today's prints?

So before writing anything, restate the idea as **if/and statements with numbers and
units**, and surface every choice you had to invent. An idea that cannot be written that
way is not ready to test — say so rather than quietly picking values and burying them.

Two cautions that save whole days:

- **Can Hermes even see this?** Hermes is **bars only** — no order flow, no tape, no
  level 2 (`CONTEXT.md`). "Large orders hitting the bid" is not expressible; a volume
  spike on a bar is the nearest proxy, and it is a *different idea*. Say which you are
  testing. Park the original rather than silently substituting.
- **Every invented number is a parameter you will have to defend.** Prefer a value the
  idea itself implies over one you will later tune (§3).

## 2b. Interview — one question at a time, recommended default first

**Skip this entirely inside `hermes-research`** — the hypothesis is pinned; fill the list
from it and record what you defaulted.

With a person driving: ask each, wait, then the next. Lead with a recommended answer so
the user can accept in a word. Cover, in order:

1. **Instrument + source** — ticker and which `DataSource` (`BinanceSource` crypto,
   `YFinanceSource` stock, `PepperstoneSource`/cTrader CFD).
2. **Timeframes** — the base (finest) Timeframe and any higher ones the logic reads
   (higher TFs are Forming-Bar aware; every TF must be an integer multiple of the base).
3. **The edge** — one sentence: what inefficiency is this exploiting? (Trend, mean-
   reversion, breakout, carry…) This shapes every later answer.
4. **Entry trigger** — the condition on indicators/price that opens a position.
5. **Exit** — signal-based (opposite condition), protective (Stop Loss / Take Profit),
   or both.
6. **Stop & target** — how SL/TP levels are computed (fixed %, ATR multiple, structure).
7. **Sizing** — which `Sizer`: `RiskPercent`, `RiskCash`, `NotionalCash`, or `Units`.
8. **Filters** — higher-TF trend filter, session/time-of-day, volatility regime.
9. **AI gate** — attach the AI Advisor confirm/veto on entries? (Default no; mention it
   exists and is opt-in.)

Record answers in the framework's words; challenge any that fight the model (e.g. a
higher-TF filter that would need a timeframe not an integer multiple of the base).

Write the quantification into the generated file's docstring: the if/and rules, the
units, and every value you had to invent. That docstring is what makes the strategy
reviewable six months later, and it is where a reader checks whether the code matches
the idea.

## 3. Parameter discipline — fewer is safer

Before writing, count the `Parameter` declarations the strategy will need. The engine
reports `metrics.num_params` and computes `metrics.parameter_adjusted_sharpe` =
Sharpe × √((n_trades − k) / n_trades), where k = number of declared Parameters. A
strategy with 10 parameters and 30 trades has a severe penalty; one with 2 parameters
and 200 trades has almost none.

**Rules of thumb:**

- Target **≤ 3 tunable Parameters** for a first version (e.g. one lookback, one SL %, one
  TP multiplier). Every extra parameter costs statistical power.
- Prefer **structural / economic logic** over an extra parameter: if a threshold only makes
  sense as 0.5%, write `0.5` — don't expose it as a Parameter just to leave it tunable.
- When a filter or timeframe is "nice to have", leave it hard-coded at a sensible default
  and note that it *could* be promoted to a Parameter later.
- If the user insists on a high-parameter design, surface the expected trade count and
  warn explicitly: "With k=8 parameters you need ≫ 80 trades for the adjusted Sharpe to
  stay meaningful."

Add a comment block at the top of the generated file:

```python
# Parameters: <k>  (≤ 3 recommended; more reduces parameter_adjusted_sharpe)
```

## 4. Write `strategies/<name>.py`

Create `strategies/` if absent. Declare indicators/parameters in `setup`, logic in
`on_bar`, and use a `Sizer` + `stop_loss`/`take_profit` on entries. Only include the AI
Advisor if the user asked for it. The file must follow the **discovery convention** so it
drops straight into `hermes-backtest` and the web UI (`hermes-ui`):

- a module constant `GENERATED_BY = "hermes-strategy"` (provenance — the UI badges it and
  auto-runs a Claude review);
- a factory `build_backtest(**overrides) -> Backtest` that returns a fully-configured
  `Backtest` (Strategy instance, source, symbol, timeframes, starting cash) and applies any
  `overrides` (symbol/start/end/starting_cash) — build a fresh Strategy each call so re-runs
  are clean. **Omit `start`/`end`** so the run defaults to year-to-date (Jan 1 → today);
  pass them only if the strategy needs a specific window;
- `if __name__ == "__main__": build_backtest().run()` for direct CLI use.

A **universe-scale** strategy (every ever-member of an index, via `ConstituentCalendar`)
returns a `UniverseBacktest` rather than a `Backtest`, so it exposes
`build_universe_backtest(**overrides)` instead — mirror `strategies/gap_breakout_sp500.py`.

Completion criterion: the file imports only names that exist in `hermes`'s public API
(verify against `src/hermes/__init__.py`), exposes `GENERATED_BY` + `build_backtest`, and
reflects every interview answer.

## 5. Hand off

**If a person is driving:** show them the file and the one line to run it
(`.venv/bin/hermes run <name>`). Mention that after the run,
`metrics.parameter_adjusted_sharpe` penalises the Sharpe for the declared parameter
count — a low adjusted Sharpe relative to the raw Sharpe is an overfitting warning.
Suggest **`hermes-backtest`** next; don't run it yourself.

**If you are inside `hermes-research`:** report the file path, the parameter count, and
the choices you defaulted, then return to the loop. It runs the backtest.
