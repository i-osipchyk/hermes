# Parameters: 2  (n, max_age)
#
# gold_fractal_breakout.py with execution on 15m bars (idea 13374202, iteration 4):
# swings are still built from 1h bars (n bars each side), but the stop-entry touch, the
# ATR(14) stop distance and the 2R target are all evaluated on 15m bars, so risk is
# sized to 15m volatility and fills are sequenced at 15m resolution.

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


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("base", "15m")
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
