# Hermes — Trading Framework

A Python framework for developing, backtesting, and (later) deploying **intraday and swing** trading strategies on candlestick data (not L2 / not tape). It unifies stocks (yfinance), CFDs (Pepperstone), and crypto (Binance) behind one format so a strategy can be written once and run against any source.

## Language

**Instrument**:
The tradable thing plus its metadata. Abstract base with concrete subclasses `Stock`, `CryptoPair`, `Cfd`. Carries source-specific facts (tick size, quote currency, session calendar, price basis, whether volume is real). Strategies use only its shared interface — they never branch on the concrete subtype.
_Avoid_: Asset, Ticker (as the object), Security, Product

**Symbol**:
The identifier string for an Instrument (`"AAPL"`, `"BTCUSDT"`). A value that indexes to an Instrument — never the object itself.
_Avoid_: Ticker, Code

**Bar**:
One normalized OHLCV row: UTC timestamp + open/high/low/close/volume + timeframe. Identical shape across all sources — the unit a strategy iterates over. Source differences never change the Bar's shape.
_Avoid_: Candle, Candlestick, OHLC, Row

**DataSource**:
A per-provider adapter that fetches raw data and normalizes it into `Bar`s and `Instrument`s — the market-data side of the parity seam. Concrete: `YFinanceSource`, `BinanceSource`, `PepperstoneSource` (CFDs via the **cTrader Open API**). Same interface serves historical/replay bars (backtest) and (later) live streaming bars. Owns all the messy provider-specific conversion, and a local Parquet cache (fetch-once, incremental). For cTrader, only ≤1h is fetched natively; anything above 1h is rebuilt from 1h so alignment stays under Hermes's control (see ADR-0006).
_Avoid_: Feed, Provider, Broker, Connector

**ExecutionVenue**:
The order-execution side of the parity seam: a Strategy emits Orders to an ExecutionVenue and receives fills back. `SimulatedVenue` (backtest) owns ALL fill/cost/margin/SL-TP simulation; a live broker adapter (out of scope for now) implements the same interface. The Strategy, Indicators, and Sizers are identical across backtest and live — deployment is "write one ExecutionVenue adapter," not a rewrite.
_Avoid_: Broker, Gateway, Exchange (as the interface name)

**Strategy**:
User-authored trading logic driven event-by-event. The same Strategy code runs unchanged in backtest and live — only the DataSource and execution venue swap underneath it. Fully polymorphic over Instruments. Trades a single Instrument (v1) and can subscribe to multiple Timeframes of it.
_Avoid_: Algo, Bot, Model

**Order**:
An instruction to transact: `market` (fills at next Base bar's open), `limit`, or `stop`. Limit/stop entries are **resting/working** orders — they persist across bars (GTC) until filled (their price is touched by a bar's range) or cancelled, not next-bar-only. Fills use OHLC-touch rules; gaps through a stop fill at the open (worse than the stop price).
_Avoid_: Deal, Trade (an Order is the instruction, not the resulting exposure)

**Trade**:
One open exposure resulting from a filled entry, carrying its own optional **Stop Loss** and **Take Profit** levels that are mutable after entry (move-to-breakeven, manual trailing). A Strategy may hold multiple concurrent Trades on its one Instrument (hedging-style); each is closed independently. Backtesting tracks Trades logically; a live venue adapter maps them onto the venue's own model (crypto spot / stock cash see only the net).
_Avoid_: Deal, Lot, Fill

**Position**:
The net aggregate exposure across all open Trades on an Instrument (sum of sizes, blended entry). Derived, not the primary unit — the Trade is.
_Avoid_: Holding, Exposure (as the object)

**Stop Loss / Take Profit**:
Protective exit levels attached to a Trade and monitored each Base step: if a bar's range touches the level, the Trade closes at that level. Mutable while the Trade is open. If one Base bar touches both, resolve via the Fill-resolution Timeframe if available, else assume Stop Loss first (conservative).
_Avoid_: SL/TP (spell out in prose), bracket

**Fill-resolution Timeframe (bar magnifier)**:
An optional timeframe finer than the Base Timeframe, fetched solely to sequence intrabar fills (stop/limit touches, SL-vs-TP clashes) — never exposed to strategy logic. Opt-in per backtest; falls back to conservative OHLC-touch / Stop-first rules when the fine data isn't available (e.g. yfinance 1m ≈ last 30 days).
_Avoid_: Tick data, sub-bar

