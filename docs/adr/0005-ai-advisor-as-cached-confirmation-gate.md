# AI Advisor as a cached confirmation gate

Hermes includes an optional **AI Advisor** a Strategy can consult to **confirm or veto** an already-formed candidate trade (entry + Stop Loss + Take Profit + size). Deterministic strategy logic forms and sizes the trade; the AI can only *block* it — never invent, size, or adjust trades. The Advisor receives a structured **text** context (candlestick windows per Timeframe, indicator values, trade params, Instrument metadata) assembled from the strategy's current look-ahead-safe view and filled into the author's prompt template, and returns a structured decision (approve/veto + confidence + reason). The provider is a pluggable interface with **Anthropic Claude as the default** (prompt caching on static prompt parts, structured output via tool use / JSON schema, temperature 0).

In backtest, every Advisor Decision is served from a **deterministic content-addressed cache** keyed on (model id, prompt, inputs): the first run makes one real call per unique candidate, re-runs read the cache. This preserves the pure-`(params, data) → result` backtest contract despite LLM non-determinism, keeps runs cheap and offline, and doubles as a record/replay fixture for tests. Decisions are stored per Trade in the BacktestResult for audit.

## Considered and rejected
- **Live calls every run**: faithful to live behavior but non-reproducible, slow, and costs money per iteration — breaks the pure-function core.
- **AI generating or sizing trades**: the least reproducible, easiest-to-overfit use; deliberately out of scope. AI is a filter, not a source of edge.

## Consequences
- The cache must be invalidated when the model id, prompt template, or input-assembly changes (all part of the hash key).
- Backtest ≠ live can diverge if the live model version differs from the cached one; the model id is recorded so this is detectable.
