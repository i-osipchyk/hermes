# Parameters: 3  (n, max_age, min_age; only n and min_age are ever varied)
#
# gold_fractal_breakout.py with the stop/target taken from LOWER-timeframe structure
# (idea 13374202, iteration 6).  Entry is unchanged: a stop-entry touch of the 1h swing.
#
#   Stop:   beyond the latest confirmed 15m fractal (n=2 bars each side) on the far side of
#           the entry (swing low for longs, swing high for shorts), but never closer than
#           1.0 x ATR(14) of the 15m bars.  If the latest 15m swing is on the wrong side
#           of the level, only the ATR floor applies.
#   Target: 2R from that stop distance (unchanged RR).
# The 15m swing is known only after its 2nd right-hand bar closes (no look-ahead).

from __future__ import annotations

import importlib.util
from pathlib import Path

from hermes import Backtest

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_gold_fractal_breakout", Path(__file__).with_name("gold_fractal_breakout.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"
LTF_N = 2


class GoldFractalLtfStop(_base.GoldFractalBreakout):
    def setup(self) -> None:
        super().setup()
        self._ltf: list = []
        self._ltf_high: float | None = None
        self._ltf_low: float | None = None

    def _on_base_bar(self, bar) -> None:
        self._ltf = (self._ltf + [bar])[-(2 * LTF_N + 1):]
        if len(self._ltf) < 2 * LTF_N + 1:
            return
        mid = self._ltf[LTF_N]
        others = self._ltf[:LTF_N] + self._ltf[LTF_N + 1:]
        if all(mid.high > b.high for b in others):
            self._ltf_high = mid.high
        if all(mid.low < b.low for b in others):
            self._ltf_low = mid.low

    def _risk(self, direction: int, level: float, atr: float) -> float:
        floor = atr
        if direction > 0 and self._ltf_low is not None and self._ltf_low < level:
            return max(level - self._ltf_low, floor)
        if direction < 0 and self._ltf_high is not None and self._ltf_high > level:
            return max(self._ltf_high - level, floor)
        return floor


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("base", "15m")
    overrides.setdefault("strategy", GoldFractalLtfStop())
    strat = overrides["strategy"]
    strat.base_tf = _base.Timeframe.parse(overrides["base"])
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
