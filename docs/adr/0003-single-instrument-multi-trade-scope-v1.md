# Single-instrument, multi-Trade scope for v1

A Strategy trades **one Instrument** in v1, with its own Account. On that Instrument it may hold **multiple concurrent Trades** (hedging-style, MT5-like), each with its own mutable Stop Loss / Take Profit, closed independently; the net Position is their sum. Cross-instrument strategies (baskets, pairs, rotation) and a shared multi-instrument Portfolio are explicitly out of scope for v1.

Chosen for a focused, correct v1. The risk — noted deliberately — is that going multi-instrument later is close to a rewrite if the public types bake in "exactly one instrument." Mitigation: keep `Strategy`, `Account`, and `ExecutionVenue` interfaces from *assuming* the instrument count in their signatures, so a universe can be added as an extension rather than a breaking change.

## Consequences
- Multi-Trade accounting (per-Trade P&L, aggregate exposure/margin) is needed even in v1.
- Live venues that are natively netting (crypto spot, stock cash) must map Hermes's logical Trades onto their net model.
