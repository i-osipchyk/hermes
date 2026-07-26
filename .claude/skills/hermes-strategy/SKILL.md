---
name: hermes-strategy
description: Interview a trading idea into a runnable Hermes Strategy file plus a Backtest config.
disable-model-invocation: true
---

# Hermes: create a strategy

Turn a trading idea into a working Hermes `Strategy` through a bounded interview, then
write it to `strategies/<name>.py`. You **stop at the file** — running it is
`hermes-backtest`'s job, diagnosing it is `hermes-analyze-results`'.

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

## 2. Interview — one question at a time, recommended default first

Ask each, wait, then the next. Lead with a recommended answer so the user can accept in
a word. Cover, in order:

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

## 3. Write `strategies/<name>.py`

Create `strategies/` if absent. The file holds the `Strategy` subclass **and** a runnable
`Backtest` config under `if __name__ == "__main__":`, mirroring the example's structure so
`hermes-backtest` can execute it directly. Declare indicators/parameters in `setup`, logic
in `on_bar`, and use a `Sizer` + `stop_loss`/`take_profit` on entries. Only include the AI
Advisor if the user asked for it.

Completion criterion: the file imports only names that exist in `hermes`'s public API
(verify against `src/hermes/__init__.py`) and reflects every interview answer.

## 4. Hand off

Show the user the file and the one line to run it. Suggest **`hermes-backtest`** next —
don't run it yourself.
