# Parameters: 1  (sl_atr)
#
# Gold 1h trend-structure breakout (idea e670d208), iteration 2: trailing exit.
#
# Iteration 1 (fixed 2R) had no edge even before costs (PF 0.94) -- a fixed target caps the
# trend continuation the idea is supposed to capture. Same entry, structure and initial
# stop; the take profit is removed and the stop trails instead:
#
#   Trail:  stop = (highest high since entry) - sl_atr x ATR(14)   (long; mirrored short),
#           re-evaluated on each CLOSED 1h bar and only ever tightened.
#
# The trail distance reuses sl_atr, so there is no extra knob and rr disappears: k = 1.

from __future__ import annotations

import importlib.util
from pathlib import Path

from hermes import Backtest
from hermes.execution import Side

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_gold_swing_breakout", Path(__file__).with_name("gold_swing_breakout.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"


class GoldSwingBreakoutTrail(_base.GoldSwingBreakout):
    def _declare_rr(self):
        return None

    def _take_profit(self, level, risk, sign):
        return None

    def _manage(self, bar) -> None:
        atr = self.indicator_value(self.atr)["value"]
        if atr is None:
            return
        dist = self._sl_atr * atr
        for t in self.venue.open_trades():
            if t.side is Side.BUY:
                self._ext[id(t)] = max(self._ext.get(id(t), bar.high), bar.high)
                new = self._ext[id(t)] - dist
                if t.stop_loss is None or new > t.stop_loss:
                    self.modify(t, stop_loss=new)
            else:
                self._ext[id(t)] = min(self._ext.get(id(t), bar.low), bar.low)
                new = self._ext[id(t)] + dist
                if t.stop_loss is None or new < t.stop_loss:
                    self.modify(t, stop_loss=new)

    def setup(self) -> None:
        super().setup()
        self._ext: dict[int, float] = {}


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("strategy", GoldSwingBreakoutTrail())
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
