from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from hermes import CryptoPair, SessionCalendar, Stock, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import MultiTimeframeView

M15 = Timeframe.parse("15m")
H1 = Timeframe.parse("1h")
D1 = Timeframe.parse("1d")
ET = ZoneInfo("America/New_York")


def _btc() -> CryptoPair:
    return CryptoPair(
        Symbol("BTCUSDT", "binance"), base_asset="BTC", quote_currency="USDT", tick_size=0.01
    )


def _aapl() -> Stock:
    session = SessionCalendar(timezone=ET, open_time=time(9, 30), close_time=time(16, 0))
    return Stock(Symbol("AAPL", "yfinance"), session=session)


def _bar(dt: datetime, o, h, l, c, v=1.0, tf=M15) -> Bar:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return Bar(dt.astimezone(UTC), tf, o, h, l, c, v)


def _utc(y, mo, d, h, mi, tz=UTC) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz)


# --- validation ------------------------------------------------------------

def test_rejects_non_multiple_timeframe():
    with pytest.raises(ValueError):
        MultiTimeframeView(_btc(), M15, [Timeframe.parse("20m"), M15])  # 20m not x of 15m


def test_series_created_for_each_timeframe():
    view = MultiTimeframeView(_btc(), M15, [H1, M15])
    assert set(view.series.keys()) == {M15, H1}


# --- the user's forming-bar scenario (crypto, UTC) -------------------------

def test_forming_higher_tf_bar():
    """At 12:30 (after the 12:15 bar), the 1h [12:00-13:00) bar is still forming:
    open from 12:00, close = the 12:15 bar's close, running high/low, not closed."""
    view = MultiTimeframeView(_btc(), M15, [H1])
    view.push(_bar(_utc(2023, 1, 2, 12, 0), 15.55, 15.60, 15.50, 15.70))
    view.push(_bar(_utc(2023, 1, 2, 12, 15), 15.70, 15.95, 15.65, 15.90))

    forming = view[H1].forming
    assert forming is not None and forming.is_closed is False
    assert forming.timestamp == _utc(2023, 1, 2, 12, 0)
    assert forming.open == 15.55
    assert forming.close == 15.90
    assert forming.high == 15.95
    assert forming.low == 15.50
    assert forming.volume == 2.0


def test_hour_finalizes_when_complete():
    view = MultiTimeframeView(_btc(), M15, [H1])
    for i, mi in enumerate((0, 15, 30, 45)):
        view.push(_bar(_utc(2023, 1, 2, 12, mi), 10 + i, 11 + i, 9 + i, 10.5 + i))
    # After the 12:45 bar (ends 13:00), the hour is complete.
    assert view[H1].forming is None
    closed = view[H1].closed()
    assert len(closed) == 1 and closed[0].is_closed
    assert closed[0].open == 10.0 and closed[0].close == 13.5

    # A new hour opens on the next bar.
    view.push(_bar(_utc(2023, 1, 2, 13, 0), 20, 21, 19, 20.5))
    assert view[H1].forming is not None
    assert view[H1].forming.timestamp == _utc(2023, 1, 2, 13, 0)


# --- stock session edges ---------------------------------------------------

def test_stock_partial_first_bar():
    """US session opens 09:30; the first hourly bucket is 09:00-10:00 but holds only
    09:30-10:00 of data (partial first bar)."""
    view = MultiTimeframeView(_aapl(), M15, [H1])
    view.push(_bar(_utc(2023, 1, 3, 9, 30, ET), 100, 101, 99, 100.5))
    view.push(_bar(_utc(2023, 1, 3, 9, 45, ET), 100.5, 102, 100, 101.5))

    closed = view[H1].closed()
    assert len(closed) == 1  # finalised at 10:00
    bar = closed[0]
    assert bar.is_closed
    # Labelled 09:00 local, but built only from the two post-open bars.
    assert bar.timestamp.astimezone(ET).hour == 9
    assert bar.timestamp.astimezone(ET).minute == 0
    assert bar.open == 100  # first *available* sub-bar's open
    assert bar.close == 101.5


def test_stock_daily_closes_at_session_end():
    view = MultiTimeframeView(_aapl(), M15, [D1])
    # 09:30 .. 15:45 inclusive = 26 fifteen-minute bars.
    minutes = [(h, m) for h in range(9, 16) for m in (0, 15, 30, 45)]
    minutes = [(h, m) for (h, m) in minutes if (h, m) >= (9, 30) and (h, m) <= (15, 45)]
    for i, (h, m) in enumerate(minutes):
        view.push(_bar(_utc(2023, 1, 3, h, m, ET), 100 + i, 100 + i, 100 + i, 100 + i))
    # The daily bar force-closes at the 16:00 session close.
    assert view[D1].forming is None
    day = view[D1].closed()[0]
    assert day.is_closed
    assert day.timestamp.astimezone(ET).hour == 0  # labelled at local midnight
    assert day.open == 100


def test_stock_hour_does_not_span_overnight():
    view = MultiTimeframeView(_aapl(), M15, [H1])
    # Last bar of day 1 then first bar of day 2 — must be different hourly bars.
    view.push(_bar(_utc(2023, 1, 3, 15, 45, ET), 200, 201, 199, 200.5))  # closes 15-16h day3
    view.push(_bar(_utc(2023, 1, 4, 9, 30, ET), 100, 101, 99, 100.5))    # opens 9-10h day4
    closed = view[H1].closed()
    assert len(closed) == 1
    assert closed[0].timestamp.astimezone(ET).day == 3  # day-1 hour, self-contained
    # Day-2 bar started a fresh forming hour — it did NOT merge into day-1's bar.
    forming = view[H1].forming
    assert forming is not None and forming.close == 100.5
    assert forming.timestamp.astimezone(ET).day == 4
