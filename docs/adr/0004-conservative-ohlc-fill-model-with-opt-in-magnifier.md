# Conservative OHLC fill model with opt-in bar magnifier

Because the engine sees only OHLC bars (no intrabar tick path), fills use explicit, conservative rules: **market orders fill at the next Base bar's open**; limit/stop entries are resting (GTC) orders that fill when a bar's range touches their price (gaps through a stop fill at the open, worse than the stop); when one bar touches both a Trade's Stop Loss and Take Profit, assume **Stop Loss first**.

For higher fidelity, an author may opt into a **Fill-resolution Timeframe (bar magnifier)** — a timeframe finer than the Base, fetched solely to sequence intrabar fills and never exposed to strategy logic. It falls back to the conservative rules when fine data isn't available (e.g. yfinance 1m ≈ last 30 days).

Chosen to make backtests honest by default (pessimistic assumptions beat unverifiable optimistic ones) while giving precision where data allows. Next-bar-open fills specifically eliminate the classic look-ahead bias of "filling at the close you just used to decide."

## Consequences
- Default backtests are deterministic and slightly pessimistic.
- Fill realism varies by how much fine history each source provides; the magnifier's effect is source-dependent.
