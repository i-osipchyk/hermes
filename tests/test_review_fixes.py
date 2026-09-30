"""Regression tests for the correctness fixes from the 2026-09-30 review."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from hermes.backtest.result import _annualisation
from hermes.core import Bar, Cfd, SessionCalendar, Stock, Symbol, Timeframe
from hermes.data.cache import checked_through, complete_bars
from hermes.execution import Account, SimulatedVenue
from hermes.execution.costs import (
    CostModel,
    FinancingModel,
    PerShareCommission,
    SlippageModel,
    SpreadModel,
)
from hermes.execution.order import Order, OrderType, Side
from hermes.execution.trade import Trade
from hermes.strategy.strategy import Strategy

D1 = Timeframe.parse("1d")
M5 = Timeframe.parse("5m")
US = SessionCalendar(
    timezone=ZoneInfo("America/New_York"), open_time=time(9, 30), close_time=time(16, 0)
)
CRYPTO = SessionCalendar(timezone=ZoneInfo("UTC"), weekdays=(0, 1, 2, 3, 4, 5, 6))
FREE = CostModel(
    PerShareCommission(0.0), SpreadModel(0.0), SlippageModel(0.0), FinancingModel(0.0)
)


def _stock():
    return Stock(Symbol("AAPL", "yfinance"), session=US)


def _venue(inst=None, cash=100_000.0):
    inst = inst or _stock()
    return SimulatedVenue(inst, Account(cash), FREE)


# --- intrabar entries must not book an unreachable take-profit --------------

def test_limit_entry_cannot_take_profit_on_its_own_fill_bar():
    """A buy limit filling near the low must not "take profit" at a high that may
    have printed BEFORE the fill — the bar's path is unknown."""
    v = _venue()
    v.on_base_bar(Bar(datetime(2024, 1, 2, tzinfo=UTC), D1, 100, 100, 100, 100, 1))
    v.submit(Order(instrument=v.instrument, side=Side.BUY, size=10,
                   type=OrderType.LIMIT, limit_price=95.0, take_profit=104.0))
    # opens 100, spikes to 105 first, then falls to 94: the limit fills at 95.
    v.on_base_bar(Bar(datetime(2024, 1, 3, tzinfo=UTC), D1, 100, 105, 94, 96, 1))

    assert v.closed_trades == []
    assert len(v.open_trades()) == 1
    assert v.open_trades()[0].entry_price == 95.0


def test_limit_entry_still_stops_out_on_its_own_fill_bar():
    """The conservative side is kept: a stop-loss touched on the entry bar fires."""
    v = _venue()
    v.on_base_bar(Bar(datetime(2024, 1, 2, tzinfo=UTC), D1, 100, 100, 100, 100, 1))
    v.submit(Order(instrument=v.instrument, side=Side.BUY, size=10,
                   type=OrderType.LIMIT, limit_price=95.0, stop_loss=93.0))
    v.on_base_bar(Bar(datetime(2024, 1, 3, tzinfo=UTC), D1, 100, 105, 92, 96, 1))

    assert [t.exit_reason for t in v.closed_trades] == ["stop_loss"]


def test_market_entry_may_still_take_profit_on_its_fill_bar():
    """A market order fills at the open, so the whole bar lies after the fill."""
    v = _venue()
    v.on_base_bar(Bar(datetime(2024, 1, 2, tzinfo=UTC), D1, 100, 100, 100, 100, 1))
    v.submit(Order(instrument=v.instrument, side=Side.BUY, size=10,
                   type=OrderType.MARKET, take_profit=104.0))
    v.on_base_bar(Bar(datetime(2024, 1, 3, tzinfo=UTC), D1, 100, 105, 99, 101, 1))

    assert [t.exit_reason for t in v.closed_trades] == ["take_profit"]


# --- modify() must not clear the level you did not mention ------------------

def test_modify_leaves_unmentioned_level_untouched():
    v = _venue()
    t = Trade(instrument=v.instrument, side=Side.BUY, size=1, entry_price=100.0,
              entry_time=datetime(2024, 1, 2, tzinfo=UTC), stop_loss=95.0, take_profit=110.0)
    strat = type("S", (), {"venue": v})()

    Strategy.modify(strat, t, stop_loss=100.0)
    assert (t.stop_loss, t.take_profit) == (100.0, 110.0)

    Strategy.modify(strat, t, take_profit=None)   # explicit None still clears
    assert (t.stop_loss, t.take_profit) == (100.0, None)


# --- a closing order must not be blocked by the margin gate -----------------

def test_reducing_order_is_affordable_when_fully_invested():
    inst = _stock()
    v = SimulatedVenue(inst, Account(1_000.0), FREE)
    v.on_base_bar(Bar(datetime(2024, 1, 2, tzinfo=UTC), D1, 100, 100, 100, 100, 1))
    v.submit(Order(instrument=inst, side=Side.BUY, size=10, type=OrderType.MARKET))
    v.on_base_bar(Bar(datetime(2024, 1, 3, tzinfo=UTC), D1, 100, 100, 100, 100, 1))
    assert len(v.open_trades()) == 1          # all 1_000 of equity deployed

    closing = v.submit(Order(instrument=inst, side=Side.SELL, size=10, type=OrderType.MARKET))
    assert closing.status.value == "working"  # not rejected for "insufficient margin"


# --- annualisation must reflect the session, not continuous time ------------

def test_bars_per_year_is_session_aware():
    assert US.bars_per_year(M5) == pytest.approx(20_350, rel=0.02)
    assert US.bars_per_year(D1) == pytest.approx(261, rel=0.02)
    assert CRYPTO.bars_per_year(Timeframe.parse("1h")) == pytest.approx(8_766, rel=0.01)


