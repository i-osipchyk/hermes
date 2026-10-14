# Parameters: 0
#
# Month-end fix fade, 1-hour hold (idea 5cf533fe, iteration 3).
#
# Identical to fx_month_end_fix.py except the exit: flat at 17:05 London, one hour after
# entry, instead of 16:30 New York. Chosen a priori from the mechanism, not from a sweep:
# the reversal is the decay of the fix flow's temporary price impact, which dealers lay
# off within roughly an hour; a 5.5-hour hold buries it in unrelated afternoon noise.

from __future__ import annotations

import importlib.util
from datetime import time, timedelta
from pathlib import Path

from hermes import Backtest, Symbol

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_fx_month_end_fix", Path(__file__).with_name("fx_month_end_fix.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"

EXIT_LONDON = time(17, 5)


class MonthEndFixFade1h(_base.MonthEndFixFade):
    def exit_due(self, bar) -> bool:
        bar_end = (bar.timestamp + timedelta(seconds=bar.timeframe.seconds)).astimezone(_base.LONDON)
        return bar_end.time() >= EXIT_LONDON or bar_end.date() != self._day


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("strategy", MonthEndFixFade1h())
    overrides.setdefault("symbol", Symbol("EURUSD", "pepperstone"))
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
