"""gold_tokyo_breakout_v2_static: the forward-test rules (gold-tokyo-breakout docs/ARCHITECTURE.md §2).

Synthetic 5m bars in winter (EST), so the 4h buckets are 22/02/06/10/14/18 UTC and the bucket
holding 06:00-09:00 is still unsealed when orders are placed after the 08:55 bar. Every quiet
bar is level +/- 5, so every sealed 4h true range is 10 and ATR(4h, 20) is exactly 10.
"""

import importlib.util
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hermes import Backtest, Symbol, Timeframe
from hermes.core import Bar, Cfd, SessionCalendar
from hermes.data import InMemorySource
from hermes.execution import (
    CostModel,
    FinancingModel,
    PerLotCommission,
    SlippageModel,
    SpreadModel,
)

M5 = Timeframe.parse("5m")
SYMBOL = Symbol("XAUUSD", "pepperstone")
FIRST_DAY = datetime(2025, 1, 6, tzinfo=UTC)    # Monday; 25 weekdays of history follow
TEST_DAY = datetime(2025, 2, 10, tzinfo=UTC)    # Monday after them

_path = Path(__file__).parents[1] / "strategies/misc/gold_tokyo_breakout_v2_static.py"
_spec = importlib.util.spec_from_file_location("gold_tokyo_breakout_v2_static", _path)
static = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(static)


def _gold() -> Cfd:
    session = SessionCalendar(
        timezone=ZoneInfo("America/New_York"),
        weekdays=(0, 1, 2, 3, 4, 6),
        day_anchor=time(17, 0),
    )
    return Cfd(SYMBOL, session=session, tick_size=0.01, lot_size=100.0, leverage=30.0)


def _zero_costs() -> CostModel:
    return CostModel(
        PerLotCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), FinancingModel(0.0)
    )


def _bar(ts, o, h, l, c) -> Bar:
    return Bar(ts, M5, o, h, l, c, 1.0)


def _quiet_day(day, level=2000.0, half=5.0) -> list[Bar]:
    return [
        _bar(day + timedelta(minutes=5 * i), level, level + half, level - half, level)
        for i in range(288)
    ]


def _history() -> list[Bar]:
    bars, day = [], FIRST_DAY
    while day < TEST_DAY:
        if day.weekday() < 5:
            bars += _quiet_day(day)
        day += timedelta(days=1)
    return bars


def _test_day(after_nine) -> list[Bar]:
    """00:00-06:00 at 2000 +/- 5, 06:00-08:55 at 2006 +/- 5 (Tokyo range 1995-2011 = 16),
    then ``after_nine``: (o, h, l, c) for each bar from 09:00, repeating the last one."""
    bars = []
    for i in range(108):
        ts = TEST_DAY + timedelta(minutes=5 * i)
        level = 2000.0 if i < 72 else 2006.0
        bars.append(_bar(ts, level, level + 5, level - 5, level))
    for i in range(108, 288):
        ohlc = after_nine[min(i - 108, len(after_nine) - 1)]
        bars.append(_bar(TEST_DAY + timedelta(minutes=5 * i), *ohlc))
    return bars


def _run(bars, early_closes=None, strategy=None, starting_cash=100_000):
    bt = Backtest(
        strategy=strategy or static.GoldTokyoBreakoutV2Static(early_closes=early_closes or {}),
        source=InMemorySource(_gold(), {M5: bars}),
        symbol=SYMBOL,
        timeframes=[M5],
        start=bars[0].timestamp,
        end=bars[-1].timestamp,
        starting_cash=starting_cash,
        cost_model=_zero_costs(),
        params={"width_ratio": 1.0},
    )
    return bt.run()


def test_breakout_in_the_0900_bar_fills_at_the_level():
    # Range 16, ATR 10: buy stop 2011 + 0.02*10 = 2011.2, SL 2011.2 - 10 = 2001.2,
    # size 1% of 100k / (10 * 100 oz) = 1.00 lot. Price then holds 2014 until the flatten.
    bars = _history() + _test_day([(2006, 2015, 2001.5, 2014), (2014, 2015, 2013, 2014)])

    trades = [t for t in _run(bars).trades if t.entry_time >= TEST_DAY]

    assert len(trades) == 1
    t = trades[0]
    assert t.side.name == "BUY"
    assert t.entry_time == TEST_DAY + timedelta(hours=9)
    assert t.entry_price == 2011.2
    assert t.stop_loss == 2001.2
    assert t.size == 1.0
    assert t.exit_time == TEST_DAY + timedelta(hours=20, minutes=50)
    assert t.exit_price == 2014