def test_annualisation_prefers_the_supplied_rate():
    curve = [(datetime(2024, 1, 2, 14, 30, tzinfo=UTC) + timedelta(minutes=5 * i), 1.0)
             for i in range(200)]
    # Without help it falls back to median spacing (assumes 24/7).
    assert _annualisation(curve) == pytest.approx(105_192, rel=0.01)
    # With the session's real rate it uses that instead.
    assert _annualisation(curve, US.bars_per_year(M5)) == pytest.approx(20_350, rel=0.02)


# --- cache must not seal a range it only partly fetched ---------------------

def test_checked_through_stops_at_the_last_bar_actually_fetched():
    start = datetime(2024, 1, 2, tzinfo=UTC)
    end = start + timedelta(hours=12)
    bars = [Bar(start + timedelta(hours=4 * i), Timeframe.parse("4h"), 1, 2, 0.5, 1.5, 1)
            for i in range(2)]          # covers only the first 8 hours
    assert checked_through(bars, Timeframe.parse("4h"), start, end) == start + timedelta(hours=8)
    # An empty fetch legitimately confirms the whole span (weekend/holiday).
    assert checked_through([], Timeframe.parse("4h"), start, end) == end


def test_complete_bars_drops_the_still_forming_bar():
    now = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)
    bars = [Bar(now - timedelta(hours=2), Timeframe.parse("1h"), 1, 2, 0.5, 1.5, 1),
            Bar(now - timedelta(hours=1), Timeframe.parse("1h"), 1, 2, 0.5, 1.5, 1),
            Bar(now, Timeframe.parse("1h"), 1, 2, 0.5, 1.5, 1)]   # still forming
    kept = complete_bars(bars, Timeframe.parse("1h"), now=now)
    assert [b.timestamp for b in kept] == [b.timestamp for b in bars[:2]]


# --- CFD order size steps in lots, not in price ticks -----------------------

def test_cfd_size_rounds_to_volume_step_not_tick_size():
    nas = Cfd(Symbol("NAS100", "pepperstone"), session=US,
              tick_size=0.1, lot_size=100.0, leverage=30.0, volume_step=0.01)
    # 0.13 lots survives; under the old tick_size rounding it floored to 0.1.
    assert nas.to_native_units(0.137) == pytest.approx(0.13)
    # And a small size no longer floors all the way to zero.
    assert nas.to_native_units(0.04) == pytest.approx(0.04)


# --- setup() is idempotent --------------------------------------------------

def test_run_setup_does_not_accumulate_indicators():
    from hermes.indicators import SMA

    class S(Strategy):
        def setup(self):
            self.use(SMA(D1, 5))
            self.use_reference("SPY")

        def on_bar(self, bar):
            pass

    s = S()
    s.run_setup()
    s.run_setup()
    assert len(s.registered_indicators) == 1
    assert len(s.references) == 1


# --- an overnight session is not permanently closed -------------------------

def test_overnight_session_is_open_across_midnight():
    overnight = SessionCalendar(
        timezone=ZoneInfo("America/New_York"), open_time=time(17, 0), close_time=time(16, 0)
    )
    assert overnight.is_overnight
    ny = ZoneInfo("America/New_York")
    assert overnight.is_open(datetime(2024, 1, 2, 18, 0, tzinfo=ny).astimezone(UTC))
    assert overnight.is_open(datetime(2024, 1, 2, 3, 0, tzinfo=ny).astimezone(UTC))
    assert not overnight.is_open(datetime(2024, 1, 2, 16, 30, tzinfo=ny).astimezone(UTC))


# --- forex weekly buckets start at the Sunday rollover, not the Monday ------

def test_weekly_bucket_groups_sunday_evening_with_its_own_week():
    from hermes.data.aggregation import bucket_bounds

    W1 = Timeframe.parse("1w")
    ny = ZoneInfo("America/New_York")
    fx = SessionCalendar(timezone=ny, weekdays=(0, 1, 2, 3, 4, 6), day_anchor=time(17, 0))

    sunday_eve = datetime(2024, 1, 7, 18, 0, tzinfo=ny).astimezone(UTC)
    wednesday = datetime(2024, 1, 10, 12, 0, tzinfo=ny).astimezone(UTC)
    # Both belong to the SAME trading week, which opens Sunday 17:00 NY.
    assert bucket_bounds(sunday_eve, W1, fx) == bucket_bounds(wednesday, W1, fx)
    assert bucket_bounds(wednesday, W1, fx)[0].astimezone(ny) == datetime(
        2024, 1, 7, 17, 0, tzinfo=ny
    )


def test_weekly_bucket_still_starts_monday_without_a_day_anchor():
    from hermes.data.aggregation import bucket_bounds

    W1 = Timeframe.parse("1w")
    ny = ZoneInfo("America/New_York")
    wednesday = datetime(2024, 1, 10, 12, 0, tzinfo=ny).astimezone(UTC)
    assert bucket_bounds(wednesday, W1, US)[0].astimezone(ny) == datetime(
        2024, 1, 8, 0, 0, tzinfo=ny
    )


# --- a portfolio equity curve keeps bar resolution --------------------------

def test_portfolio_curve_keeps_intraday_resolution():
    from hermes.backtest.portfolio import _dedupe_timestamps

    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    # Two legs each writing a point per 5m bar on the same day.
    curve = []
    for i in range(78):
        ts = t0 + timedelta(minutes=5 * i)
        curve.append((ts, 10_000.0 + i))
        curve.append((ts, 10_000.0 + i))
    out = _dedupe_timestamps(curve)

    assert len(out) == 78                # not collapsed to one point per day
    assert out[0] == (t0, 10_000.0)      # curve still starts at starting equity
    assert out == sorted(out)
