# Parameters: 2  (width_ratio, stop_atr)
#
# gold_tokyo_breakout_v2_flat on XAGUSD with costs measured from the Pepperstone account
# (2026-10-10): silver is spread-only (ProtoOASymbol commission 0, lot 5,000 oz) and the
# bid/ask spread sampled from cTrader ticks was $0.008-0.0145 in 2018-22 (median ~$0.012),
# $0.017-0.019 in 2023-25, ~$0.05 in 2026 at $84 silver (~6 bp of price throughout).
# The first silver run (5bc6d89a) charged $0.025 + $3.5/lot -- about twice the real cost.
# The spread is chosen by the window's start year (measured level for that period); 2026's
# ~$0.05 is not modelled -- windows reaching into 2026 are optimistic there.

from __future__ import annotations

import importlib.util
from pathlib import Path

from hermes import Symbol
from hermes.execution import (
    CostModel,
    FinancingModel,
    PerLotCommission,
    SlippageModel,
    SpreadModel,
    SpreadSchedule,
)

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_gold_tokyo_breakout_v2_flat",
    Path(__file__).with_name("gold_tokyo_breakout_v2_flat.py"),
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"

SPREAD_BY_PERIOD = ((2023, 0.012), (9999, 0.018))   # (start year before, spread $)


def build_backtest(**overrides):
    overrides.setdefault("symbol", Symbol("XAGUSD", "pepperstone"))
    start_year = overrides["start"].year if "start" in overrides else 2018
    spread = next(sp for before, sp in SPREAD_BY_PERIOD if start_year < before)
    overrides.setdefault("cost_model", CostModel(
        commission=PerLotCommission(per_lot=0.0),
        spread=SpreadModel(points=spread, schedule=SpreadSchedule.cfd_default()),
        slippage=SlippageModel(ticks=0.0),
        financing=FinancingModel(annual_rate=0.005),
    ))
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
