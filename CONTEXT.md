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
A per-provider adapter that fetches raw data and normalizes it into `Bar`s and `Instrument`s — the market-data side of the parity seam. Concrete: `YFinanceSource`, `BinanceSource`, `PepperstoneSource`. Same interface serves historical/replay bars (backtest) and (later) live streaming bars. Owns all the messy provider-specific conversion, and a local Parquet cache (fetch-once, incremental).
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
A pluggable per-Instrument model of trading costs with asset-class defaults, covering four components: **commission** (% for crypto, per-share/flat for stocks, per-lot for CFD), **spread** (bid/ask applied at fill — buy@ask/sell@bid; the dominant cost for CFDs), **slippage** (fixed ticks or %), and **financing/swap** (carry for positions held past a session; a configurable rate, since it isn't in candle data). Overridable per Instrument.
_Avoid_: Fees, Commission (as the whole model)

**Corporate Action handling**:
Stock Bars are always **split-adjusted** (no fake gaps), but price *levels* are kept real so indicators see true prices. **Dividends** are modeled as cash credited/debited to the Account on the ex-date when a Trade is held through it (long credited, short debited) — mirroring how share-CFD dividends work at Pepperstone. Crypto has none.
_Avoid_: Adjusted close (as the whole policy)

**Trading Window vs Lead-in**:
A backtest's **Trading Window** is the date range you want results for. The engine auto-extends the data fetch *backwards* by the max Indicator lookback (+buffer) — the **Lead-in** — and feeds those bars into Indicators silently so they are already warm at the Trading Window's start; the first Trade can fire on day one. `on_bar` is suppressed during Lead-in (an `on_start` hook fires when trading begins). If a source can't supply enough Lead-in history, trading starts once warm and the engine warns.
_Avoid_: Warmup period (as the trading range), burn-in

**AI Advisor**:
An optional, pluggable component a Strategy can consult to **confirm or veto an already-formed candidate trade** (entry + Stop Loss + Take Profit + size). It receives a structured **text** context assembled from the strategy's current look-ahead-safe view (candlestick/OHLC windows per Timeframe, indicator values, trade params, Instrument metadata) filled into the author's prompt template, and returns a structured **Advisor Decision** (approve/veto + confidence + reason). It can only block a trade, never invent, size, or adjust one. Provider interface is pluggable with Anthropic Claude as the default (prompt caching on static parts, structured output, temperature 0).
_Avoid_: AI signal, LLM strategy, model (overloaded)

**Advisor Decision**:
The structured result of an AI Advisor call — approve/veto + confidence + reason — recorded per Trade in the BacktestResult for audit. In backtest it is served from a **deterministic content-addressed cache** (keyed on model id + prompt + inputs) so runs stay reproducible, cheap, and offline; the cache doubles as a record/replay fixture.
_Avoid_: Verdict, Response

**Strategy Parameter**:
A declared, tunable input of a Strategy (lookback lengths, thresholds, risk %). First-class so a backtest run is a pure function of (Parameters, data) — the basis for reproducibility and Optimization.
_Avoid_: Setting, Config, Hyperparameter

**Optimization**:
Running the core backtest repeatedly over a space of Strategy Parameters (grid search, walk-forward). **Deferred from v1** but the design stays ready for it: a backtest run is a pure function of (Parameters, data), so an optimizer can later sit on top of the engine without bypassing its fill/cost logic.
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
Per-Instrument trading hours/days + timezone that define higher-Timeframe boundaries and when the Instrument is tradeable. Crypto = 24/7 UTC; Stocks = exchange session (e.g. 09:30–16:00 ET); CFDs = near-24/5. Lives on the Instrument.
_Avoid_: Trading hours, market hours

**Bar bucketing rule**:
Higher-Timeframe bars anchor to **wall-clock/calendar boundaries in the Instrument's exchange-local timezone** (US stock → ET hours 09:00–10:00…; Binance → UTC hours), but are **session-bounded** — a bar never spans an overnight/weekend gap. Consequence: the first bar of a session is the partial one (09:00–10:00 holds only 09:30–10:00 of data); a bar is force-closed at session end. Bars are stored/compared in UTC internally; only the bucketing boundaries are local.