**Sizer**:
The way an order's size is expressed; all Sizers resolve to the Instrument's native units (shares/coins/lots) before the Order is placed. Interchangeable forms: risk amount in cash, risk amount in % of equity (given entry→stop distance), notional/position size in cash, and size in native units/lots.
_Avoid_: Position sizing (as the object), quantity

**Account**:
The capital pool behind a Strategy: cash/equity, used vs free margin (per-Instrument leverage), and whether shorting is allowed. Rejects orders that exceed available margin; does not auto-liquidate in v1 (risk is managed via Stop Loss).
_Avoid_: Wallet, Balance (as the object)

**Cost Model**:
A pluggable per-Instrument model of trading costs with asset-class defaults, covering four components: **commission** (% for crypto, per-share/flat for stocks, per-lot for CFD; maker/taker-aware), **spread** (bid/ask applied at fill — buy@ask/sell@bid; the dominant cost for CFDs), **slippage** (fixed ticks or %), and **financing/swap** (carry for positions held past a session; a configurable rate, since it isn't in candle data). Costs are **liquidity-aware** (ADR-0009): a resting limit order (limit entry, take-profit) fills as a **maker** — maker fee (possibly a rebate), no slippage/spread; market/stop/signal fills are **takers** — taker fee + slippage. Overridable per Instrument.
_Avoid_: Fees, Commission (as the whole model)

**Corporate Action handling**:
Stock Bars are always **split-adjusted** (no fake gaps), but price *levels* are kept real so indicators see true prices. **Dividends** are modeled as cash credited/debited to the Account on the ex-date when a Trade is held through it (long credited, short debited) — mirroring how share-CFD dividends work at Pepperstone. Crypto has none.
_Avoid_: Adjusted close (as the whole policy)

**Trading Window vs Lead-in**:
A backtest's **Trading Window** is the date range you want results for. The engine auto-extends the data fetch *backwards* by the max Indicator lookback (+buffer) — the **Lead-in** — and feeds those bars into Indicators silently so they are already warm at the Trading Window's start; the first Trade can fire on day one. `on_bar` is suppressed during Lead-in (an `on_start` hook fires when trading begins). If a source can't supply enough Lead-in history, trading starts once warm and the engine warns.
_Avoid_: Warmup period (as the trading range), burn-in

**AI Advisor**:
An optional, pluggable component a Strategy can consult to **confirm or veto an already-formed candidate trade** (entry + Stop Loss + Take Profit + size). It receives a structured **text** context assembled from the strategy's current look-ahead-safe view (candlestick/OHLC windows per Timeframe, indicator values, trade params, Instrument metadata) filled into the author's prompt template, and returns a structured **Advisor Decision** (approve/veto + confidence + reason). It can only block a trade, never invent, size, or adjust one. Provider interface is pluggable with Anthropic Claude as the default (prompt caching on static parts, forced-tool structured output). Reproducibility comes from the [[advisor-decision]] cache, not from sampling settings. When the provider errors the gate **fails open** — see [[fail-open]].
_Avoid_: AI signal, LLM strategy, model (overloaded)

**Advisor Decision**:
The structured result of an AI Advisor call — approve/veto + confidence + reason — recorded per Trade in the BacktestResult for audit. In backtest it is served from a **deterministic content-addressed cache** (keyed on model id + prompt + inputs) so runs stay reproducible, cheap, and offline; the cache doubles as a record/replay fixture.
_Avoid_: Verdict, Response

**Strategy Parameter**:
A declared, tunable input of a Strategy (lookback lengths, thresholds, risk %). First-class so a backtest run is a pure function of (Parameters, data) — the basis for reproducibility and Optimization.
_Avoid_: Setting, Config, Hyperparameter

**Backtest Review**:
An AI-written diagnosis of a `BacktestResult` — is the edge real, biggest risks, overfitting/look-ahead smells, what to try next. Produced by driving **Claude Code** headlessly with the `hermes-analyze-results` skill (not a Claude API call), written to a review file and displayed in the web UI. Distinct from the per-trade [[ai-advisor]] confirm/veto — a review judges the whole run.
_Avoid_: Analysis, critique, AI report

**Optimization**:
Running the core backtest repeatedly over a space of Strategy Parameters. **Implemented** — a run is a pure function of (Parameters, data), so the optimizer sits on top of the engine without bypassing its fill/cost logic. The grid is either given explicitly or auto-discovered from each Strategy Parameter's `choices`/`bounds`. Optimization **only ever happens in-sample**: the selected parameters are then judged on data the search never saw (see [[walk-forward-analysis]]). Picking parameters on the whole sample and reporting that number is the thing this vocabulary exists to prevent.
_Avoid_: Tuning, Sweep, Backtest (optimization is many backtests)

**BacktestResult**:
The output of a backtest: the equity curve, a Trade blotter (every closed Trade with entry/exit/P&L/costs), and computed metrics (return, CAGR, Sharpe/Sortino, max drawdown, win rate, profit factor, exposure). Plotting and third-party tear-sheets (quantstats) consume it but the object itself is viz-independent.
_Avoid_: Report, Stats

**Price Basis**:
Per-Instrument metadata stating what the Bar's prices represent — bid (Pepperstone/MT5 bars are bid), last/mid (crypto, stocks). Determines how the Cost Model's spread is applied at fill. Lives on the Instrument.
_Avoid_: Quote type

**Timeframe**:
The bar interval (15m, 1h, 1D). A Strategy may subscribe to several Timeframes of its one Instrument. Every subscribed Timeframe must be an integer multiple of the Base Timeframe.
_Avoid_: Interval, Resolution, Period

**Base Timeframe**:
The finest subscribed Timeframe. It drives the clock: the engine steps one Base bar at a time, and that is when `on_bar` fires. All higher Timeframes are aggregated up from Base bars.
_Avoid_: Tick (this is not tick data)

**Forming Bar**:
The still-open current bar of a higher Timeframe, aggregated from Base bars seen so far: open = first sub-bar open, high/low = running extremes, close = most recent completed sub-bar's close, volume = running sum, `is_closed = False`. It occupies the current/last slot of its Timeframe's series and mutates on each Base step; when the Timeframe boundary is crossed it is frozen (`is_closed = True`) and appended, and a new Forming Bar takes the slot. Indicators include the Forming Bar as their latest data point and recompute every Base step (intentional "repaint"; parity-safe because it only ever reflects information available up to now).
_Avoid_: Developing bar, partial bar, incomplete candle

**Session Calendar**:
Per-Instrument trading hours/days + timezone that define higher-Timeframe boundaries and when the Instrument is tradeable. Crypto = 24/7 UTC; Stocks = exchange session (e.g. 09:30–16:00 ET); CFDs = near-24/5. Lives on the Instrument. Optional **`day_anchor`** sets the time-of-day the trading "day" rolls over for bucket anchoring (`None` = local midnight for stocks/crypto; 17:00 New York for forex/CFD, which is how their 4h/1d bars line up with TradingView — see ADR-0006).
_Avoid_: Trading hours, market hours

**Bar bucketing rule**:
Higher-Timeframe bars anchor to **wall-clock/calendar boundaries in the Instrument's exchange-local timezone** (US stock → ET hours 09:00–10:00…; Binance → UTC hours), but are **session-bounded** — a bar never spans an overnight/weekend gap. Consequence: the first bar of a session is the partial one (09:00–10:00 holds only 09:30–10:00 of data); a bar is force-closed at session end. Bars are stored/compared in UTC internally; only the bucketing boundaries are local.

## Language — running many

The v1 vocabulary above describes **one** backtest. These terms describe the
research loop built on top of it: a run is a pure function of (Parameters, data),
so anything that runs it many times composes without touching the engine.

**Reference Feed**:
An Instrument a Strategy **observes but never trades** — SPY for a market-regime filter, DXY for a currency cross. The engine steps it in lockstep with the trading clock, so its values are look-ahead-safe, and Indicators declared on it count toward the Lead-in. Still single-instrument trading (ADR-0003): the Strategy trades its one Instrument and merely reads the Reference. Indicators on a Reference default to `latest_confirmed` (closed bars only) rather than including the Forming Bar.
_Avoid_: Secondary instrument, benchmark (a benchmark is for comparison, a Reference is an input)

**Constituent Calendar**:
A point-in-time record of which Symbols belonged to an index over time, built from a historical-membership snapshot. Answers two questions: who was *ever* a member during a window, and what was each member's `membership_window`. It exists solely to kill **survivorship bias** — a universe fixed to today's index members silently deletes every company that was delisted or removed, leaving only winners.
_Avoid_: Index list, ticker list (those are point-in-*now*, which is the bug)

**Universe Backtest**:
A bias-free portfolio backtest over an index. Consults a [[constituent-calendar]] for every ever-member in the window, clips each leg to that Symbol's membership period so the Strategy never trades a stock before it joined or after it left, and delegates to a [[portfolio-backtest]] over one shared capital pool.
_Avoid_: Screener, multi-asset backtest

**Portfolio Backtest**:
Many single-Instrument backtests ("legs") sharing **one** Account, so the legs compete for the same capital and margin exactly as they would live. Distinct from running N independent backtests and adding the equity curves — that overstates capacity by assuming unlimited money.
_Avoid_: Multi-strategy, basket

**Batch Run**:
The same Strategy run across a list of Symbols as **independent** backtests, each with its own capital, summarised into a comparison table. The right tool for "does this edge generalise across instruments?"; the wrong tool for "could I actually have held all of these at once" — that's a [[portfolio-backtest]].
_Avoid_: Sweep (that is parameter space, not symbol space)

**Walk-forward Analysis**:
The honest form of [[optimization]]. The window is cut into successive in-sample/out-of-sample pairs; parameters are chosen on each IS block and scored only on the OOS block that follows, then the OOS blocks are stitched into a single equity curve. Rolling (fixed-length IS) or anchored (expanding IS). The stitched OOS result is the only number worth quoting — the IS result is, by construction, the best the search could find.
_Avoid_: Backtest (the OOS curve is the result; the IS fits are scaffolding), cross-validation (time order matters here)

**Cost Sensitivity**:
Re-running one backtest with the [[cost-model]] scaled by several multipliers (typically 0x / 1x / 2x) to see how much of the edge survives realistic and pessimistic friction. An edge that exists only at 0x costs is a data artifact, not a strategy.
_Avoid_: Slippage test (it covers all four cost components, not just slippage)

**Regime Analysis**:
Partitioning a run's Trades by the market state they were taken in (trend vs chop, high vs low volatility, benchmark up vs down) and reporting per-regime statistics. Separates "this strategy has an edge" from "this strategy was long during a bull market".
_Avoid_: Market conditions, segmentation

**Random Baseline**:
A distribution of backtests in which the [[ai-advisor]]'s confirm/veto is replaced by approving each signal with fixed probability *p*. It answers the only question that matters about a gate: does the AI's selectivity beat a coin weighted to the same approval rate? An AI gate that lands inside its own random distribution has no demonstrated skill — it is just trading less.
_Avoid_: Control group, null model

**Statistical Validation**:
The set of checks that ask whether a result is distinguishable from luck, rather than merely positive: bootstrap confidence intervals on the headline metrics, Monte Carlo reshuffling of the trade sequence, Probabilistic and Deflated Sharpe (the latter penalising the number of configurations tried), a minimum-track-record length, and a sample-quality verdict. Attaches to a [[backtestresult]] as `stat_validation`.
_Avoid_: Significance testing (too narrow), p-value

**Context Enricher**:
A pluggable source of **point-in-time** text appended to the [[ai-advisor]]'s prompt — fundamentals as reported at the time, 10-K section text, a news window. `enrich(ticker, as_of) -> str`. The `as_of` argument is the whole point: an enricher that returns today's fundamentals for a 2019 decision has silently inserted look-ahead into the backtest. Responses are cached to `.hermes_cache/pit/` per (ticker, date).
_Avoid_: Data feed, plugin, context (unqualified)

**Fail-open**:
The [[ai-advisor]]'s behaviour when its provider errors: the candidate trade is **approved** without a real decision, because the gate's only power is to block and an outage must not silently rewrite the strategy into one that never trades. Fail-open decisions are never written to the decision cache, are exempt from the confidence threshold, and are counted in the run's `llm_summary.fail_open_calls` — a non-zero count means the run's metrics describe a *less gated* strategy than the one configured, and should not be compared against a clean run.
_Avoid_: Fallback, default approve (without the audit trail those imply no record)
