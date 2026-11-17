# Pessimistic fill model: quote-side triggers, limit penetration, margin at fill

Status: accepted (audit F8, F9, F14). Amends ADR-0004 and ADR-0009; ADR-0009's maker
mechanics (own price, no spread, no slippage, rebates) are unchanged.

ADR-0004 says fills are pessimistic by default. Three places were not.

## 1. Triggers are evaluated on the side of the book the order trades (F14)

Bars carry one price series. On a `BID` basis (Pepperstone/MT5) it is the bid and
`ask = bid + spread`; on a `LAST`/`MID` basis the bid and ask sit half a spread either
side. Triggers compared the bar to the level directly, which is right for orders that sell
and one spread late for orders that **buy**: a long limit entry, a short take-profit, a
short stop-loss, a buy-stop entry.

Now a buy-side order sees `bar + buy_offset` and a sell-side order sees `bar + sell_offset`,
where `(buy_offset, sell_offset)` is `(spread, 0)` on a BID basis and `(spread/2, -spread/2)`
otherwise, the spread being the one in force at the bar (schedule included). A taker fill
price is still `raw + spread`; a gap fills at the (offset) open; a maker fill is at its
level or the better offset open. With a zero spread everything is unchanged.

## 2. A resting limit needs penetration, not a touch (F8)

A limit entry or take-profit fills only when the market trades `limit_penetration_ticks`
(default 1) **through** the level, not when it merely touches it. A touch fills the queue
ahead of you, not you. `CostModel.limit_penetration_ticks = 0` restores touch-fills.
Gap-through fills are unaffected. Stops are unchanged: a stop converts on touch.

## 3. Margin is checked where the fill happens, and there is a floor (F9)

* A working order is re-checked against free margin **at its fill price**, excluding its own
  reservation; if it no longer fits it is `REJECTED` (a gap can make last night's affordable
  order unaffordable).
* After each bar, if equity (including other legs' open P&L in a portfolio) is at or below
  zero, this venue's open trades are closed at the bar close with reason `liquidation`, taker
  costs, and working orders are cancelled. `unconstrained` runs skip both.

This is deliberately a bankruptcy floor, not a broker stop-out: real brokers close at a
margin level (often 50%), earlier. That level is a broker fact we have not sourced, so it is
left as a follow-up rather than guessed.

## Consequences

Every stored run is stale for any strategy with limits, TPs, a spread, or leverage. The
run-cache key includes the engine fingerprint (audit F2), so they re-run rather than being
served. Limit/mean-reversion strategies get worse, which is the point; judge a strategy by
its post-change numbers, never by comparing across the change.
