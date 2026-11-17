"""F16 (docs/audit-2026-11.md, ADR-0016): raw bars + corporate-action events, adjusted at
read time. Seams: the adjustment functions, ``TiingoSource.history`` and ``data_epoch``.
All offline: the provider call is replaced by a fake that can learn about a dividend
*after* earlier bars were cached — the exact situation that used to stitch two bases."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from hermes.core import Bar, Symbol, Timeframe
from hermes.data import BarCache
from hermes.data.adjustments import (
    AdjustmentEvent,
    AdjustmentStore,
    adjust_bars,
    yfinance_rows_to_raw,
)
from hermes.data.sources import TiingoSource

D1 = Timeframe.parse("1d")
DAY0 = datetime(2024, 1, 1, tzinfo=UTC)


def _day(n: int) -> datetime:
    return DAY0 + timedelta(days=n)


def _bars(closes, volume=1000.0):
    return [Bar(_day(i), D1, c, c, c, c, volume) for i, c in enumerate(closes)]


# --- the maths, against worked literals -----------------------------------------

def test_a_dividend_scales_earlier_prices_only_in_total_mode():
    ev = [AdjustmentEvent(date(2024, 1, 4), dividend=1.0, prev_close=100.0)]   # factor 0.99
    raw = _bars([100.0] * 6)
    total = adjust_bars(raw, ev, "total")
    assert [round(b.close, 6) for b in total] == [99.0, 99.0, 99.0, 100.0, 100.0, 100.0]
    assert [b.close for b in adjust_bars(raw, ev, "split")] == [100.0] * 6
    assert [b.close for b in adjust_bars(raw, ev, "none")] == [100.0] * 6


def test_a_split_divides_earlier_prices_and_multiplies_earlier_volume():
    ev = [AdjustmentEvent(date(2024, 1, 4), split=2.0)]
    out = adjust_bars(_bars([100.0, 100.0, 100.0, 50.0, 50.0]), ev, "split")
    assert [b.close for b in out] == [50.0, 50.0, 50.0, 50.0, 50.0]
    assert [b.volume for b in out] == [2000.0, 2000.0, 2000.0, 1000.0, 1000.0]
    assert [b.close for b in adjust_bars(_bars([100.0, 50.0]), ev, "none")] == [100.0, 50.0]


def test_split_and_dividend_compose_only_for_bars_before_each_event():
    ev = [AdjustmentEvent(date(2024, 1, 3), dividend=1.0, prev_close=100.0),   # x0.99
          AdjustmentEvent(date(2024, 1, 5), split=2.0)]
    out = adjust_bars(_bars([100.0] * 6), ev, "total")
    # bar 1 (Jan 2): both events are later -> 100/2*0.99; bar 3 (Jan 4): only the split
    assert out[1].close == pytest.approx(49.5)
    assert out[3].close == pytest.approx(50.0)
    assert out[5].close == pytest.approx(100.0)


def test_a_dividend_without_a_prior_close_is_not_adjusted_rather_than_guessed():
    ev = [AdjustmentEvent(date(2024, 1, 4), dividend=1.0, prev_close=None)]
    assert [b.close for b in adjust_bars(_bars([100.0] * 5), ev, "total")] == [100.0] * 5


def test_no_events_returns_the_bars_unchanged():
    raw = _bars([1.0, 2.0, 3.0])
    assert adjust_bars(raw, [], "total") == raw


def test_an_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="adjust"):
        adjust_bars(_bars([1.0]), [], "bogus")


# --- yfinance: split-adjusted-as-of-now rows become truly raw ---------------------

def test_yfinance_rows_are_unsplit_back_to_the_prices_actually_printed():
    # 2:1 split on Jan 3. Yahoo shows pre-split prices already halved.
    rows = [
        {"ts": _day(0), "open": 50, "high": 50, "low": 50, "close": 50, "volume": 2000, "dividend": 0.0},
        {"ts": _day(1), "open": 50, "high": 50, "low": 50, "close": 50, "volume": 2000, "dividend": 0.25},
        {"ts": _day(2), "open": 50, "high": 50, "low": 50, "close": 50, "volume": 1000, "dividend": 0.0},
        {"ts": _day(3), "open": 50, "high": 50, "low": 50, "close": 50, "volume": 1000, "dividend": 0.5},
    ]
    bars, events = yfinance_rows_to_raw(rows, D1, splits=[(date(2024, 1, 3), 2.0)])
    assert [b.close for b in bars] == [100.0, 100.0, 50.0, 50.0]
    assert [b.volume for b in bars] == [1000.0, 1000.0, 1000.0, 1000.0]
    by_date = {e.ex_date: e for e in events}
    assert by_date[date(2024, 1, 3)].split == 2.0
    # dividend 0.25 (split-adjusted) was really 0.5 per pre-split share, on a raw 100 close
    assert by_date[date(2024, 1, 2)].dividend == pytest.approx(0.5)
    assert by_date[date(2024, 1, 2)].prev_close == 100.0
    assert by_date[date(2024, 1, 4)].dividend == pytest.approx(0.5)
    assert by_date[date(2024, 1, 4)].prev_close == 50.0


# --- the event store ---------------------------------------------------------------

def test_store_merges_by_date_and_keeps_a_known_prior_close(tmp_path):
    st = AdjustmentStore(tmp_path)
    st.merge("t", "AAA", [AdjustmentEvent(date(2024, 1, 4), dividend=1.0, prev_close=100.0)])
    st.merge("t", "AAA", [AdjustmentEvent(date(2024, 1, 4), dividend=1.0, prev_close=None),
                          AdjustmentEvent(date(2024, 2, 1), split=2.0)])
    got = st.load("t", "AAA")
    assert [(e.ex_date, e.prev_close) for e in got] == [(date(2024, 1, 4), 100.0),
                                                        (date(2024, 2, 1), None)]


def test_digest_changes_only_when_the_events_change(tmp_path):
    st = AdjustmentStore(tmp_path)
    empty = st.digest("t", "AAA")
    st.merge("t", "AAA", [AdjustmentEvent(date(2024, 1, 4), dividend=1.0, prev_close=100.0)])
    one = st.digest("t", "AAA")
    st.merge("t", "AAA", [AdjustmentEvent(date(2024, 1, 4), dividend=1.0, prev_close=100.0)])
    assert one != empty and st.digest("t", "AAA") == one


# --- F16 itself: a dividend learned AFTER earlier bars were cached ------------------

def _row(n, close=100.0, div=0.0, split=1.0):
    d = _day(n).isoformat()
    return {"date": d, "open": close, "high": close, "low": close, "close": close,
            "volume": 1000.0, "divCash": div, "splitFactor": split,
            # the provider's own adjusted fields differ — they must NOT be what is cached
            "adjOpen": 1.0, "adjHigh": 1.0, "adjLow": 1.0, "adjClose": 1.0, "adjVolume": 1.0}


class FakeTiingo(TiingoSource):
    """Provider whose knowledge grows: ``available`` is what it would return today."""

    def __init__(self, cache, **kw):
        super().__init__(api_key="x", cache=cache, **kw)
        self.available: list[dict] = []
        self.calls: list[tuple] = []

    def _get_rows(self, ticker, start, end, resample):
        self.calls.append((start, end))
        return [r for r in self.available
                if start.date() <= datetime.fromisoformat(r["date"]).date() <= end.date()]


def _hist(src, a, b):
    inst = src.get_instrument(Symbol("AAA", "tiingo"))
    return src.history(inst, D1, _day(a), _day(b))


def test_a_dividend_that_arrives_later_does_not_create_a_fake_gap(tmp_path):
    src = FakeTiingo(BarCache(tmp_path))
    src.available = [_row(i) for i in range(0, 5)]                       # days 0-4, no events
    first = _hist(src, 0, 4)
    assert [b.close for b in first] == [100.0] * 5

    # Later the provider reports an ex-dividend on day 6 (1.00 on a 100 close).
    src.available += [_row(5), _row(6, div=1.0), _row(7), _row(8), _row(9)]
    full = _hist(src, 0, 9)
    closes = [round(b.close, 6) for b in full]
    # One continuous basis: everything before the ex-date is x0.99, nothing jumps at the
    # join between the old cache (days 0-4) and the new fetch (days 5-9).
    assert closes == [99.0] * 6 + [100.0] * 4
    assert max(abs(closes[i + 1] / closes[i] - 1) for i in range(4, 5)) == 0.0


def test_raw_prices_not_the_providers_adjusted_fields_are_what_gets_cached(tmp_path):
    src = FakeTiingo(BarCache(tmp_path), adjust="none")
    src.available = [_row(i, close=100.0 + i) for i in range(5)]
    assert [b.close for b in _hist(src, 0, 4)] == [100.0, 101.0, 102.0, 103.0, 104.0]


def test_data_epoch_moves_when_a_corporate_action_arrives_and_is_stable_otherwise(tmp_path):
    src = FakeTiingo(BarCache(tmp_path))
    src.available = [_row(i) for i in range(5)]
    _hist(src, 0, 4)
    e0 = src.data_epoch("AAA")
    assert src.data_epoch("AAA") == e0
    src.available += [_row(5), _row(6, div=1.0)]
    _hist(src, 0, 6)
    assert src.data_epoch("AAA") != e0


def test_data_epoch_names_the_adjustment_basis(tmp_path):
    a = FakeTiingo(BarCache(tmp_path / "a"), adjust="total").data_epoch("AAA")
    b = FakeTiingo(BarCache(tmp_path / "b"), adjust="split").data_epoch("AAA")
    assert a != b


def test_a_repeat_request_does_not_refetch(tmp_path):
    src = FakeTiingo(BarCache(tmp_path))
    src.available = [_row(i) for i in range(5)]
    _hist(src, 0, 4)
    n = len(src.calls)
    _hist(src, 0, 4)
    assert len(src.calls) == n


def test_the_first_bar_of_a_fetch_still_gets_its_dividend_adjustment(tmp_path):
    """The ex-date is the very first requested day: the prior close must still be found
    (the fetch is padded back), so the dividend is adjusted rather than silently dropped."""
    src = FakeTiingo(BarCache(tmp_path))
    src.available = [_row(0), _row(1), _row(2, div=1.0), _row(3)]
    out = _hist(src, 2, 3)
    assert [b.close for b in out] == [100.0, 100.0]       # nothing after the ex-date to adjust
    full = _hist(src, 0, 3)
    assert [round(b.close, 6) for b in full] == [99.0, 99.0, 100.0, 100.0]
