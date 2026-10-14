# Parameters: 3  (n, max_age, min_age; max_age stays 0 -- iteration 3 showed it inert)
#
# Gold fractal-structure breakout v2 (idea 13374202; parent e670d208 rejected at n=1).
#
# Same logic as gold_swing_breakout.py, generalised:
#   Swing:     fractal of width n on 1h bars: bar i is a swing high if its high is STRICTLY
#              above the highs of the n bars on each side (lows mirrored).  Known only when
#              bar i+n closes.
#   Structure: uptrend = latest swing high > previous AND latest swing low > previous;
#              downtrend = both lower; else no trade.
#   Entry:     BUY STOP at the latest swing high (uptrend) / SELL STOP at the latest swing
#              low (downtrend), fills on touch.  Placed only while price is still on the
#              near side of the level; each swing level triggers at most once.
#   Expiry:    max_age = how many 1h bars after confirmation the swing stays tradeable
#              (0 = never expires).  An old swing is stale liquidity.
#   Stop/TP:   entry -/+ 1.5 x ATR(14) of the BASE timeframe bars; TP at 2R.  Both are
#              hard-coded, carried over from iteration 1 so they are not a search space.
#
# Swings are always built from 1h bars; the base (execution) timeframe may be finer
# (BASE_TF env-free: pass base="15m" to build_backtest).  1h bars are assembled from base
# bars by UTC clock hour, and a 1h bar is only used after its last base bar has closed.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

ATR_PERIOD = 14
SL_ATR = 1.5
RR = 2.0
SPREAD_USD = 0.12
HOUR = timedelta(hours=1)


