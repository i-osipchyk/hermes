# Parameters: 2  (width_ratio, stop_atr)
#
# gold_tokyo_breakout_v2_flat as it is forward tested (gold-tokyo-breakout docs/ARCHITECTURE.md §2):
# the two stop orders are placed ONCE, right after the 08:55 bar closes (earliest fill on the
# 09:00 bar), with ATR(4h, 20) frozen at that moment, and rest untouched until they fill or expire.
# Live this is two stateless runs a day (09:00:10 place, 20:50:05 flatten) instead of re-placing
# orders every 5 minutes.
#
#   Stop:    d = min(stop_atr x ATR, range + 2 x offset - 0.05 x ATR): on narrow days (~2% of
#            traded days) the stop is shortened so it is hit before the opposite entry, which keeps
#            one position at a time without cancelling the resting opposite order.
#   Size:    from the full stop_atr x ATR, as if the stop were never shortened.
#   Expiry:  unfilled orders cancelled at the 20:00 bar (the last bar that can fill).
#   Flat:    close sent at the 20:45 bar, filled at the 20:50 open (as _flat).
#   Holidays: on an early-close day the flatten fills at the open of the session's last bar
#            (min with 20:50 -- the same as a summer Friday, whose last bar is 20:50) and orders
#            expire two bars before it.

from __future__ import annotations

import importlib.util
from collections import deque
from datetime import UTC, date, time
from pathlib import Path
from statistics import median

from hermes import ATR, Parameter, Strategy, Timeframe
from hermes.execution import OrderStatus, OrderType

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_gold_tokyo_breakout_v2", Path(__file__).with_name("gold_tokyo_breakout_v2.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"

TRIG_ATR = 0.02
SL_BUFFER_ATR = 0.05              # a shortened stop stays this far inside the opposite entry
RISK = 0.01
PLAN_MIN = 8 * 60 + 55            # orders placed while processing the 08:55 bar
FLAT_FILL_MIN = 20 * 60 + 50      # flatten fills at the 20:50 open (close sent at the 20:45 bar)
EXPIRE_MIN = 20 * 60              # unfilled orders cancelled at the 20:00 bar (its fills count)
LOOKBACK = 20
MIN_LOOKBACK = 15
MIN_RANGE_BARS = 50

# Weekday sessions whose last bar (open time, UTC) is before 20:50 -- CME holiday early closes.
# Derived once from the Pepperstone XAUUSD 5m bars (2018-01-01 -> 2026-10-09); the live bot's
# data/early_close.yaml holds the same past dates plus the coming ones.
EARLY_CLOSES: dict[date, time] = {
    date(2018, 1, 15): time(17, 55),
    date(2018, 2, 19): time(18, 30),
    date(2018, 5, 28): time(17, 0),
    date(2018, 9, 3): time(17, 10),
    date(2018, 11, 23): time(18, 45),
    date(2018, 12, 24): time(18, 55),
    date(2019, 1, 21): time(17, 55),
    date(2019, 2, 18): time(17, 55),
    date(2019, 5, 27): time(17, 0),
    date(2019, 7, 4): time(16, 55),
    date(2019, 9, 2): time(16, 55),
    date(2019, 11, 28): time(17, 55),
    date(2019, 11, 29): time(18, 40),
    date(2019, 12, 24): time(18, 40),
    date(2020, 1, 20): time(17, 55),
    date(2020, 2, 17): time(17, 55),
    date(2020, 5, 25): time(16, 55),
    date(2020, 7, 3): time(16, 55),
    date(2020, 9, 7): time(16, 55),
    date(2020, 11, 26): time(17, 55),
    date(2020, 11, 27): time(18, 40),
    date(2020, 12, 24): time(18, 40),
    date(2021, 1, 18): time(17, 55),
    date(2021, 2, 15): time(17, 55),
    date(2021, 5, 31): time(16, 55),
    date(2021, 7, 5): time(16, 40),
    date(2021, 9, 6): time(16, 55),
    date(2021, 11, 25): time(17, 55),
    date(2021, 11, 26): time(18, 40),
    date(2022, 1, 17): time(19, 25),
    date(2022, 2, 21): time(19, 25),
    date(2022, 5, 30): time(18, 25),
    date(2022, 6, 20): time(18, 25),
    date(2022, 7, 4): time(18, 25),
    date(2022, 9, 5): time(18, 25),
    date(2022, 11, 24): time(19, 25),
    date(2022, 11, 25): time(18, 40),
    date(2023, 1, 16): time(19, 25),
    date(2023, 2, 20): time(19, 25),
    date(2023, 5, 29): time(18, 25),
    date(2023, 6, 19): time(18, 25),
    date(2023, 7, 4): time(18, 25),
    date(2023, 9, 4): time(18, 25),
    date(2023, 11, 23): time(19, 25),
    date(2023, 11, 24): time(18, 40),
    date(2024, 1, 15): time(19, 25),
    date(2024, 2, 19): time(19, 25),
    date(2024, 5, 27): time(18, 25),
    date(2024, 6, 19): time(18, 25),
    date(2024, 7, 4): time(18, 25),
    date(2024, 9, 2): time(18, 25),
    date(2024, 11, 28): time(19, 25),
    date(2024, 11, 29): time(19, 40),
    date(2024, 12, 24): time(18, 40),
    date(2024, 12, 31): time(19, 50),
    date(2025, 1, 20): time(19, 25),
    date(2025, 2, 17): time(19, 25),
    date(2025, 5, 26): time(18, 25),
    date(2025, 6, 19): time(18, 25),
    date(2025, 7, 4): time(16, 55),
    date(2025, 9, 1): time(18, 25),
    date(2025, 11, 27): time(19, 25),
    date(2025, 11, 28): time(19, 40),
    date(2025, 12, 24): time(18, 40),
    date(2025, 12, 31): time(19, 50),
    date(2026, 1, 19): time(19, 30),
    date(2026, 2, 16): time(19, 25),
    date(2026, 5, 25): time(18, 25),
    date(2026, 6, 19): time(16, 55),
    date(2026, 7, 3): time(16, 55),
    date(2026, 9, 7): time(18, 25),
}


