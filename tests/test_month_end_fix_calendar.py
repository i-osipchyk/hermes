"""Early-close days for the month-end fix strategy: the exit must fire before the feed stops.

Close times are what the cached Pepperstone EURUSD 5m bars show: last bar starts 14:50 NY on
Dec 24/31 and 15:50 on Good Friday (2024-03-29, 2025-04-18).
"""

import importlib.util
from datetime import date, time
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "month_end_fix_for_test",
    Path(__file__).parents[1] / "strategies" / "fx_month_end_fix" / "fx_month_end_fix.py",
)
fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix)


def test_exit_is_16h30_on_an_ordinary_day():
    assert fix.exit_bar_end_ny(date(2024, 5, 31)) == time(16, 30)


def test_exit_fires_in_time_for_the_next_open_fill_on_early_close_days():
    assert fix.exit_bar_end_ny(date(2024, 12, 31)) == time(14, 50)
    assert fix.exit_bar_end_ny(date(2025, 12, 24)) == time(14, 50)
    assert fix.exit_bar_end_ny(date(2024, 3, 29)) == time(15, 50)   # Good Friday
    assert fix.exit_bar_end_ny(date(2025, 4, 18)) == time(15, 50)   # Good Friday
