# Pepperstone CFDs via cTrader; rebuild timeframes above 1h from 1h

Pepperstone CFD data is sourced through the **cTrader Open API** (Spotware), not
MetaTrader5. Two cTrader realities drive the design:

1. **Non-midnight trading day.** cTrader treats the day as rolling at **17:00 New
   York**, so its native 4h/1d bars are offset from a midnight-anchored view and from
   how TradingView draws them (the "~1 hour shift" observed in practice, which is
   really the day-rollover plus EET/EEST DST). We model this with a `day_anchor` on
   the CFD's `SessionCalendar` (17:00 America/New_York), which the aggregation uses to
   anchor 4h/1d buckets — DST handled automatically by the timezone.

2. **Don't trust native higher-timeframe bars.** `CTraderSource.history` fetches only
   **≤ 1h natively** and **rebuilds every timeframe above 1h from 1h bars** using
   Hermes's own bucketing (`resample_from_hourly`). Alignment is therefore always
   under our control and consistent with the live engine, regardless of cTrader's
   native aggregation. An optional `bar_utc_offset_minutes` corrects any residual
   whole-hour shift on the raw timestamps.

## Consequences
- The timestamp-decode, offset-correction, and 1h→N resampling are pure and tested;
  only the Protobuf/TLS transport (`_request_trendbars`) needs live credentials.
- `day_anchor` is a general `SessionCalendar` field (default `None` = midnight), so
  stocks/crypto are unaffected; only forex/CFD set it.
- Matching a *specific* TradingView chart may still need the user to tune
  `day_anchor` / `bar_utc_offset_minutes` to their broker+symbol.
