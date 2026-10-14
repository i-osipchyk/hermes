# Parameters: 3  (n, max_age, min_age) + seed (a control knob, not a strategy parameter)
#
# RANDOM-DIRECTION CONTROL for gold_fractal_breakout_ltfstop.py (idea 13374202, iteration 7).
#
# Identical in every respect -- 1h n-fractals, 15m-structure stop floored at 1xATR15, 2R
# target, stop-entry on touch, one position, costs -- except the SIDE: whenever market
# structure is a trend (up or down), the side is drawn at random (seeded) once per swing
# set, instead of following the structure.  A with-structure edge must beat the
# distribution of this control; if it does not, the stop geometry and gold's drift are doing
# the work and the "breakout in the direction of structure" claim is unsupported.

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

from hermes import Backtest, Parameter

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_gold_fractal_breakout_ltfstop",
    Path(__file__).with_name("gold_fractal_breakout_ltfstop.py"),
)
_ltf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ltf)

GENERATED_BY = "hermes-strategy"


class RandomDirectionControl(_ltf.GoldFractalLtfStop):
    def setup(self) -> None:
        super().setup()
        seed = int(self.param(Parameter("seed", 0, bounds=(0, 10_000), description="control RNG seed")))
        self._rng = random.Random(seed)
        self._draws: dict = {}

    def _structure(self) -> int:
        if super()._structure() == 0:
            return 0
        key = (self._highs[1][1], self._lows[1][1])
        if key not in self._draws:
            self._draws[key] = self._rng.choice((1, -1))
        return self._draws[key]


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("strategy", RandomDirectionControl())
    return _ltf.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
