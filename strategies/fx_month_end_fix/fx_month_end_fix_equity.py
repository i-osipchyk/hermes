# Parameters: 0
#
# Month-end fix, equity-implied direction (idea 5cf533fe, iteration 2).
#
# Same entry/exit/costs as fx_month_end_fix.py. The only change is WHICH direction to
# fade: instead of the realised 15:00-16:00 London move, the direction the hedging flow
# should push the pair, from month-to-date equity performance (Melvin & Prins 2015):
#
#   US equities outperformed the foreign market month-to-date
#     -> foreign holders of US stocks are under-hedged, sell USD into the fix
#     -> USD weak into the fix, so fade by BUYING USD after it.
#   Foreign equities outperformed -> the mirror image: SELL USD after the fix.
#
# MTD return = latest closed 5m bar / last close of the previous calendar month, for
# US500 and the pair's foreign index (Reference feeds: closed bars only, ADR-0002).
# A month whose previous close was not observed (the first month of the window) is
# skipped.

from __future__ import annotations

import importlib.util
from pathlib import Path

from hermes import Backtest, Symbol

# Discovery loads strategy files by path, so a sibling is imported the same way.
_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_fx_month_end_fix", Path(__file__).with_name("fx_month_end_fix.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
M5, MonthEndFixFade, _build = _base.M5, _base.MonthEndFixFade, _base.build_backtest

GENERATED_BY = "hermes-strategy"

# Foreign equity index for the non-USD leg of each pair.
FOREIGN_INDEX = {"EUR": "GER40", "GBP": "UK100", "JPY": "JPN225"}


def _mtd(month_closes: dict, latest) -> float | None:
    if latest is None:
        return None
    prev = month_closes.get(_prev_month(latest.timestamp))
    return None if prev is None else latest.close / prev - 1


def _prev_month(ts) -> tuple[int, int]:
    return (ts.year - 1, 12) if ts.month == 1 else (ts.year, ts.month - 1)


class MonthEndFixEquity(MonthEndFixFade):
    def setup(self) -> None:
        super().setup()
        base, quote = self.instrument.symbol.ticker[:3], self.instrument.symbol.ticker[3:]
        self._usd_is_quote = quote == "USD"
        foreign = base if self._usd_is_quote else quote
        self._us = self.use_reference("US500")
        self._fx = self.use_reference(FOREIGN_INDEX[foreign])
        # (year, month) -> last close seen in that month, per reference.
        self._closes: dict = {id(self._us): {}, id(self._fx): {}}

    def _latest(self, ref):
        closed = ref.data(M5).closed()
        if not closed:
            return None
        bar = closed[-1]
        self._closes[id(ref)][(bar.timestamp.year, bar.timestamp.month)] = bar.close
        return bar

    def on_bar(self, bar) -> None:
        self._us_bar = self._latest(self._us)
        self._fx_bar = self._latest(self._fx)
        super().on_bar(bar)

    def pre_fix_direction(self, bar) -> int:
        us = _mtd(self._closes[id(self._us)], self._us_bar)
        foreign = _mtd(self._closes[id(self._fx)], self._fx_bar)
        if us is None or foreign is None or us == foreign:
            return 0
        usd_weak_into_fix = us > foreign
        # Pair direction into the fix; the base class fades it.
        if self._usd_is_quote:
            return 1 if usd_weak_into_fix else -1
        return -1 if usd_weak_into_fix else 1


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("strategy", MonthEndFixEquity())
    overrides.setdefault("symbol", Symbol("EURUSD", "pepperstone"))
    return _build(**overrides)


if __name__ == "__main__":
    build_backtest().run()
