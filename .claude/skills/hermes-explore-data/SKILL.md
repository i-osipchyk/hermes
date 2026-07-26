---
name: hermes-explore-data
description: Fetch and analyse candlestick data for an instrument with Hermes. Use when the user wants to see, plot, or understand a symbol's price history, volatility, or session behaviour — often before building a strategy.
---

# Hermes: explore data

Pull candles through a Hermes `DataSource` and turn them into a picture the user can
reason about.

## Read the API from the repo, not memory

Check `examples/` and `src/hermes/data/` for the current `DataSource` usage and
`Timeframe`/`Symbol` construction. Use the project's virtualenv (`.venv/bin/python`) so
installed extras (yfinance, pyarrow, matplotlib) are available.

## Steps

1. **Pin the request** — source (`BinanceSource`/`YFinanceSource`/`PepperstoneSource`),
   symbol, timeframe, and date range. If the user was vague, pick a sensible default
   (e.g. 1h over the last 90 days) and say so.
2. **Fetch** via the source's `history(...)`, which caches to Parquet — a re-run is cheap.
3. **Analyse and show** what fits the question; by default:
   - an OHLC/close plot over the range;
   - return + volatility summary (mean/σ of bar returns, annualised; ATR);
   - session/coverage sanity — bar count vs expected, gaps, weekend/overnight breaks
     (especially for stocks/CFDs, where the Forming-Bar bucketing matters);
   - optional indicator overlays (SMA/EMA/RSI/Bollinger from `hermes.indicators`) if the
     user names any.
4. **Summarise** in plain language: what the data looks like and anything that would bite
   a strategy (thin history, illiquid gaps, limited fine-timeframe range for yfinance).

Completion criterion: the user has a plot and a short written read of the instrument's
behaviour over the window — not just a raw dataframe dump.
