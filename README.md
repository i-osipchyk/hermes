# Hermes

A Python framework for developing, backtesting, and (later) deploying **intraday and swing**
trading strategies on **candlestick data** — unifying stocks (yfinance), CFDs (Pepperstone), and
crypto (Binance) behind one format so a strategy is written once and runs against any source.

> **Not** high-frequency and **not** tick/L2/tape. Bars only.

## Design status

The domain model is **settled**. Start here before reading code:

- [`CONTEXT.md`](./CONTEXT.md) — the glossary / ubiquitous language (~27 terms).
- [`docs/adr/`](./docs/adr) — the load-bearing architectural decisions:
  1. [Event-driven engine with a two-interface parity seam](./docs/adr/0001-event-driven-engine-with-two-interface-parity-seam.md)
  2. [Multi-timeframe forming-bar model](./docs/adr/0002-multi-timeframe-forming-bar-model.md)
  3. [Single-instrument, multi-Trade scope for v1](./docs/adr/0003-single-instrument-multi-trade-scope-v1.md)
  4. [Conservative OHLC fill model with opt-in magnifier](./docs/adr/0004-conservative-ohlc-fill-model-with-opt-in-magnifier.md)
  5. [AI Advisor as a cached confirmation gate](./docs/adr/0005-ai-advisor-as-cached-confirmation-gate.md)

## Core ideas in one breath

- A **Strategy** reacts **bar-by-bar** (`on_bar`) — the *same code* runs in backtest and live.
- It sits between two swappable interfaces: a **`DataSource`** (market data in) and an
  **`ExecutionVenue`** (orders out). Backtest = replay source + `SimulatedVenue`.
- One uniform **`Bar`**; a polymorphic **`Instrument`** (`Stock` / `CryptoPair` / `Cfd`) hides every
  source-specific difference. Strategies never branch on subtype.
- **Multi-timeframe**: a Strategy sees several timeframes at once; higher ones are visible as
  **Forming Bars** and recompute each base step (parity-safe "repaint").
- **Multiple concurrent Trades** per instrument, each with a mutable Stop Loss / Take Profit.
- Honest fills (next-open, OHLC-touch, conservative SL/TP-clash) with an opt-in **bar magnifier**.
- Full **Cost Model** (commission + spread + slippage + financing) and margin-aware **Account**.
- An optional **AI Advisor** that can only *confirm or veto* an already-formed trade, with a
  deterministic response cache so backtests stay reproducible.

## Project layout

```
src/hermes/
├── core/         # Bar, Timeframe, Symbol, Instrument (+ Stock/CryptoPair/Cfd), SessionCalendar
├── data/         # DataSource ABC, Parquet cache, forming-bar aggregation, provider adapters
├── indicators/   # Indicator ABC, built-ins, library wrappers
├── strategy/     # Strategy base + lifecycle hooks, Parameters, Sizers
├── execution/    # Order, Trade, Position, Account, CostModel, ExecutionVenue, SimulatedVenue
├── ai/           # AIAdvisor, provider interface, Claude provider, response cache
└── backtest/     # Engine (the clock), BacktestResult, reporting
```

## Install (dev)

```bash
pip install -e ".[dev,yfinance,binance,ai,report]"
```

## Status

**Usable.** The core is implemented and tested (28 tests):

- ✅ Multi-timeframe **forming-bar aggregation** (wall-clock/exchange-local bucketing,
  session-bounded, partial first bars) — the keystone, with your 15m→1h scenario pinned in tests.
- ✅ Indicators (SMA/EMA/RSI/ATR/MACD/Bollinger) + a pandas-ta/TA-Lib wrapper.
- ✅ Event-driven **backtest engine**: next-open + OHLC-touch fills, resting orders,
  multiple concurrent Trades with mutable SL/TP, SL/TP-clash → Stop-first (+ opt-in magnifier),
  full Cost Model (commission/spread/slippage/financing), margin-aware Account, Sizers.
