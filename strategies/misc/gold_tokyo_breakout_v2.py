# Parameters: 2  (width_ratio, stop_atr)
#
# Gold Tokyo-range breakout v2 (idea 0945d084; research/tokyo/tokyo_filters.py, yardstick B).
#
#   Range:   high/low of the 00:00-09:00 UTC bars (TradingView's Tokyo session), weekdays.
#   Filter:  trade only when today's range is >= width_ratio x the median of the previous 20
#            Tokyo ranges (needs >= 15). Self-normalising: no ATR threshold to drift with the
#            volatility regime. 1.31 = top fifth of 2018-22 days, chosen there, not tuned here.
#   Entry:   buy stop 0.02 x ATR above the range high / sell stop below the low, 09:00-20:00 UTC;
#            each side at most once a day; one position at a time.
#   Exit:    stop stop_atr x ATR(4h,20) from entry; no target; flat at the 20:55 UTC bar.
#
# Differences from v1 (gold_tokyo_breakout): 00-09 window instead of 00-06, and the width
# yardstick is the instrument's own recent Tokyo ranges instead of ATR(4h). Costs are per symbol
# so the same rules can be scored on silver.

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from statistics import median

from hermes import ATR, Backtest, Parameter, RiskPercent, Strategy, Symbol, Timeframe
from hermes.data import PepperstoneSource
from hermes.execution import (
    CostModel,
    FinancingModel,
    OrderStatus,
    OrderType,
    PerLotCommission,
    SlippageModel,
    SpreadModel,
    SpreadSchedule,
)

GENERATED_BY = "hermes-strategy"

TRIG_ATR = 0.02
RANGE_END_MIN = 9 * 60            # Tokyo window 00:00-09:00 UTC
LAST_ENTRY_MIN = 20 * 60
FLAT_MIN = 20 * 60 + 55
LOOKBACK = 20                     # previous Tokyo ranges in the yardstick
MIN_LOOKBACK = 15
MIN_RANGE_BARS = 50               # a full session; skips holidays and short Sundays
# Full spread in price units (Pepperstone Razor, typical): gold $0.12, silver $0.025.
SPREAD = {"XAUUSD": 0.12, "XAGUSD": 0.025}


class GoldTokyoBreakoutV2(Strategy):
    base_tf = Timeframe.parse("5m")
    flat_min = FLAT_MIN               # minute of the bar that issues the flatten order

    def setup(self) -> None:
        self.width_ratio = float(self.param(Parameter(
            "width_ratio", 1.31, bounds=(1.0, 2.0),
            description="Trade only if the Tokyo range is at least this multiple of the "
                        "median of the previous 20 Tokyo ranges")))
        self.stop_atr = float(self.param(Parameter(
            "stop_atr", 1.0, bounds=(0.5, 1.5), description="Stop distance in ATR(4h,20)")))
        self.atr = self.use(ATR(Timeframe.parse("4h"), 20))
        self._ranges: deque[float] = deque(maxlen=LOOKBACK)
        self._day = None
        self._hi = self._lo = None
        self._n = 0
        self._done: set[int] = set()
        self._pending: list = []
        self._range_ok: bool | None = None

    def _close_day(self) -> None:
        """Record the finished day's Tokyo range for tomorrow's yardstick."""
        if self._day is not None and self._day.weekday() < 5 and self._n >= MIN_RANGE_BARS:
            self._ranges.append(self._hi - self._lo)

    def on_bar(self, bar) -> None:
        ts = bar.timestamp.astimezone(UTC)
        minute = ts.hour * 60 + ts.minute
        if ts.date() != self._day:
            self._close_day()
            self._day, self._hi, self._lo, self._n = ts.date(), None, None, 0
            self._done, self._range_ok = set(), None
        for o in self._pending:
            if o.status is OrderStatus.FILLED:
                self._done.add(1 if o.side.name == "BUY" else -1)
            else:
                self.venue.cancel(o)
        self._pending = []

        if minute < RANGE_END_MIN:
            self._hi = bar.high if self._hi is None else max(self._hi, bar.high)
            self._lo = bar.low if self._lo is None else min(self._lo, bar.low)
            self._n += 1
            return

        trades = self.venue.open_trades()
        if trades:
            if minute >= self.flat_min:
                for t in trades:
                    self.close(t)
            return
        atr = self.indicator_value(self.atr)["value"]
        if atr is None or self._hi is None or minute + 5 > LAST_ENTRY_MIN:
            return
        if self._range_ok is None:              # decided once, at the first bar after 09:00
            self._range_ok = (
                ts.weekday() < 5 and self._n >= MIN_RANGE_BARS
                and len(self._ranges) >= MIN_LOOKBACK
                and (self._hi - self._lo) >= self.width_ratio * median(self._ranges)
            )
        if not self._range_ok:
            return
        trig = TRIG_ATR * atr
        if 1 not in self._done:
            entry = self._hi + trig
            self._pending.append(self.buy(RiskPercent(0.01), type=OrderType.STOP, stop=entry,
                                          stop_loss=entry - self.stop_atr * atr, tag="tokyo_long"))
        if -1 not in self._done:
            entry = self._lo - trig
            self._pending.append(self.sell(RiskPercent(0.01), type=OrderType.STOP, stop=entry,
                                           stop_loss=entry + self.stop_atr * atr, tag="tokyo_short"))


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("XAUUSD", "pepperstone"))
    if "cost_model" not in overrides:
        overrides["cost_model"] = CostModel(
            commission=PerLotCommission(per_lot=3.5),
            spread=SpreadModel(points=SPREAD.get(symbol.ticker, SPREAD["XAUUSD"]),
                               schedule=SpreadSchedule.cfd_default()),
            slippage=SlippageModel(ticks=0.0),
            financing=FinancingModel(annual_rate=0.005),
        )
    strategy = overrides.pop("strategy", None) or GoldTokyoBreakoutV2()
    return Backtest(
        strategy=strategy,
        source=PepperstoneSource(),
        symbol=symbol,
        timeframes=[strategy.base_tf],
        start=overrides.pop("start", datetime(2018, 1, 1, tzinfo=UTC)),
        end=overrides.pop("end", datetime(2026, 8, 6, tzinfo=UTC)),
        starting_cash=overrides.pop("starting_cash", 100_000),
        **overrides,
    )


if __name__ == "__main__":
    build_backtest().run()
