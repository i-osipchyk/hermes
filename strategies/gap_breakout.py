"""Daily gap-breakout strategy (stocks, long only).

Edge: swing highs (n bars each side) confirmed within the past year act as
resistance levels. When the next candle opens AND closes above any such level —
in a gap — with volume at least ``vol_mult``× the 10-day average, the breakout
is treated as real and an entry is queued for the following day's open.

Optional trend filter: if ``ema_period`` > 0 the signal is suppressed unless
the breakout candle closes above the EMA of that period.

Parameters: 7
"""

from __future__ import annotations

from datetime import timedelta

from hermes import (
    ATR,
    Backtest,
    EMA,
    Parameter,
    RiskPercent,
    Strategy,
    Symbol,
    Timeframe,
)
from hermes.execution import CostModel
from hermes.data import YFinanceSource

GENERATED_BY = "hermes-strategy"

D1 = Timeframe.parse("1D")

_VOL_LB = 10
_SWING_LOOKBACK_DAYS = 365

COST_MODEL = CostModel.etoro_stock()


class GapBreakout(Strategy):
    def setup(self) -> None:
        self._atr_sl_mult = self.param(
            Parameter(
                "atr_sl_mult", 1.5, bounds=(0.5, 3.0),
                description="ATR(14) multiplier for the Stop Loss distance",
            )
        )
        self._tp_sl_ratio = self.param(
            Parameter(
                "tp_sl_ratio", 2.0, bounds=(1.0, 4.0),
                description="Take Profit as a multiple of the Stop Loss distance",
            )
        )
        self._vol_mult = self.param(
            Parameter(
                "vol_mult", 1.5, bounds=(1.0, 5.0),
                description="Minimum volume on the breakout candle as a multiple of the 10-day average",
            )
        )
        self._swing_n = self.param(
            Parameter(
                "swing_n", 2, bounds=(1, 10),
                description="Bars each side required to qualify a swing high",
            )
        )
        self._ema_period = self.param(
            Parameter(
                "ema_period", 0, bounds=(0, 500),
                description="EMA period for the trend filter; 0 disables the filter",
            )
        )
        self._min_gap_pct = self.param(
            Parameter(
                "min_gap_pct", 0.05, bounds=(0.0, 0.30),
                description="Minimum gap size as a fraction of prev close (0 = no filter)",
            )
        )
        self._atr = self.use(ATR(D1, 14))
        ema_p = int(self._ema_period)
        self._ema: EMA | None = self.use(EMA(D1, ema_p)) if ema_p > 0 else None
        self._swing_highs: list[tuple] = []
        self._seen_swing_ts: set = set()
        self._pending_atr: float | None = None

    def on_start(self) -> None:
        bars = self.data(D1).closed()
        n = int(self._swing_n)
        for i in range(n, len(bars) - n):
            self._try_add_swing(bars, i)

    def _try_add_swing(self, bars: list, i: int) -> None:
        cand = bars[i]
        ts = cand.timestamp
        if ts in self._seen_swing_ts:
            return
        n = int(self._swing_n)
        if (
            all(bars[i - k].high < cand.high for k in range(1, n + 1))
            and all(bars[i + k].high < cand.high for k in range(1, n + 1))
        ):
            self._swing_highs.append((ts, cand.high))
            self._seen_swing_ts.add(ts)

    def on_bar(self, bar) -> None:
        bars = self.data(D1).closed()
        n = int(self._swing_n)

        if len(bars) < max(2 * n + 1 + 2, _VOL_LB + 1):
            return

        atr = self.indicator_value(self._atr)["value"]
        if atr is None:
            return

        # Adjust SL/TP to actual fill price (deferred from previous bar).
        if self._pending_atr is not None:
            trades = self.venue.open_trades()
            if trades:
                trade = trades[0]
                sl_dist = self._atr_sl_mult * self._pending_atr
                self.modify(
                    trade,
                    stop_loss=trade.entry_price - sl_dist,
                    take_profit=trade.entry_price + self._tp_sl_ratio * sl_dist,
                )
            self._pending_atr = None

        self._try_add_swing(bars, len(bars) - (n + 1))

        cutoff = bar.timestamp - timedelta(days=_SWING_LOOKBACK_DAYS)
        self._swing_highs = [(ts, lvl) for ts, lvl in self._swing_highs if ts >= cutoff]

        if not self._swing_highs:
            return

        prev_bar = bars[-2]
        cur_bar  = bars[-1]

        def _gap_met(lvl: float) -> bool:
            if prev_bar.close <= 0:
                return False
            if cur_bar.open / prev_bar.close > 3:
                return False  # likely a corporate action
            if self._min_gap_pct > 0 and (cur_bar.open - prev_bar.close) / prev_bar.close < self._min_gap_pct:
                return False
            return (prev_bar.close < lvl
                    and cur_bar.open  > lvl
                    and cur_bar.close > lvl
                    and cur_bar.close > cur_bar.open)

        # Remove levels breached organically (no gap).
        broken = {ts for ts, lvl in self._swing_highs
                  if cur_bar.close > lvl and not _gap_met(lvl)}
        if broken:
            self._swing_highs = [(ts, lvl) for ts, lvl in self._swing_highs
                                  if ts not in broken]
            self._seen_swing_ts -= broken

        if not self.venue.position().is_flat:
            return

        avg_vol = sum(b.volume for b in bars[-(_VOL_LB + 1):-1]) / _VOL_LB
        if avg_vol <= 0 or cur_bar.volume < self._vol_mult * avg_vol:
            return

        triggered = [(ts, lvl) for ts, lvl in self._swing_highs if _gap_met(lvl)]
        if not triggered:
            return

        if self._ema is not None:
            ema_val = self.indicator_value(self._ema)["value"]
            if ema_val is None or cur_bar.close <= ema_val:
                return

        sl_dist  = self._atr_sl_mult * atr
        self.buy(
            self.sizer or RiskPercent(0.01),
            stop_loss=cur_bar.close - sl_dist,
            take_profit=cur_bar.close + self._tp_sl_ratio * sl_dist,
            tag="gap_breakout",
        )
        self._pending_atr = atr

        triggered_ts = {ts for ts, _ in triggered}
        self._swing_highs = [(ts, lvl) for ts, lvl in self._swing_highs
                              if ts not in triggered_ts]
        self._seen_swing_ts -= triggered_ts


def build_backtest(**overrides) -> Backtest:
    """Factory used by hermes-backtest and the web UI."""
    symbol = overrides.pop("symbol", Symbol("AAPL", "yfinance"))
    starting_cash = overrides.pop("starting_cash", 100_000)
    return Backtest(
        strategy=GapBreakout(),
        source=YFinanceSource(),
        symbol=symbol,
        timeframes=[D1],
        starting_cash=starting_cash,
        cost_model=COST_MODEL,
        **overrides,
    )


if __name__ == "__main__":
    result = build_backtest().run()
    m = result.metrics
    print(
        f"trades={m.num_trades}  return={m.total_return:.2%}  "
        f"sharpe={m.sharpe:.2f}  adj_sharpe={m.parameter_adjusted_sharpe:.2f}  "
        f"max_dd={m.max_drawdown:.2%}  win_rate={m.win_rate:.2%}"
    )