- ✅ `Lead-in ≠ Trading Window` warmup, `BacktestResult` metrics, plots + quantstats hook.
- ✅ **AI Advisor** confirm/veto gate with deterministic content-addressed cache; Claude provider.
- ✅ **Data**: Binance (public REST, no dep) and yfinance (split-only adjust) sources, Parquet cache,
  and an `InMemorySource` for offline/synthetic runs.
- ✅ **Pepperstone CFDs via cTrader**: timestamp/price decode, whole-hour offset correction, and
  17:00-NY day-anchored bucketing that rebuilds 4h/1d from 1h to match TradingView (all tested);
  the Spotware Protobuf transport is the one live-only seam.

Try it: `python examples/sma_crossover_with_ai.py`

### Known gaps / next up

- **cTrader live transport** (`_request_trendbars`) needs Spotware credentials + network — the
  decode/normalise/resample/anchoring pipeline around it is implemented and tested.
- **Dividend-as-cash** on ex-dates: yfinance surfaces the data; wiring it into the Account is a TODO.
- **Live deployment** (streaming `DataSource` + real broker `ExecutionVenue`) is deliberately out of
  scope — the seam exists, the adapters don't.
- **Optimization** (grid/walk-forward) deferred by design; the pure `(params, data) → result` core is ready.

## Using Hermes with Claude Code

The repo ships a set of [Claude Code](https://claude.com/claude-code) skills under
`.claude/skills/` — they load automatically when you open Hermes. Type **`/ask-hermes`**
for a router that explains the flow; the short version:

| Skill | Invoke | What it does |
|---|---|---|
| `ask-hermes` | `/ask-hermes` | Router — which skill fits your situation |
| `hermes-strategy` | `/hermes-strategy` | Interview a trading idea → `strategies/<name>.py` + backtest config |
| `hermes-explore-data` | automatic | Fetch + plot + analyse an instrument's candles |
| `hermes-backtest` | automatic | Run a strategy's backtest, report metrics/blotter/plots |
| `hermes-analyze-results` | automatic | Diagnose *why* a strategy wins/loses |
| `hermes-extend` | automatic | Scaffold a custom Indicator / DataSource / ExecutionVenue |

Main flow: **`/hermes-strategy` → run → analyse → iterate.** The four `automatic` skills
fire on their own in conversation; `hermes-strategy` and `ask-hermes` you invoke by name.
The skills read the repo's living docs (`CONTEXT.md`, `examples/`, ADRs), so they stay in
step with the code.

**Using the skills in another project.** Claude Code doesn't auto-load skills from
installed packages, so they're bundled in the wheel and materialised on demand:

```bash
pip install hermes
hermes install-skills          # into ./.claude/skills   (--user for ~/.claude/skills)
```

…or from Python:

```python
import hermes
hermes.install_skills()        # or hermes.install_skills(user=True)
```

This copies the five user-facing skills plus a `hermes-reference/` folder (CONTEXT.md,
ADRs, the example) the ported skills point at. `hermes-extend` stays repo-only (it targets
the library's own source). Afterwards all three consumers work in that project: **you**
(`/hermes-strategy`), **Claude Code** (the model-invoked utilities fire on their own), and
the **UI's headless review** (`hermes-analyze-results` resolves in the project's cwd).

## Web UI

A local Streamlit app for running and reviewing backtests (ADR-0008):

```bash
pip install -e ".[ui]"
hermes-ui
```

Pick a strategy from `strategies/`, tweak the pre-filled config (symbol/dates/cash), and
Run. You get an interactive **equity curve + drawdown**, **metric cards**, a **trades
table**, and a **Claude review** — an AI diagnosis of the run produced by driving *Claude
Code headlessly* (`claude -p`, reusing the `hermes-analyze-results` skill and your
subscription — no API key, no billed request), written to `.hermes_cache/reviews/` and
shown on refresh. AI-generated strategies auto-review; others review on a button. If the
headless call isn't available, the page shows a copy-paste prompt to run it by hand.
