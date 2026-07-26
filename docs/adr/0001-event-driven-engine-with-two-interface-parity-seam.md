# Event-driven engine with a two-interface parity seam

Strategies are driven **event-by-event, bar-by-bar** (`on_bar`), not vectorized, so the *same* Strategy code runs unchanged in backtest and live. A Strategy sits between two abstract interfaces: a **DataSource** (market data in) and an **ExecutionVenue** (orders out). Backtest uses a historical/replay DataSource plus a `SimulatedVenue` that owns all fill/cost/margin/SL-TP simulation; live (out of scope for now) swaps in a streaming DataSource and a real broker ExecutionVenue implementing the same interfaces.

Chosen because "easily deployable" is a core goal: a vectorized engine would be faster to backtest but cannot run live without a rewrite. For intraday/swing (non-HFT) speeds, event-driven is fast enough, and the parity seam turns deployment into "write one ExecutionVenue adapter" rather than a reimplementation.

## Consequences
- Backtests are slower than vectorized; acceptable given the timeframes.
- No backtest-only shortcut may leak into Strategy code, or parity breaks.
