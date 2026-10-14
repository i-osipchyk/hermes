"""research/tokyo/static_acceptance.py: is the static variant close enough to run 2fe13c7c?

Thresholds were fixed before the variant was run (gold-tokyo-breakout docs/ARCHITECTURE.md §9.1).
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

_path = Path(__file__).parents[1] / "research/tokyo/static_acceptance.py"
_spec = importlib.util.spec_from_file_location("static_acceptance", _path)
acc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = acc     # dataclasses look their module up here
_spec.loader.exec_module(acc)

PASSING = {"trades": 1100, "sharpe": 1.02, "mean_r": 0.09, "profit_factor": 1.20,
           "mc_worst_dd": -0.50, "overlap": 0.96}


def test_variant_inside_every_threshold_is_accepted():
    verdict = acc.check(PASSING)

    assert verdict.accepted
    assert {c.name for c in verdict.criteria} == {
        "trades", "sharpe", "mean_r", "profit_factor", "mc_worst_dd", "overlap"}


@pytest.mark.parametrize("name, value", [
    ("trades", 1064), ("trades", 1178), ("sharpe", 0.94), ("mean_r", 0.079),
    ("profit_factor", 1.17), ("mc_worst_dd", -0.53), ("overlap", 0.949),
])
def test_one_metric_outside_its_threshold_rejects_the_variant(name, value):
    verdict = acc.check({**PASSING, name: value})

    assert not verdict.accepted
    assert [c.name for c in verdict.criteria if not c.passed] == [name]


@pytest.mark.parametrize("name, value", [
    ("trades", 1065), ("trades", 1177), ("sharpe", 0.95), ("mean_r", 0.08),
    ("profit_factor", 1.18), ("mc_worst_dd", -0.52), ("overlap", 0.95),
])
def test_thresholds_are_inclusive(name, value):
    assert acc.check({**PASSING, name: value}).accepted


def test_overlap_is_the_share_of_reference_day_sides_the_variant_also_traded():
    reference = [(date(2026, 8, 6), "SELL"), (date(2026, 8, 7), "BUY"),
                 (date(2026, 9, 16), "BUY"), (date(2026, 9, 16), "SELL")]
    variant = [(date(2026, 8, 6), "SELL"), (date(2026, 8, 7), "SELL"),   # wrong side
               (date(2026, 9, 16), "BUY"), (date(2026, 9, 17), "BUY")]   # extra day ignored

    assert acc.overlap(variant, reference) == 0.5


def test_monte_carlo_worst_drawdown_in_r_over_shuffled_trade_order():
    # Worst ordering of two losses and two wins is the two losses back to back: -2 R.
    assert acc.mc_worst_drawdown_r([-1.0, -1.0, 1.0, 1.0], n=1000, seed=42) == -2.0
    assert acc.mc_worst_drawdown_r([0.5, 1.0, 2.0], n=100, seed=42) == 0.0


def test_trade_r_is_net_pnl_over_one_percent_of_equity_at_entry():
    trades = [
        {"entry_time": "2026-08-06T13:05:00+00:00", "exit_time": "2026-08-06T20:50:00+00:00",
         "net_pnl": 1000.0},
        {"entry_time": "2026-08-07T09:40:00+00:00", "exit_time": "2026-08-07T15:00:00+00:00",
         "net_pnl": -1010.0},                    # entered with equity 101,000 -> 1 R = 1,010
    ]

    assert acc.trade_rs(trades, 100_000) == [1.0, -1.0]
