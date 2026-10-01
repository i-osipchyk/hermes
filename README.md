# Hermes

A Python framework for developing, backtesting, and (later) deploying **intraday and swing**
trading strategies on **candlestick data** — unifying stocks (yfinance/Tiingo), CFDs
(Pepperstone via cTrader), and crypto (Binance spot + futures) behind one format so a strategy
is written once and runs against any source.

> **Not** high-frequency and **not** tick/L2/tape. Bars only.

Hermes is built to be driven by an **AI research agent** as much as by a person: the library is
a set of composable primitives, the vocabulary is written down, and the honest-backtest rules
are recorded as decisions rather than folklore. See [Using Hermes with Claude Code](#using-hermes-with-claude-code).

## The process

The repo is organised around an explicit five-step research process (ADR-0011), because an
agent with good tools and no process optimises for the wrong thing — given a backtest and a
free hand it will find a number that looks good, since finding numbers is what search does.
Each step is backed by something computed rather than suggested:

| Step | What it is | Tooling |
|---|---|---|
| **1 Idea generation** | Books, discretionary screen time, other traders, the data | `hermes ideas` — a committed backlog with provenance and a per-source hit rate |
| **2 Quantification** | English → unambiguous if/and rules, with units | `hermes-strategy` — hunt the ambiguity, name every invented number |
| **3 Testing** | Historical → robustness → out-of-sample → forward | `hermes analyze` — evidence tiers, parameter sensitivity, Monte Carlo, costs, walk-forward |
| **4 Portfolio** | Combine *uncorrelated* strategies | `hermes correlate` — the matrix and the diversification ratio |
| **5 Repeat** | Name the gap, go back to step 1 | `hermes-portfolio` + `hermes ideas --open` |

The last rung of step 3, **forward testing** on paper or minimum size, is outside Hermes —
there is no live venue (ADR-0001: the seam exists, the adapter doesn't). A strategy that
survives the whole rubric is *ready to forward test*, never "deployable".

## Design status

**New here? Read [`docs/MANUAL.md`](./docs/MANUAL.md)** — how to run all five stages,
with an AI agent driving and entirely by hand, with every command and the Python
equivalent.

The domain model is **settled**. Start here before reading code:

- [`docs/MANUAL.md`](./docs/MANUAL.md) — the user manual: the five stages, end to end.
- [`CONTEXT.md`](./CONTEXT.md) — the glossary / ubiquitous language (~40 terms). Two sections:
  the v1 vocabulary for **one** backtest, then the terms for **running many** (universe,
  walk-forward, cost sensitivity, random baseline, statistical validation).
- [`docs/adr/`](./docs/adr) — the load-bearing architectural decisions:
  1. [Event-driven engine with a two-interface parity seam](./docs/adr/0001-event-driven-engine-with-two-interface-parity-seam.md)
  2. [Multi-timeframe forming-bar model](./docs/adr/0002-multi-timeframe-forming-bar-model.md)
  3. [Single-instrument, multi-Trade scope for v1](./docs/adr/0003-single-instrument-multi-trade-scope-v1.md)
  4. [Conservative OHLC fill model with opt-in magnifier](./docs/adr/0004-conservative-ohlc-fill-model-with-opt-in-magnifier.md)
  5. [AI Advisor as a cached confirmation gate](./docs/adr/0005-ai-advisor-as-cached-confirmation-gate.md)
  6. [Pepperstone via cTrader: rebuild above 1h](./docs/adr/0006-pepperstone-via-ctrader-rebuild-above-1h.md)
  7. [Companion Claude Code skills in-repo](./docs/adr/0007-companion-claude-code-skills-in-repo.md)
  8. [Backtesting web UI with Claude Code review](./docs/adr/0008-backtesting-web-ui-with-claude-code-review.md)
  9. [Liquidity-aware costs (maker/taker)](./docs/adr/0009-liquidity-aware-costs.md)
  10. [Agent-first research surface](./docs/adr/0010-agent-first-research-surface.md)
  11. [The five-step research process](./docs/adr/0011-five-step-research-process.md) — the current direction

## Core ideas in one breath

- A **Strategy** reacts **bar-by-bar** (`on_bar`) — the *same code* runs in backtest and live.
- It sits between two swappable interfaces: a **`DataSource`** (market data in) and an
  **`ExecutionVenue`** (orders out). Backtest = replay source + `SimulatedVenue`.
- One uniform **`Bar`**; a polymorphic **`Instrument`** (`Stock` / `CryptoPair` /
  `CryptoPerpetual` / `Cfd`) hides every source-specific difference. Strategies never branch
  on subtype.
- **Multi-timeframe**: a Strategy sees several timeframes at once; higher ones are visible as
  **Forming Bars** and recompute each base step (parity-safe "repaint").
- **Multiple concurrent Trades** per instrument, each with a mutable Stop Loss / Take Profit.
- Honest fills (next-open, OHLC-touch, conservative SL/TP-clash) with an opt-in **bar magnifier**.
- Full **Cost Model** (commission + spread + slippage + financing), **liquidity-aware**
  (maker vs taker), and a margin-aware **Account**.
- A run is a pure function of **(Parameters, data, config)** — which is what lets everything in
  *Running many* below compose without touching the engine.
- An optional **AI Advisor** that can only *confirm or veto* an already-formed trade, with a
  deterministic response cache so backtests stay reproducible.

## Project layout

```
src/hermes/
├── core/         # Bar, Timeframe, Symbol, Instrument (+ Stock/CryptoPair/Cfd), SessionCalendar
├── data/         # DataSource ABC, Parquet cache, forming-bar aggregation, provider adapters,
│                 #   ConstituentCalendar (point-in-time index membership)
├── indicators/   # Indicator ABC, built-ins, library wrappers
├── strategy/     # Strategy base + lifecycle hooks, Parameters, Sizers, Reference feeds
├── execution/    # Order, Trade, Position, Account, CostModel, ExecutionVenue, SimulatedVenue
├── ai/           # AIAdvisor, provider interface, Claude/DeepSeek providers, response cache,
│                 #   point-in-time ContextEnrichers, LLM observability
├── backtest/     # Engine (the clock), BacktestResult, the research primitives, reporting
├── research/     # The loop: the idea backlog, strategy discovery, the run ledger,
│                 #   the rubric, the review
├── cli.py        # `hermes` — the loop as commands, with --json everywhere
└── webui/        # Local Streamlit app over the same primitives
```

Everything public is re-exported from the top level, so
`python -c "import hermes; print(hermes.__all__)"` is a complete inventory (156 names), not a
subset. A test enforces that (`tests/test_public_api.py`).

## Install (dev)

```bash
pip install -e ".[dev,yfinance,binance,ai,report,ui]"
```

Extras: `yfinance`, `binance`, `pepperstone`, `ai` (Anthropic — the default advisor provider),
`deepseek` (OpenAI-compatible client), `indicators` (pandas-ta), `report`
(matplotlib/quantstats), `ui` (Streamlit/plotly), `dev`.

Behind a **corporate TLS proxy** (self-signed root in the chain)? Data fetches trust the
OS certificate store automatically via `truststore` — no `SSL_CERT_FILE` needed. Opt out
with `HERMES_NO_TRUSTSTORE=1`.

Local caches all live under one root, `.hermes_cache/` — `bars/` (Parquet), `ai/` (advisor
decisions), `pit/` (point-in-time enricher responses), `runs/`, `reviews/`,
`random_baselines/`. Delete the root to start cold; nothing in it is ever committed.

## Status

**Usable.** 337 tests, fully offline, ~2.8s.

### One backtest

- ✅ Multi-timeframe **forming-bar aggregation** (wall-clock/exchange-local bucketing,
  session-bounded, partial first bars) — the keystone, with the 15m→1h scenario pinned in tests.
- ✅ Indicators: SMA/EMA/RSI/ATR/MACD/Bollinger/ADX/Fractals/FairValueGap + a pandas-ta/TA-Lib
  wrapper.
- ✅ Event-driven **backtest engine**: next-open + OHLC-touch fills, resting orders,
  multiple concurrent Trades with mutable SL/TP, SL/TP-clash → Stop-first (+ opt-in magnifier),
  full liquidity-aware Cost Model, margin-aware Account, Sizers (incl. `LeveragedFraction`).
- ✅ `Lead-in ≠ Trading Window` warmup, `BacktestResult` metrics, plots + quantstats hook.
- ✅ **Reference feeds** — observe an instrument (e.g. SPY for a regime filter) without
  trading it, stepped in lockstep so it stays look-ahead-safe.

### Running many — the research primitives

All exported from `hermes` directly:

| What you want to know | Primitive |
|---|---|
| Does it generalise across instruments? | `run_batch` → `BatchResult` |
| Could I have held all of these at once? | `PortfolioBacktest` (one shared Account) |
| Does it survive survivorship bias? | `UniverseBacktest` + `ConstituentCalendar` |
| Do the parameters hold out of sample? | `WalkForward`, `split_isoos` |
| Does the edge survive real friction? | `cost_sensitivity` |
| Was it the strategy or the bull market? | `regime_analysis` |
| Is it distinguishable from luck? | `validate` → `StatValidation` (bootstrap CIs, Monte Carlo, PSR/DSR, sample quality) |
| Does the AI gate beat a weighted coin? | `run_random_simulations` |
| Is the result a plateau or a lucky spike? | `param_sensitivity` |
| Are these strategies the same bet? | `correlate_curves` / `correlate_results` |

### AI

- ✅ **AI Advisor** confirm/veto gate with a deterministic content-addressed cache; Claude
  (default) and DeepSeek providers; per-call observability with token/cost accounting and a
  **fail-open count** so a run whose gate silently errored is detectable.
- ✅ **Point-in-time Context Enrichers** — fundamentals as reported, 10-K sections, news
  windows — keyed on `(ticker, as_of)` so they cannot leak the future into a decision.

### Data

- ✅ Binance spot + futures (public REST, no dep), yfinance (split-only adjust), Tiingo,
  Parquet cache with gap-aware incremental fetch, and an `InMemorySource` for offline runs.
- ✅ **Pepperstone CFDs via cTrader**: timestamp/price decode, whole-hour offset correction,
  and 17:00-NY day-anchored bucketing that rebuilds 4h/1d from 1h to match TradingView (all
  tested); the Spotware Protobuf transport is the one live-only seam.

Try it: `python examples/sma_crossover_with_ai.py`

## The `hermes` CLI — the research loop as commands

The point of the pivot (ADR-0010): an agent shouldn't have to write a script per
question. Every command takes `--json` and puts **one** JSON document on stdout, with
progress on stderr — so piping is always safe. Exit codes: `0` fine, `2` ran but
produced nothing usable (e.g. zero trades), `1` bad request.

```bash
hermes strategies                 # what can I run? (broken files listed with their error)
hermes run ema_crossover --symbol AAPL --start 2021-01-01 -p fast=10
hermes runs                       # what have I already tried?
hermes show 35fc --trades         # a stored run (key prefixes work)
hermes analyze 35fc               # execute the rubric; writes analysis.json
hermes review 35fc                # a written verdict over that evidence
```

A zero-trade run exits `2` and diagnoses itself rather than listing possibilities —
`coverage` reports the bars actually stepped, and `zero_trade_diagnosis` separates "no
data reached the strategy" from "every signal was vetoed" from "the entry condition was
never true", which need completely different fixes.

**Runs are recorded and input-keyed.** The key hashes the strategy's *file contents*
plus symbol, window, parameters and sizer, so re-running an identical configuration is
free (`"from_cache": true`) and editing the strategy invalidates its old runs rather
than serving a result the current code wouldn't produce. That makes `hermes runs` a real
experiment log — the memory that lets a research agent resume instead of re-deriving.

### `hermes analyze` — the rubric, computed

The nine axes, cheapest first. The first seven run by default; the last two are opt-in
because they cost many backtests.

| Axis | Question |
|---|---|
| `trades` | Is the P&L an edge, or two lucky trades? |
| `costs` | Does it survive 1x and 2x friction? |
| `params` | Is the result a plateau, or a spike the search found? |
| `oos` | Does it hold on data the parameters didn't see? |
| `validation` | Is it distinguishable from luck, and how much evidence is there? (CIs, Monte Carlo, PSR/DSR, **evidence tier**) |
| `regime` | The strategy, or being long in a bull market? |
| `gate` | Was the AI gate actually in effect, or did it fail open? |
| `walkforward` | Do *fitted* parameters hold out of sample? |
| `baseline` | Does the AI gate beat a coin weighted to the same approval rate? |

An axis that can't run reports `ran: false` with the reason rather than failing the
whole analysis — a partial answer beats an exception. Real output, on a run whose
headline looked healthy (24.7% return, Sharpe 0.54):

```
  ✓ trades       the top 5 trades are 100% of all winning P&L; every trade is buy
  ✓ costs        edge survives 1x and 2x costs
  ✓ oos          in-sample Sharpe 0.39 does not carry out of sample (-0.40)
  ✓ validation   Only 13 trades — need ≥ 30…; deflated Sharpe 82% once the search is accounted for
  ✓ regime       all profit comes from Bull regimes (bull 28,573 vs bear -3,880)
  · gate         no AI advisor on this run
```

### `hermes review` — interpretation, not calculation

`review` hands `result.json` **and** `analysis.json` to Claude Code running headlessly
(`claude -p`, your subscription — no API key, no billed request) and asks for a verdict.
It deliberately runs **without Bash**: Hermes computes every number, the reviewer only
reads and judges them. An LLM shelling out to work out whether an edge survives costs
would be slower, unauditable, and non-reproducible. If no `analysis.json` is attached,
the prompt tells the reviewer the axes are *unmeasured* and names the command that would
measure them — so a review can never quietly imply it checked something it didn't.

### Known gaps / next up

- **Universe runs are not in the CLI yet.** `hermes run` covers single-symbol backtests;
  `UniverseBacktest` / `PortfolioBacktest` runs are still library- or UI-only. Discovery
  recognises `build_backtest` but not yet `build_universe_backtest`.
- **cTrader live transport** (`_request_trendbars`) needs Spotware credentials + network — the
  decode/normalise/resample/anchoring pipeline around it is implemented and tested.
- **Dividend-as-cash** on ex-dates: yfinance surfaces the data; wiring it into the Account is a TODO.
- **Live deployment** (streaming `DataSource` + real broker `ExecutionVenue`) is deliberately out of
  scope — the seam exists, the adapters don't.
- **Typing**: `mypy` is configured `strict` over `src/hermes` (the Streamlit layer is exempt)
  but is **not yet green** — ~340 pre-existing errors. The enforced gates are `pytest` and `ruff check .`. See [`CLAUDE.md`](./CLAUDE.md).

## Using Hermes with Claude Code

The repo ships a set of [Claude Code](https://claude.com/claude-code) skills under
`.claude/skills/` — they load automatically when you open Hermes. Type **`/ask-hermes`**
for a router that explains the flow; the short version:

| Skill | Invoke | What it does |
|---|---|---|
| `hermes-research` | automatic | **Owns the whole loop** — hypothesis → variants → run → analyse → iterate → verdict |
| `ask-hermes` | `/ask-hermes` | Router — which skill fits your situation |
| `hermes-strategy` | automatic or `/hermes-strategy` | Turn an idea into `strategies/<name>.py` + backtest config |
| `hermes-explore-data` | automatic | Fetch + plot + analyse an instrument's candles |
| `hermes-backtest` | automatic | Run a strategy's backtest, report metrics/blotter/plots |
| `hermes-analyze-results` | automatic | Diagnose *why* a strategy wins/loses |
| `hermes-extend` | automatic | Scaffold a custom Indicator / DataSource / ExecutionVenue |

**Two ways to work.** Describe an idea and ask for it to be *investigated* and
`hermes-research` takes the whole loop — it declares an iteration budget, checks the
ledger for work already done, writes and runs variants, judges each against the rubric,
iterates only on what the analysis points at, and stops on an explicit rule (the rubric
holds, no out-of-sample improvement in two iterations, or the parameter budget is spent).
It asks first before spending money (an AI Advisor, paid data) or changing the idea.
Or walk the hops yourself: **`/hermes-strategy` → run → analyse → iterate**.
The skills read the repo's living docs (`CONTEXT.md`, `examples/`, ADRs), so they stay in
step with the code — which is why those docs are treated as source, not commentary.

[`CLAUDE.md`](./CLAUDE.md) records the repo conventions an agent needs (which gates are
authoritative, the strategy-file contract, what never to commit).

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
the library's own source).

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
shown on refresh. Runs are cached by input hash in `.hermes_cache/runs/`, so re-opening a
previous configuration is instant. A strategy file that fails to import is reported inline
rather than taking the whole picker down.

> Per ADR-0010 the UI is becoming one of several front-ends over the same library
> primitives, not the only place some of them exist.

## Strategy files

`strategies/*.py` follow a discovery contract so they drop straight into the UI, the skills,
and (soon) the CLI:

- `GENERATED_BY = "hermes-strategy"` — provenance marker, when written by the skill;
- `build_backtest(**overrides) -> Backtest` — a factory returning a fully-configured run,
  building a **fresh** Strategy each call so re-runs are clean;
- `if __name__ == "__main__":` for direct CLI use.

A universe-scale run returns a `UniverseBacktest` rather than a `Backtest`, so it exposes
`build_universe_backtest(**overrides)` instead — see `strategies/gap_breakout_sp500.py`.
