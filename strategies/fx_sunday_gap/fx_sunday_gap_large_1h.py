# Parameters: 1  (k)
#
# Sunday-open gap fill, large gaps, 2014-2026 on 1h bars (idea 116ee823, iteration 4).
#
# Iteration 3's rules unchanged (k = 0.25 x 14-session daily ATR, gap measured and faded
# at 20:00 NY, take profit at Friday's close), on the sample the analysis asked for: 18-45
# trades per pair over 3 years was exploratory, and every pair had no in-sample edge but a
# positive out-of-sample one -- a period effect until a longer history says otherwise.
# cTrader's 1h history reaches back past 2014 (~665 weekends per pair vs ~155 on 5m).
#
# On 1h bars a fill is priced 10 min after the bar open, so: entry decided on the bar
# ending 20:00 fills 20:10; the time exit moves to the bar ending Monday 16:00 NY (fills
# 16:10), because the bar ending 17:00 would fill through the rollover.

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, time
from pathlib import Path

from hermes import Backtest, Symbol, Timeframe

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_fx_sunday_gap_large", Path(__file__).with_name("fx_sunday_gap_large.py")
)
_large = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_large)
_base = _large._base

GENERATED_BY = "hermes-strategy"

H1 = Timeframe.parse("1h")


class SundayGapFadeLarge1h(_large.SundayGapFadeLarge):
    exit_bar_end = time(16, 0)


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("strategy", SundayGapFadeLarge1h())
    overrides.setdefault("symbol", Symbol("EURUSD", "pepperstone"))
    overrides.setdefault("timeframes", [H1])
    overrides.setdefault("start", datetime(2014, 1, 1, tzinfo=UTC))
    overrides.setdefault("end", datetime(2026, 9, 30, 23, 59, tzinfo=UTC))
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