def test_no_trade_when_the_tokyo_range_is_below_the_median():
    # Tokyo 2000 +/- 4.9: range 9.8 < median 10. A later breakout to 2020 must not trade.
    tokyo = [
        _bar(TEST_DAY + timedelta(minutes=5 * i), 2000, 2004.9, 1995.1, 2000) for i in range(108)
    ]
    rally = [
        _bar(TEST_DAY + timedelta(minutes=5 * i), 2000, 2020, 1999, 2019) for i in range(108, 288)
    ]

    trades = [t for t in _run(_history() + tokyo + rally).trades if t.entry_time >= TEST_DAY]

    assert trades == []


def test_no_trade_with_fewer_than_15_previous_ranges():
    # Only 10 weekdays before the test day, part of them spent warming up ATR(4h, 20).
    short = [b for b in _history() if b.timestamp >= TEST_DAY - timedelta(days=14)]
    bars = short + _test_day([(2006, 2015, 2001.5, 2014), (2014, 2015, 2013, 2014)])

    trades = [t for t in _run(bars).trades if t.entry_time >= TEST_DAY]

    assert trades == []


def test_levels_use_the_atr_frozen_at_plan_time():
    # 10:00-14:00 swings 1995-2011 inside the two stops: that bucket's true range is 16, so the
    # live ATR becomes (19 * 10 + 16) / 20 = 10.3 at 14:00. The 15:00 fill still uses ATR 10.
    calm = (2006, 2007, 2005, 2006)
    swing = (2003, 2011, 1995, 2003)
    after_nine = [calm] * 12 + [swing] * 48 + [calm] * 12 + [(2006, 2013, 2005, 2012),
                                                             (2012, 2012.5, 2011.5, 2012)]

    trades = [t for t in _run(_history() + _test_day(after_nine)).trades
              if t.entry_time >= TEST_DAY]

    assert [(t.entry_time, t.entry_price, t.stop_loss, t.size) for t in trades] == [
        (TEST_DAY + timedelta(hours=15), 2011.2, 2001.2, 1.0)
    ]


def test_stop_is_shortened_to_stay_inside_the_opposite_entry_and_size_ignores_it():
    # Range 10 = median 10 (passes) = ATR 10. d = min(10, 10 + 2*0.2 - 0.05*10) = 9.9, so the
    # long's SL is 2005.2 - 9.9 = 1995.3, above the sell stop at 1994.8 (a full 10 would put it at
    # 1995.2). Size still from the full ATR: 1.00 lot, not 1% / 9.9 = 1.01.
    quiet = _quiet_day(TEST_DAY)[:108]
    after = [(2000, 2006, 1999, 2005.5)] + [(2005.5, 2006, 2005, 2005.5)] * 179
    rest = [_bar(TEST_DAY + timedelta(minutes=5 * (108 + i)), *ohlc) for i, ohlc in enumerate(after)]

    trades = [t for t in _run(_history() + quiet + rest).trades if t.entry_time >= TEST_DAY]

    assert len(trades) == 1
    assert trades[0].entry_price == pytest.approx(2005.2)
    assert trades[0].stop_loss == pytest.approx(1995.3)
    assert trades[0].size == 1.0


def test_long_stops_out_before_the_short_fills_and_each_side_fills_once():
    # Narrow day as above: buy 2005.2 / SL 1995.3, sell 1994.8 / SL 1994.8 + 9.9 = 2004.7.
    # The 10:00 bar crosses both the long's SL and the sell stop; the 12:00 bar rallies through
    # the short's SL and the (already used) buy stop.
    quiet = _quiet_day(TEST_DAY)[:108]
    after = (
        [(2000, 2006, 1999, 2005.5)]
        + [(2000, 2000.5, 1999.5, 2000)] * 11
        + [(2000, 2000.5, 1990, 1991)]
        + [(1991, 1991.5, 1990.5, 1991)] * 23
        + [(1991, 2010, 1990.5, 2009)]
        + [(2009, 2009.5, 2008.5, 2009)] * 143
    )
    rest = [_bar(TEST_DAY + timedelta(minutes=5 * (108 + i)), *ohlc) for i, ohlc in enumerate(after)]

    trades = [t for t in _run(_history() + quiet + rest).trades if t.entry_time >= TEST_DAY]

    got = [(t.side.name, t.entry_time.hour, round(t.entry_price, 2), t.exit_time.hour,
            round(t.exit_price, 2), t.exit_reason) for t in trades]
    assert got == [
        ("BUY", 9, 2005.2, 10, 1995.3, "stop_loss"),
        ("SELL", 10, 1994.8, 12, 2004.7, "stop_loss"),
    ]


