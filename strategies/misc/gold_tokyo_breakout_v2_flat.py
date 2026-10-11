# Parameters: 2  (width_ratio, stop_atr)
#
# gold_tokyo_breakout_v2 with the flatten order issued at the 20:45 UTC bar (fills at the 20:50
# open) instead of 20:55. At 20:55 the market close filled on the next bar, which is the reopen
# after the 21:00/22:00 UTC daily break, or Sunday's open on Fridays: 53 of 540 trades were held
# through a weekend or break and earned 58% of the net P&L -- a gap bet the intraday rule never
# intended. This makes the backtest flat before the break, as the rule says.

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_gold_tokyo_breakout_v2", Path(__file__).with_name("gold_tokyo_breakout_v2.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"


class GoldTokyoBreakoutV2Flat(_base.GoldTokyoBreakoutV2):
    flat_min = 20 * 60 + 45


def build_backtest(**overrides):
    overrides.setdefault("strategy", GoldTokyoBreakoutV2Flat())
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