class GoldFractalBreakout(Strategy):
    base_tf = Timeframe.parse("1h")

    def setup(self) -> None:
        self._n = int(self.param(Parameter(
            "n", 1, bounds=(1, 10), description="Fractal width: bars on each side of a 1h swing",
        )))
        self._max_age = int(self.param(Parameter(
            "max_age", 0, bounds=(0, 200),
            description="Swing expires this many 1h bars after confirmation (0=never)",
        )))
        self._min_age = int(self.param(Parameter(
            "min_age", 0, bounds=(0, 96),
            description="A swing counts only after standing unbroken this many 1h bars (0=at once)",
        )))
        self.atr = self.use(ATR(self.base_tf, ATR_PERIOD))
        self._cand: list = []                       # fractals awaiting min_age bars unbroken
        self._broken: set = set()                   # swing timestamps whose level has been touched
        self._hour_bars: list = []                  # closed 1h bars (last 2n+1)
        self._acc = None                            # [open_ts, high, low] of the forming hour
        self._hour_idx = 0                          # count of closed 1h bars
        self._highs: list[tuple] = []               # (price, swing_ts, confirmed_hour_idx), last 2
        self._lows: list[tuple] = []
        self._hour_closed = False
        self._pending = None
        self._pending_id = None
        self._consumed: set = set()

    # --- 1h assembly -------------------------------------------------------
    def _feed_hour(self, bar) -> bool:
        """Accumulate base bars into 1h bars; return True when a 1h bar just closed."""
        hour = bar.timestamp.replace(minute=0, second=0, microsecond=0)
        if self._acc is not None and self._acc[0] != hour:
            self._acc = None                        # missing tail of an hour (gap): drop it
        if self._acc is None:
            self._acc = [hour, bar.high, bar.low]
        else:
            self._acc[1] = max(self._acc[1], bar.high)
            self._acc[2] = min(self._acc[2], bar.low)
        closes_hour = bar.timestamp + timedelta(seconds=bar.timeframe.seconds) >= hour + HOUR
        if not closes_hour:
            return False
        self._hour_bars = (self._hour_bars + [tuple(self._acc)])[-(2 * self._n + 1):]
        self._acc = None
        self._hour_idx += 1
        return True

    def _detect_swings(self) -> None:
        n = self._n
        if len(self._hour_bars) < 2 * n + 1:
            return
        ts, hi, lo = self._hour_bars[n]
        others = self._hour_bars[:n] + self._hour_bars[n + 1:]
        if all(hi > b[1] for b in others):
            self._cand.append(("high", hi, ts, self._hour_idx))
        if all(lo < b[2] for b in others):
            self._cand.append(("low", lo, ts, self._hour_idx))

    def _qualify(self) -> None:
        """Promote candidates that have stood unbroken for min_age 1h bars."""
        keep = []
        for kind, price, ts, idx in self._cand:
            if self._hour_idx - idx < self._min_age:
                keep.append((kind, price, ts, idx))
            elif kind == "high":
                self._highs = (self._highs + [(price, ts, idx)])[-2:]
            else:
                self._lows = (self._lows + [(price, ts, idx)])[-2:]
        self._cand = keep

    def _structure(self) -> int:
        if len(self._highs) < 2 or len(self._lows) < 2:
            return 0
        up = self._highs[1][0] > self._highs[0][0] and self._lows[1][0] > self._lows[0][0]
        down = self._highs[1][0] < self._highs[0][0] and self._lows[1][0] < self._lows[0][0]
        return 1 if up else -1 if down else 0

    def _fresh(self, swing) -> bool:
        age = self._hour_idx - swing[2]
        if self._max_age and age > self._max_age:
            return False
        return not (self._min_age and swing[1] in self._broken)

    def _mark_broken(self, bar) -> None:
        self._cand = [
            c for c in self._cand
            if not (bar.high >= c[1] if c[0] == "high" else bar.low <= c[1])
        ]
        for price, ts, _ in self._highs:
            if bar.high >= price:
                self._broken.add(ts)
        for price, ts, _ in self._lows:
            if bar.low <= price:
                self._broken.add(ts)

    def _risk(self, direction: int, level: float, atr: float) -> float:
        """Stop distance from the entry level; overridden by lower-timeframe variants."""
        return SL_ATR * atr

    def _on_base_bar(self, bar) -> None:
        """Hook: called on every base bar before the trading logic."""

    # --- trading -----------------------------------------------------------
    def on_bar(self, bar) -> None:
        self._on_base_bar(bar)
        self._hour_closed = self._feed_hour(bar)
        if self._hour_closed:
            self._detect_swings()
        self._mark_broken(bar)
        if self._hour_closed:
            self._qualify()

        if self._pending is not None and self._pending.status is OrderStatus.FILLED:
            self._consumed.add(self._pending_id)
            self._pending = None
        if self.venue.open_trades():
            return
        if self._pending is not None:
            self.venue.cancel(self._pending)
            self._pending = None

        atr = self.indicator_value(self.atr)["value"]
        direction = self._structure()
        if atr is None or direction == 0:
            return

        if direction > 0:
            swing = self._highs[1]
            level = swing[0]
            if bar.close >= level or ("long", swing[1]) in self._consumed or not self._fresh(swing):
                return
            risk = self._risk(1, level, atr)
            self._pending_id = ("long", swing[1])
            self._pending = self.buy(
                RiskPercent(0.01), type=OrderType.STOP, stop=level,
                stop_loss=level - risk, take_profit=level + RR * risk, tag="fractal_long",
            )
        else:
            swing = self._lows[1]
            level = swing[0]
            if bar.close <= level or ("short", swing[1]) in self._consumed or not self._fresh(swing):
                return
            risk = self._risk(-1, level, atr)
            self._pending_id = ("short", swing[1])
            self._pending = self.sell(
                RiskPercent(0.01), type=OrderType.STOP, stop=level,
                stop_loss=level + risk, take_profit=level - RR * risk, tag="fractal_short",
            )


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("XAUUSD", "pepperstone"))
    base = Timeframe.parse(overrides.pop("base", "1h"))
    source = PepperstoneSource()
    if "cost_model" not in overrides:
        overrides["cost_model"] = CostModel(
            commission=PerLotCommission(per_lot=3.5),
            spread=SpreadModel(points=SPREAD_USD, schedule=SpreadSchedule.cfd_default()),
            slippage=SlippageModel(ticks=0.0),
            financing=FinancingModel(annual_rate=0.005),
        )
    strategy = overrides.pop("strategy", None)
    if strategy is None:
        strategy = GoldFractalBreakout()
        strategy.base_tf = base
    return Backtest(
        strategy=strategy,
        source=source,
        symbol=symbol,
        timeframes=[base],
        start=overrides.pop("start", datetime(2018, 1, 1, tzinfo=UTC)),
        end=overrides.pop("end", datetime(2026, 9, 30, 23, 59, tzinfo=UTC)),
        starting_cash=overrides.pop("starting_cash", 100_000),
        **overrides,
    )


if __name__ == "__main__":
    build_backtest().run()
