# Parameters: 0
#
# Sunday-open gap fill, entry at the Tokyo open (idea 116ee823, iteration 2).
#
# Identical to fx_sunday_gap.py except the entry: the gap is measured and faded at
# 20:00 New York (09:00 Tokyo in summer) instead of 18:00. Chosen a priori, to test an
# artifact the baseline pointed at: bars are bid-only and the cost model charges a normal
# spread from 18:00, but Sunday-evening spreads stay wide for longer. A still-wide spread
# depresses the bid, which reads as a "gap down" that "fills" as the spread normalises --
# the baseline was 2-4x more often long than short on every pair, and the longs made the
# money. If the overshoot is real it should largely survive two more hours (the gap is
# meant to fill within a day) and the long/short split should even out; if it was the
# spread, the edge goes with it.

from __future__ import annotations

import importlib.util
from datetime import time
from pathlib import Path

from hermes import Backtest, Symbol

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_fx_sunday_gap", Path(__file__).with_name("fx_sunday_gap.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"


class SundayGapFadeTokyo(_base.SundayGapFade):
    entry_bar_end = time(20, 0)


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("strategy", SundayGapFadeTokyo())
    overrides.setdefault("symbol", Symbol("EURUSD", "pepperstone"))
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