def _late_breakout(bar_index):
    """Calm 2006 +/- 1 from 09:00, then a breakout through 2011.2 on bar ``bar_index`` after 09:00."""
    calm, up = (2006, 2007, 2005, 2006), (2006, 2013, 2005, 2012)
    return [calm] * bar_index + [up, (2012, 2012.5, 2011.5, 2012)]


def test_the_2000_bar_is_the_last_that_can_fill():
    after_nine = _late_breakout(132)                     # 09:00 + 132 * 5 min = 20:00

    trades = [t for t in _run(_history() + _test_day(after_nine)).trades
              if t.entry_time >= TEST_DAY]

    assert [(t.entry_time, t.exit_time) for t in trades] == [
        (TEST_DAY + timedelta(hours=20), TEST_DAY + timedelta(hours=20, minutes=50))
    ]


def test_orders_expire_before_the_2005_bar():
    after_nine = _late_breakout(133)                     # 20:05

    trades = [t for t in _run(_history() + _test_day(after_nine)).trades
              if t.entry_time >= TEST_DAY]

    assert trades == []


def test_early_close_day_flattens_at_the_open_of_the_last_bar():
    # Session's last bar opens at 17:00: close sent at the 16:55 bar, filled at the 17:00 open.
    bars = _history() + _test_day([(2006, 2015, 2001.5, 2014), (2014, 2015, 2013, 2014)])

    trades = [t for t in _run(bars, {TEST_DAY.date(): time(17, 0)}).trades
              if t.entry_time >= TEST_DAY]

    assert [(t.exit_time, t.exit_price) for t in trades] == [
        (TEST_DAY + timedelta(hours=17), 2014)
    ]


def test_early_close_day_orders_expire_two_bars_before_the_last_bar():
    # Last bar 17:00 -> the 16:50 bar is the last that can fill; a breakout at 16:55 is too late.
    too_late = _late_breakout(95)                        # 09:00 + 95 * 5 min = 16:55

    trades = [t for t in _run(_history() + _test_day(too_late), {TEST_DAY.date(): time(17, 0)})
              .trades if t.entry_time >= TEST_DAY]

    assert trades == []


def _plan_for(day, bars, **kw):
    strategy = static.GoldTokyoBreakoutV2Static(early_closes={})
    _run(bars, strategy=strategy, **kw)
    return next(p for p in strategy.day_plans if p["date"] == day.date())


def test_day_plan_records_every_input_and_both_orders():
    bars = _history() + _test_day([(2006, 2015, 2001.5, 2014), (2014, 2015, 2013, 2014)])

    plan = _plan_for(TEST_DAY, bars)

    assert plan == {
        "date": TEST_DAY.date(), "tokyo_high": 2011, "tokyo_low": 1995, "bars": 108,
        "range": 16, "history_len": 20, "median": 10, "atr": 10, "equity": 100_000,
        "trade": True, "reason": None,
        "long_entry": pytest.approx(2011.2), "long_sl": pytest.approx(2001.2),
        "short_entry": pytest.approx(1994.8), "short_sl": pytest.approx(2004.8),
        "d": 10, "shortened": False, "lots": 1.0,
    }


def test_size_below_the_minimum_lot_skips_the_day():
    # $500 at 1% over a 10 x 100 oz stop = 0.005 lot, which floors to 0.
    bars = _history() + _test_day([(2006, 2015, 2001.5, 2014), (2014, 2015, 2013, 2014)])
    strategy = static.GoldTokyoBreakoutV2Static(early_closes={})

    result = _run(bars, strategy=strategy, starting_cash=500)

    plan = next(p for p in strategy.day_plans if p["date"] == TEST_DAY.date())
    assert (plan["trade"], plan["reason"], plan["lots"]) == (False, "skipped_min_size", 0.0)
    assert [t for t in result.trades if t.entry_time >= TEST_DAY] == []


def test_day_plan_says_why_there_was_no_trade():
    tokyo = [
        _bar(TEST_DAY + timedelta(minutes=5 * i), 2000, 2004.9, 1995.1, 2000) for i in range(288)
    ]
    narrow = _plan_for(TEST_DAY, _history() + tokyo)
    short = _plan_for(TEST_DAY, [b for b in _history() if b.timestamp >= TEST_DAY - timedelta(days=14)]
                      + _test_day([(2006, 2007, 2005, 2006)]))

    assert (narrow["trade"], narrow["reason"], narrow["range"]) == (False, "narrow_range",
                                                                     pytest.approx(9.8))
    assert (short["trade"], short["reason"]) == (False, "short_history")
