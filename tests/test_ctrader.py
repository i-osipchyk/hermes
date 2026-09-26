"""Tests for the cTrader-specific normalization: timestamp/price decode, the
whole-hour offset correction, forex day-anchored bucketing, rebuilding higher
timeframes from 1h, env-var credential defaults, and trendbar pagination."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hermes import Cfd, CTraderSource, PepperstoneSource, Symbol, Timeframe
from hermes.core import Bar
from hermes.data.aggregation import bucket_bounds
from hermes.data.sources.ctrader_source import (
    _CFD_SESSION,
    prev_page_end,
    resample_from_hourly,
    trendbar_to_bar,
)

H1 = Timeframe.parse("1h")
H4 = Timeframe.parse("4h")
NY = ZoneInfo("America/New_York")


def _cfd():
    return Cfd(Symbol("EURUSD", "pepperstone"), session=_CFD_SESSION, tick_size=1e-5,
               lot_size=100_000, leverage=30)


def test_trendbar_decode_prices():
    # low=110000 -> 1.10000; open=+500 -> 1.10500; high=+800; close=+200
    bar = trendbar_to_bar(110_000, 500, 800, 200, 1234, ts_minutes=0, timeframe=H1)
    assert bar.low == 1.10
    assert round(bar.open, 5) == 1.105
    assert round(bar.high, 5) == 1.108
    assert round(bar.close, 5) == 1.102
    assert bar.volume == 1234


def test_trendbar_timestamp_utc_and_offset():
    # 28229760 minutes since epoch = 2023-09-01 00:00 UTC.
    minutes = int(datetime(2023, 9, 1, tzinfo=UTC).timestamp() // 60)
    assert trendbar_to_bar(1, 0, 0, 0, 1, minutes, H1).timestamp == datetime(2023, 9, 1, tzinfo=UTC)
    # A +60 offset means the raw stamps run 1h ahead of UTC; correct them back.
    shifted = trendbar_to_bar(1, 0, 0, 0, 1, minutes, H1, offset_minutes=60)
    assert shifted.timestamp == datetime(2023, 8, 31, 23, 0, tzinfo=UTC)


def test_forex_4h_anchors_to_17_ny():
    """A 4h CFD bucket must start on the 17:00 NY grid (…13,17,21,01…), not the
    midnight grid (…12,16,20,00…)."""
    # 2023-09-05 19:30 NY -> falls in the 17:00-21:00 NY bucket.
    ts = datetime(2023, 9, 5, 19, 30, tzinfo=NY).astimezone(UTC)
    open_utc, close_utc = bucket_bounds(ts, H4, _CFD_SESSION)
    assert open_utc.astimezone(NY).hour == 17
    assert close_utc.astimezone(NY).hour == 21


def test_daily_anchor_differs_from_midnight():
    ts = datetime(2023, 9, 5, 19, 30, tzinfo=NY).astimezone(UTC)
    open_utc, _ = bucket_bounds(ts, Timeframe.parse("1d"), _CFD_SESSION)
    # Forex "day" for a 19:30 NY bar started at 17:00 NY the same evening.
    assert open_utc.astimezone(NY).hour == 17
    assert open_utc.astimezone(NY).day == 5


def test_resample_from_hourly_builds_aligned_4h():
    cfd = _cfd()
    # 8 consecutive 1h bars starting 17:00 NY -> exactly two 17:00-anchored 4h bars.
    start = datetime(2023, 9, 5, 17, 0, tzinfo=NY).astimezone(UTC)
    bars_1h = [
        Bar(start + timedelta(hours=i), H1, 1 + i, 2 + i, 0 + i, 1 + i, 1.0) for i in range(8)
    ]
    four_h = resample_from_hourly(bars_1h, cfd, H4)
    assert len(four_h) == 2
    assert all(b.is_closed for b in four_h)
    assert four_h[0].timestamp.astimezone(NY).hour == 17
    assert four_h[0].open == bars_1h[0].open       # first hour's open
    assert four_h[0].close == bars_1h[3].close     # 4th hour's close
    assert four_h[0].high == max(b.high for b in bars_1h[:4])


def _clear_ctrader_env(monkeypatch):
    for name in (
        "CTRADER_CLIENT_ID",
        "CTRADER_CLIENT_SECRET",
        "CTRADER_ACCESS_TOKEN",
        "CTRADER_ACCOUNT_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_credentials_default_from_environment(monkeypatch):
    _clear_ctrader_env(monkeypatch)
    monkeypatch.setenv("CTRADER_CLIENT_ID", "id-from-env")
    monkeypatch.setenv("CTRADER_CLIENT_SECRET", "secret-from-env")
    monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "token-from-env")
    monkeypatch.setenv("CTRADER_ACCOUNT_ID", "42")

    source = CTraderSource()
    assert source.client_id == "id-from-env"
    assert source.client_secret == "secret-from-env"
    assert source.access_token == "token-from-env"
    assert source.account_id == 42
    # Pepperstone is just cTrader under another name; same env defaults apply.
    assert PepperstoneSource().account_id == 42


def test_explicit_credentials_override_environment(monkeypatch):
    _clear_ctrader_env(monkeypatch)
    monkeypatch.setenv("CTRADER_CLIENT_ID", "id-from-env")
    monkeypatch.setenv("CTRADER_ACCOUNT_ID", "42")

    source = CTraderSource(client_id="explicit-id", account_id=7)
    assert source.client_id == "explicit-id"
    assert source.account_id == 7


def test_missing_credentials_raise_on_connect(monkeypatch):
    _clear_ctrader_env(monkeypatch)
    source = CTraderSource()
    with pytest.raises(RuntimeError, match="requires client_id"):
        source._connection()


def test_prev_page_end_continues_on_short_non_empty_page():
    # A short but non-empty page may have older data before it (calendar gaps).
    page = [{"utcTimestampInMinutes": 1000}]
    next_end = prev_page_end(page, from_ms=0, period_seconds=3600)
    assert next_end == 1000 * 60_000 - 3600 * 1000


def test_prev_page_end_stops_when_from_reached():
    # If stepping back would land at or before from_ms, stop.
    page = [{"utcTimestampInMinutes": ts} for ts in range(1000, 1000 + 1000)]
    # from_ms sits right at the first bar: next_end = 1000*60000 - 3600000 <= from_ms
    from_ms = 1000 * 60_000
    assert prev_page_end(page, from_ms=from_ms, period_seconds=3600) is None


def test_prev_page_end_steps_back_past_first_bar():
    page = [{"utcTimestampInMinutes": ts} for ts in range(1000, 2000)]
    # first bar at minute 1000 → next_end = 1000*60000 - 3600*1000
    next_end = prev_page_end(page, from_ms=0, period_seconds=3600)
    assert next_end == 1000 * 60_000 - 3600 * 1000


def test_prev_page_end_empty_page_stops_pagination():
    assert prev_page_end([], from_ms=0, period_seconds=3600) is None
