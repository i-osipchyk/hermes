# Parameters: 2  (range_min, stop_atr)
#
# Gold Tokyo-range breakout (idea 0945d084; research/tokyo/tokyo_breakout_study.py).
#
#   Range:   high/low of the 00:00-06:00 UTC bars of the current UTC day.
#   Filter:  trade only when the range is wide, >= range_min x ATR(4h,20) at 06:00 -- a trending
#            Asian session (event study: 2018-22 +0.127 R vs +0.011 for all breaks).
#   Entry:   buy stop 0.02 x ATR above the range high / sell stop below the low, 06:00-20:00 UTC;
#            each side at most once per day; one position at a time.
#   Exit:    stop stop_atr x ATR from entry; no target; flat at the 20:55 UTC bar (intraday).
#
# Base bars 5m; ATR from 4h bars (Forming Bar included, ADR-0002).

from __future__ import annotations

from datetime import UTC, datetime

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
RANGE_END_MIN = 6 * 60            # Tokyo window 00:00-06:00 UTC
LAST_ENTRY_MIN = 20 * 60
FLAT_MIN = 20 * 60 + 55
SPREAD_USD = 0.12


class GoldTokyoBreakout(Strategy):
    base_tf = Timeframe.parse("5m")

    def setup(self) -> None:
        self.range_min = float(self.param(Parameter(
            "range_min", 1.2, bounds=(0.0, 3.0),
            description="Trade only if the Tokyo range is at least this many ATR wide")))
        self.stop_atr = float(self.param(Parameter(
            "stop_atr", 1.0, bounds=(0.25, 3.0), description="Stop distance in ATR(4h,20)")))
        self.atr = self.use(ATR(Timeframe.parse("4h"), 20))
        self._day = None
        self._hi = self._lo = None
        self._done: set[int] = set()
        self._pending: list = []
        self._range_ok: bool | None = None

    def on_bar(self, bar) -> None:
        ts = bar.timestamp.astimezone(UTC)
        minute = ts.hour * 60 + ts.minute
        if ts.date() != self._day:
            self._day, self._hi, self._lo = ts.date(), None, None
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
            return

        trades = self.venue.open_trades()
        if trades:
            if minute >= FLAT_MIN:
                for t in trades:
                    self.close(t)
            return
        atr = self.indicator_value(self.atr)["value"]
        if atr is None or self._hi is None or minute + 5 > LAST_ENTRY_MIN:
            return
        if self._range_ok is None:              # decided once, at the first bar after 06:00
            self._range_ok = (self._hi - self._lo) >= self.range_min * atr
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
            spread=SpreadModel(points=SPREAD_USD, schedule=SpreadSchedule.cfd_default()),
            slippage=SlippageModel(ticks=0.0),
            financing=FinancingModel(annual_rate=0.005),
        )
    strategy = overrides.pop("strategy", None) or GoldTokyoBreakout()
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
