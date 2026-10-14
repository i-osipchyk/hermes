# Parameters: 2  (sl_atr, rr)
#
# Gold 1h trend-structure breakout (idea e670d208), iteration 1: fixed RR baseline.
#
# Rules, all on closed XAUUSD 1h bars:
#   Swing:      fractal with n=1.  Bar i is a swing high if high[i] > high[i-1] and
#               high[i] > high[i+1] (strict; ties are not swings); swing low mirrored.
#               It is KNOWN only when bar i+1 closes -- the order below is placed then,
#               so no information from after the close of i+1 reaches the decision.
#   Structure:  uptrend   = latest swing high > previous swing high AND
#                           latest swing low  > previous swing low
#               downtrend = both lower.  Anything else (mixed) = no trade.
#   Entry:      uptrend -> BUY STOP at the latest swing high (fills on touch, gap fills at
#               the open, ADR-0004).  Downtrend -> SELL STOP at the latest swing low.
#               Re-planted every flat bar so it always follows the current structure and
#               is cancelled the moment the structure stops being a trend.  Only placed
#               while price is still on the near side of the level (close < swing high
#               for longs), and each swing level triggers at most once.
#   Stop loss:  entry -/+ sl_atr x ATR(14) of the 1h bars (ATR period fixed, not tuned).
#   Take profit: entry +/- rr x stop distance (fixed RR; trailing is a later variant).
#   Sizing:     RiskPercent(1%) to the stop; one position at a time; no time exit.
#   Costs:      Razor-style: $3.50/lot/side; spread 0.12 USD (a priori, ~12 ticks on gold,
#               with the CFD rollover spread schedule).  Financing left at the 0.5%
#               default -- NOT a gold swap rate, flagged as unmeasured.
#
# sl_atr = 1.5 and rr = 2.0 are set a priori, not fitted.

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

H1 = Timeframe.parse("1h")
ATR_PERIOD = 14
SPREAD_USD = 0.12


class GoldSwingBreakout(Strategy):
    def setup(self) -> None:
        self._sl_atr = self.param(Parameter(
            "sl_atr", 1.5, bounds=(0.75, 3.0), description="Stop distance in 1h ATR(14) multiples",
        ))
        self._rr = self._declare_rr()
        self.atr = self.use(ATR(H1, ATR_PERIOD))
        self._bars: list = []                       # last 3 closed bars
        self._highs: list[tuple] = []               # (swing high price, bar timestamp), last 2
        self._lows: list[tuple] = []
        self._pending = None
        self._pending_id = None
        self._consumed: set = set()

    def _declare_rr(self):
        return self.param(Parameter(
            "rr", 2.0, bounds=(1.0, 4.0), description="Take profit as a multiple of the stop distance",
        ))

    def _take_profit(self, level: float, risk: float, sign: int) -> float | None:
        return level + sign * self._rr * risk

    def _manage(self, bar) -> None:
        """Hook for open-trade management; the fixed-RR baseline has none."""

    def _detect_swings(self) -> None:
        if len(self._bars) < 3:
            return
        a, b, c = self._bars
        if b.high > a.high and b.high > c.high:
            self._highs = (self._highs + [(b.high, b.timestamp)])[-2:]
        if b.low < a.low and b.low < c.low:
            self._lows = (self._lows + [(b.low, b.timestamp)])[-2:]

    def _structure(self) -> int:
        if len(self._highs) < 2 or len(self._lows) < 2:
            return 0
        up = self._highs[1][0] > self._highs[0][0] and self._lows[1][0] > self._lows[0][0]
        down = self._highs[1][0] < self._highs[0][0] and self._lows[1][0] < self._lows[0][0]
        return 1 if up else -1 if down else 0

    def on_bar(self, bar) -> None:
        self._bars = (self._bars + [bar])[-3:]
        self._detect_swings()

        if self._pending is not None and self._pending.status is OrderStatus.FILLED:
            self._consumed.add(self._pending_id)
            self._pending = None
        if self.venue.open_trades():
            self._manage(bar)
            return
        if self._pending is not None:
            self.venue.cancel(self._pending)
            self._pending = None

        atr = self.indicator_value(self.atr)["value"]
        direction = self._structure()
        if atr is None or direction == 0:
            return

        risk = self._sl_atr * atr
        if direction > 0:
            level, swing_ts = self._highs[1]
            if bar.close >= level or ("long", swing_ts) in self._consumed:
                return
            self._pending_id = ("long", swing_ts)
            self._pending = self.buy(
                RiskPercent(0.01), type=OrderType.STOP, stop=level,
                stop_loss=level - risk, take_profit=self._take_profit(level, risk, 1), tag="swing_long",
            )
        else:
            level, swing_ts = self._lows[1]
            if bar.close <= level or ("short", swing_ts) in self._consumed:
                return
            self._pending_id = ("short", swing_ts)
            self._pending = self.sell(
                RiskPercent(0.01), type=OrderType.STOP, stop=level,
                stop_loss=level + risk, take_profit=self._take_profit(level, risk, -1), tag="swing_short",
            )


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("XAUUSD", "pepperstone"))
    source = PepperstoneSource()
    if "cost_model" not in overrides:
        overrides["cost_model"] = CostModel(
            commission=PerLotCommission(per_lot=3.5),
            spread=SpreadModel(points=SPREAD_USD, schedule=SpreadSchedule.cfd_default()),
            slippage=SlippageModel(ticks=0.0),
            financing=FinancingModel(annual_rate=0.005),
        )
    return Backtest(
        strategy=overrides.pop("strategy", GoldSwingBreakout()),
        source=source,
        symbol=symbol,
        timeframes=[H1],
        start=overrides.pop("start", datetime(2018, 1, 1, tzinfo=UTC)),
        end=overrides.pop("end", datetime(2026, 9, 30, 23, 59, tzinfo=UTC)),
        starting_cash=overrides.pop("starting_cash", 100_000),
        **overrides,
    )


if __name__ == "__main__":
    build_backtest().run()
