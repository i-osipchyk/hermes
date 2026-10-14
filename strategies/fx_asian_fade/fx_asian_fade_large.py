# Parameters: 1  (k)
#
# Asian-session overshoot fade, large moves only (idea e411c67d, iteration 2).
#
# Same entry/exit/costs as fx_asian_fade.py. The mechanism is about OVERSHOOT -- a thin
# book lets an order push price further than London liquidity would allow -- so the
# temporary component should be largest on unusually big Asian moves. The baseline faded
# every move, and its small gross edge was eaten by costs on ~775 trades/pair. Here only
# moves larger than k x the mean |Asian move| of the previous 20 sessions are faded.
# The 20-session lookback is a fixed constant (about a trading month), not tuned.

from __future__ import annotations

import importlib.util
from collections import deque
from pathlib import Path

from hermes import Backtest, Parameter, Symbol

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_fx_asian_fade", Path(__file__).with_name("fx_asian_fade.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"

LOOKBACK = 20


class AsianFadeLarge(_base.AsianFade):
    def setup(self) -> None:
        super().setup()
        self._k = self.param(Parameter(
            "k", 1.5, bounds=(1.0, 2.5),
            description="Fade only Asian moves larger than k x their trailing 20-session mean size",
        ))
        self._sizes: deque[float] = deque(maxlen=LOOKBACK)

    def asian_direction(self, move: float) -> int:
        # Threshold from PRIOR sessions only; today's move is appended afterwards.
        ready = len(self._sizes) == LOOKBACK
        threshold = self._k * sum(self._sizes) / LOOKBACK if ready else None
        self._sizes.append(abs(move))
        if threshold is None or abs(move) <= threshold:
            return 0
        return (move > 0) - (move < 0)


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("strategy", AsianFadeLarge())
    overrides.setdefault("symbol", Symbol("EURUSD", "pepperstone"))
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
