# Parameters: 0
#
# London range bracket at the NY open, target = the farther edge, stop = the nearer edge (RR > 1).
# Base rules in gold_london_range_ny.py (idea c706aa04; research/london_range/london_bracket_study.py).

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_gold_london_range_ny", Path(__file__).with_name("gold_london_range_ny.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"


class GoldLondonBracketFar(_base.GoldLondonRangeNY):
    toward = "far"
    stop_at = "edge"


def build_backtest(**overrides):
    overrides.setdefault("strategy", GoldLondonBracketFar())
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