class GoldTokyoBreakoutV2Static(Strategy):
    base_tf = Timeframe.parse("5m")

    def __init__(self, early_closes: dict[date, time] | None = None) -> None:
        super().__init__()
        self.early_closes = EARLY_CLOSES if early_closes is None else early_closes

    def setup(self) -> None:
        self.width_ratio = float(self.param(Parameter(
            "width_ratio", 1.0, bounds=(1.0, 2.0),
            description="Trade only if the Tokyo range is at least this multiple of the "
                        "median of the previous 20 Tokyo ranges")))
        self.stop_atr = float(self.param(Parameter(
            "stop_atr", 1.0, bounds=(0.5, 1.5), description="Stop distance in ATR(4h,20)")))
        self.atr = self.use(ATR(Timeframe.parse("4h"), 20))
        self._ranges: deque[float] = deque(maxlen=LOOKBACK)
        self._day = None
        self._hi = self._lo = None
        self._n = 0
        self._orders: list = []
        self.day_plans: list[dict] = []

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
            last_bar = self.early_closes.get(self._day)
            self._flat_fill = FLAT_FILL_MIN if last_bar is None else min(
                FLAT_FILL_MIN, last_bar.hour * 60 + last_bar.minute)

        if minute < 9 * 60:
            self._hi = bar.high if self._hi is None else max(self._hi, bar.high)
            self._lo = bar.low if self._lo is None else min(self._lo, bar.low)
            self._n += 1
            if minute == PLAN_MIN and ts.weekday() < 5:
                self._plan(ts)
            return

        if minute >= min(EXPIRE_MIN, self._flat_fill - 10) and self._orders:
            for o in self._orders:
                if o.status is OrderStatus.WORKING:
                    self.venue.cancel(o)
            self._orders = []
        if minute >= self._flat_fill - 5:
            for t in self.venue.open_trades():
                self.close(t)

    def _plan(self, ts) -> None:
        """Decide the day once, after the 08:55 bar, and record why (``day_plans``)."""
        atr = self.indicator_value(self.atr)["value"]
        rng = self._hi - self._lo
        med = median(self._ranges) if self._ranges else None
        plan = {
            "date": ts.date(), "tokyo_high": self._hi, "tokyo_low": self._lo, "bars": self._n,
            "range": rng, "history_len": len(self._ranges), "median": med, "atr": atr,
            "equity": self.venue.equity(), "trade": False, "reason": None,
            "long_entry": None, "long_sl": None, "short_entry": None, "short_sl": None,
            "d": None, "shortened": None, "lots": None,
        }
        self.day_plans.append(plan)
        if atr is None:
            plan["reason"] = "no_atr"
        elif self._n < MIN_RANGE_BARS:
            plan["reason"] = "too_few_bars"
        elif len(self._ranges) < MIN_LOOKBACK:
            plan["reason"] = "short_history"
        elif rng < self.width_ratio * med:
            plan["reason"] = "narrow_range"
        if plan["reason"] is not None:
            return

        off = TRIG_ATR * atr
        risk_distance = self.stop_atr * atr
        d = min(risk_distance, rng + 2 * off - SL_BUFFER_ATR * atr)
        lots = self.instrument.to_native_units(RISK * plan["equity"] / (
            risk_distance * self.instrument.contract_size() * self.venue.quote_rate()))
        long_entry, short_entry = self._hi + off, self._lo - off
        plan.update(
            long_entry=long_entry, long_sl=long_entry - d, short_entry=short_entry,
            short_sl=short_entry + d, d=d, shortened=d < risk_distance, lots=lots,
        )
        if lots <= 0:
            plan["reason"] = "skipped_min_size"
            return
        plan["trade"] = True
        self._orders = [
            self.buy(lots, type=OrderType.STOP, stop=long_entry, stop_loss=plan["long_sl"],
                     tag="tokyo_long"),
            self.sell(lots, type=OrderType.STOP, stop=short_entry, stop_loss=plan["short_sl"],
                      tag="tokyo_short"),
        ]

def build_backtest(**overrides):
    overrides.setdefault("strategy", GoldTokyoBreakoutV2Static())
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
