# Parameters: 0
#
# fx_month_end_fix_1h on USDJPY. A separate file because the CLI's --symbol keeps the
# cost model built for the default symbol, and Razor costs are priced in ticks: a
# JPY pair's 0.001 tick needs its own CostModel (built here from USDJPY's instrument).

from __future__ import annotations

import importlib.util
from pathlib import Path

from hermes import Backtest, Symbol

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_fx_month_end_fix_1h", Path(__file__).with_name("fx_month_end_fix_1h.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("symbol", Symbol("USDJPY", "pepperstone"))
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
