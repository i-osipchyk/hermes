# Multi-timeframe forming-bar model

A Strategy may subscribe to several Timeframes of its Instrument. The finest is the **Base Timeframe**, which drives the clock (one `on_bar` per Base bar). Higher Timeframes are aggregated up from Base bars and are visible as **Forming Bars** — the current higher-TF bar is exposed while still open (`is_closed=False`), with running high/low/volume and its close set to the most recent completed sub-bar's close. Indicators include the Forming Bar as their latest data point and **recompute every Base step** (they "repaint").

Higher-TF bars anchor to **wall-clock/calendar boundaries in the Instrument's exchange-local timezone** but are **session-bounded** — they never span overnight/weekend gaps, so the first bar of a session is partial and a bar force-closes at session end.

Chosen so intraday+swing strategies can react to developing higher-TF context (the way a live chart looks) instead of only closed bars. Critically, this is **parity-safe**: the Forming Bar only ever reflects information available up to now, and live trading computes it identically each Base step — so it is repaint-in-plotting, not look-ahead.

## Considered and rejected
- **Closed-only indicators** (no repaint): simpler and never repaints, but hides developing higher-TF structure the user explicitly wants to trade on.
- **UTC-anchored / gap-spanning buckets**: simpler math, but produces bars that don't match how stock/CFD charts actually look.

## Consequences
- The recorded indicator series has multiple intermediate values per higher-TF bar; result recording/plotting must account for this.
- Aggregation requires every subscribed Timeframe to be an integer multiple of the Base Timeframe.
