"""Tests for ConstituentCalendar, TiingoSource, and UniverseBacktest."""

from __future__ import annotations

import io
import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes import (
    CryptoPair,
    Stock,
    Strategy,
    Symbol,
    Timeframe,
    UniverseBacktest,
    UniverseResult,
)
from hermes.core import Bar
from hermes.data import ConstituentCalendar, InMemorySource, TiingoSource

D1 = Timeframe.parse("1d")
T0 = datetime(2020, 1, 2, tzinfo=UTC)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_snapshot_csv(rows: list[tuple[str, str]]) -> Path:
    """Write a temporary snapshot CSV and return its Path (uses tmp_path via fixture)."""
    lines = ["date,tickers"] + [f"{d},{t}" for d, t in rows]
    return "\n".join(lines)


def _make_changes_csv(rows: list[tuple[str, str, str]]) -> str:
    lines = ["date,ticker,action"] + [f"{d},{t},{a}" for d, t, a in rows]
    return "\n".join(lines)


def _cal_from_str(csv_str: str, fmt: str = "snapshot") -> ConstituentCalendar:
    """Build a ConstituentCalendar from a CSV string without touching the filesystem."""
    import csv as csv_mod
    if fmt == "snapshot":
        reader = csv_mod.DictReader(io.StringIO(csv_str))
        snapshots = []
        for row in reader:
            d = date.fromisoformat(row["date"].strip())
            raw = row["tickers"].strip().strip('"')
            tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
            snapshots.append((d, tickers))
        return ConstituentCalendar(snapshots)
    else:
        reader = csv_mod.DictReader(io.StringIO(csv_str))
        events = []
        for row in reader:
            d = date.fromisoformat(row["date"].strip())
            ticker = row["ticker"].strip().upper()
            action = row["action"].strip().lower()
            events.append((d, ticker, action))
        events.sort(key=lambda x: x[0])
        current: set[str] = set()
        snapshots = []
        prev_date = None
        for d, ticker, action in events:
            if prev_date is not None and d != prev_date:
                snapshots.append((prev_date, sorted(current)))
            if action == "add":
                current.add(ticker)
            else:
                current.discard(ticker)
            prev_date = d
        if current and prev_date is not None:
            snapshots.append((prev_date, sorted(current)))
        return ConstituentCalendar(snapshots)


# Synthetic calendar:
# 2019-01-01: AAPL, MSFT, ENRN
# 2019-07-01: AAPL, MSFT, AMZN  (ENRN removed, AMZN added)
# 2020-01-01: AAPL, MSFT, AMZN, TSLA  (TSLA added)
_SNAPSHOT_CSV = textwrap.dedent("""\
    date,tickers
    2019-01-01,"AAPL,MSFT,ENRN"
    2019-07-01,"AAPL,MSFT,AMZN"
    2020-01-01,"AAPL,MSFT,AMZN,TSLA"
""")


def _make_cal() -> ConstituentCalendar:
    return _cal_from_str(_SNAPSHOT_CSV)


# ─────────────────────────────────────────────────────────────────────────────
# ConstituentCalendar — symbols_on
# ─────────────────────────────────────────────────────────────────────────────

class TestSymbolsOn:
    def test_exact_snapshot_date(self):
        cal = _make_cal()
        members = cal.symbols_on(date(2019, 1, 1))
        assert set(members) == {"AAPL", "MSFT", "ENRN"}

    def test_mid_period_uses_earlier_snapshot(self):
        cal = _make_cal()
        # Between 2019-01-01 and 2019-07-01 → uses Jan snapshot
        members = cal.symbols_on(date(2019, 4, 15))
        assert set(members) == {"AAPL", "MSFT", "ENRN"}

    def test_after_second_snapshot(self):
        cal = _make_cal()
        members = cal.symbols_on(date(2019, 10, 1))
        assert "AMZN" in members
        assert "ENRN" not in members

    def test_before_all_snapshots_returns_empty(self):
        cal = _make_cal()
        # No data available before the first snapshot (2019-01-01).
        members = cal.symbols_on(date(2010, 1, 1))
        assert members == []

    def test_accepts_datetime(self):
        cal = _make_cal()
        members = cal.symbols_on(datetime(2019, 7, 1, tzinfo=UTC))
        assert "AMZN" in members


# ─────────────────────────────────────────────────────────────────────────────
# ConstituentCalendar — ever_member
# ─────────────────────────────────────────────────────────────────────────────

class TestEverMember:
    def test_full_range_includes_removed_and_added(self):
        cal = _make_cal()
        universe = cal.ever_member(date(2019, 1, 1), date(2020, 12, 31))
        assert "ENRN" in universe   # removed mid-period, still included
        assert "TSLA" in universe   # added in 2020
        assert "AAPL" in universe

    def test_narrow_range_before_enrn_removal(self):
        cal = _make_cal()
        universe = cal.ever_member(date(2019, 1, 1), date(2019, 6, 30))
        assert "ENRN" in universe
        assert "AMZN" not in universe  # AMZN joined Jul 1

    def test_range_after_enrn_gone(self):
        cal = _make_cal()
        universe = cal.ever_member(date(2019, 7, 2), date(2019, 12, 31))
        assert "ENRN" not in universe
        assert "AMZN" in universe

    def test_returns_sorted(self):
        cal = _make_cal()
        universe = cal.ever_member(date(2019, 1, 1), date(2020, 12, 31))
        assert universe == sorted(universe)


# ─────────────────────────────────────────────────────────────────────────────
# ConstituentCalendar — membership_window
# ─────────────────────────────────────────────────────────────────────────────

class TestMembershipWindow:
    def test_permanent_member_spans_full_range(self):
        cal = _make_cal()
        w = cal.membership_window("AAPL", date(2019, 1, 1), date(2020, 12, 31))
        assert w is not None
        first, last = w
        assert first == date(2019, 1, 1)
        assert last == date(2020, 12, 31)

    def test_removed_stock_ends_before_range_end(self):
        cal = _make_cal()
        w = cal.membership_window("ENRN", date(2019, 1, 1), date(2020, 12, 31))
        assert w is not None
        first, last = w
        assert first == date(2019, 1, 1)
        # Last seen in Jan snapshot; removed by Jul → last_seen < Jun 30
        assert last < date(2019, 7, 1)
        assert last >= date(2019, 1, 1)

    def test_late_addition_starts_mid_range(self):
        cal = _make_cal()
        # AMZN joined 2019-07-01; window starts there
        w = cal.membership_window("AMZN", date(2019, 1, 1), date(2020, 12, 31))
        assert w is not None
        first, last = w
        assert first == date(2019, 7, 1)
        assert last == date(2020, 12, 31)

    def test_ticker_not_in_range_returns_none(self):
        cal = _make_cal()
        w = cal.membership_window("GOOGL", date(2019, 1, 1), date(2020, 12, 31))
        assert w is None

    def test_ticker_outside_query_range_returns_none(self):
        cal = _make_cal()
        # TSLA joined 2020-01-01; querying before that
        w = cal.membership_window("TSLA", date(2019, 1, 1), date(2019, 12, 31))
        assert w is None

    def test_window_clipped_to_start(self):
        cal = _make_cal()
        # Start well inside AAPL's membership — first_seen should equal start
        w = cal.membership_window("AAPL", date(2019, 9, 1), date(2019, 12, 31))
        assert w is not None
        assert w[0] == date(2019, 9, 1)


# ─────────────────────────────────────────────────────────────────────────────
# ConstituentCalendar — from_snapshot_csv / from_changes_csv
# ─────────────────────────────────────────────────────────────────────────────

class TestCSVLoaders:
    def test_snapshot_csv_roundtrip(self, tmp_path):
        p = tmp_path / "snap.csv"
        p.write_text(_SNAPSHOT_CSV)
        cal = ConstituentCalendar.from_snapshot_csv(p)
        assert "ENRN" in cal.symbols_on(date(2019, 3, 1))
        assert "AMZN" not in cal.symbols_on(date(2019, 3, 1))

    def test_snapshot_csv_empty_raises(self, tmp_path):
        p = tmp_path / "empty.csv"
        p.write_text("date,tickers\n")
        with pytest.raises(ValueError, match="No data"):
            ConstituentCalendar.from_snapshot_csv(p)

    def test_changes_csv_roundtrip(self, tmp_path):
        csv_str = textwrap.dedent("""\
            date,ticker,action
            2019-01-01,AAPL,add
            2019-01-01,MSFT,add
            2019-07-01,ENRN,remove
            2019-07-01,AMZN,add
        """)
        p = tmp_path / "changes.csv"
        p.write_text(csv_str)
        cal = ConstituentCalendar.from_changes_csv(p)
        members_mar = cal.symbols_on(date(2019, 3, 1))
        assert "AAPL" in members_mar
        # ENRN was never added (no add event), AMZN not yet
        assert "AMZN" not in members_mar

    def test_changes_csv_invalid_action(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("date,ticker,action\n2019-01-01,AAPL,buy\n")
        with pytest.raises(ValueError, match="Unknown action"):
            ConstituentCalendar.from_changes_csv(p)

    def test_constructor_empty_raises(self):
        with pytest.raises(ValueError, match="at least one snapshot"):
            ConstituentCalendar([])


# ─────────────────────────────────────────────────────────────────────────────
# TiingoSource
# ─────────────────────────────────────────────────────────────────────────────

def _tiingo_response(ticker="AAPL", n=3):
    rows = []
    for i in range(n):
        d = datetime(2020, 1, 2 + i, tzinfo=UTC)
        rows.append({
            "date": d.isoformat(),
            "adjOpen": 100.0 + i,
            "adjHigh": 105.0 + i,
            "adjLow": 99.0 + i,
            "adjClose": 104.0 + i,
            "adjVolume": 1_000_000,
        })
    return rows


class TestTiingoSource:
    def test_get_instrument_returns_stock(self):
        src = TiingoSource(api_key="test-key")
        sym = Symbol("AAPL", "tiingo")
        inst = src.get_instrument(sym)
        assert isinstance(inst, Stock)
        assert inst.symbol.ticker == "AAPL"

    def test_supported_timeframes(self):
        src = TiingoSource(api_key="test-key")
        tfs = src.supported_timeframes()
        assert Timeframe.parse("1d") in tfs
        assert Timeframe.parse("1w") in tfs

    def test_history_calls_fetch_and_caches(self, tmp_path):
        from hermes.data import BarCache
        cache = BarCache(root=tmp_path / "cache")
        src = TiingoSource(api_key="test-key", cache=cache)
        sym = Symbol("AAPL", "tiingo")
        inst = src.get_instrument(sym)

        mock_resp = MagicMock()
        mock_resp.json.return_value = _tiingo_response("AAPL", n=3)
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp) as mock_get:
            bars = src.history(inst, D1, datetime(2020, 1, 2, tzinfo=UTC), datetime(2020, 1, 4, tzinfo=UTC))

        assert len(bars) == 3
        assert bars[0].open == pytest.approx(100.0)
        assert bars[2].close == pytest.approx(106.0)
        assert mock_get.call_count == 1

        # Second call should hit the cache — no extra HTTP request.
        with patch("requests.get", return_value=mock_resp) as mock_get2:
            bars2 = src.history(inst, D1, datetime(2020, 1, 2, tzinfo=UTC), datetime(2020, 1, 4, tzinfo=UTC))
        assert mock_get2.call_count == 0
        assert len(bars2) == 3

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("TIINGO_API_KEY", raising=False)
        src = TiingoSource(api_key="")
        with pytest.raises(RuntimeError, match="API key"):
            src._fetch("AAPL", D1, datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 1, 5, tzinfo=UTC))

    def test_unsupported_timeframe_raises(self):
        src = TiingoSource(api_key="test-key")
        h1 = Timeframe.parse("1h")
        s, e = datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 1, 5, tzinfo=UTC)
        with pytest.raises(ValueError, match="does not support"):
            src._fetch("AAPL", h1, s, e)

    def test_empty_response_returns_no_bars(self, tmp_path):
        from hermes.data import BarCache
        cache = BarCache(root=tmp_path / "cache")
        src = TiingoSource(api_key="test-key", cache=cache)
        sym = Symbol("AAPL", "tiingo")
        inst = src.get_instrument(sym)

        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            bars = src.history(inst, D1, datetime(2020, 1, 2, tzinfo=UTC), datetime(2020, 1, 4, tzinfo=UTC))
        assert bars == []

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("TIINGO_API_KEY", "env-key")
        src = TiingoSource()
        assert src._api_key == "env-key"

    def test_name(self):
        assert TiingoSource.name == "tiingo"


# ─────────────────────────────────────────────────────────────────────────────
# UniverseBacktest
# ─────────────────────────────────────────────────────────────────────────────

class _AlwaysFlat(Strategy):
    """Strategy that never trades — used to verify wiring without execution noise."""
    def setup(self): ...
    def on_bar(self, bar): ...


def _flat_bars(ticker: str, start: datetime, n: int = 5, price: float = 100.0) -> list[Bar]:
    return [
        Bar(start + timedelta(days=i), D1, price, price, price, price, 1_000_000.0)
        for i in range(n)
    ]


def _in_memory_source(ticker: str, bars: list[Bar]) -> InMemorySource:
    from hermes.core import Stock
    sym = Symbol(ticker, "memory")
    inst = Stock(sym)
    return InMemorySource(inst, {D1: bars})


class TestUniverseBacktest:
    """Tests use InMemorySource so no network is needed."""

    def _build_ub(self, tickers_with_bars, cal, start, end):
        """Build a UniverseBacktest with InMemorySource using a multi-symbol setup."""
        from hermes.data.sources.memory_source import InMemorySource as IMS

        # Build one InMemorySource per ticker; a shim dispatches by ticker.
        sources = {}
        for ticker, bars in tickers_with_bars.items():
            sym = Symbol(ticker, "memory")
            inst = CryptoPair(sym, base_asset=ticker, quote_currency="USD", tick_size=0.01)
            sources[ticker] = IMS(inst, {D1: bars})

        class MultiInMemorySource:
            name = "memory"

            def get_instrument(self, symbol):
                return sources[symbol.ticker].get_instrument(symbol)

            def history(self, instrument, timeframe, s, e):
                return sources[instrument.symbol.ticker].history(instrument, timeframe, s, e)

            def supported_timeframes(self):
                return {D1}

        return UniverseBacktest(
            strategy_factory=_AlwaysFlat,
            source=MultiInMemorySource(),
            calendar=cal,
            timeframes=[D1],
            start=start,
            end=end,
            starting_cash=50_000,
        )

    def test_runs_and_returns_universe_result(self):
        start = datetime(2020, 1, 1, tzinfo=UTC)
        end = datetime(2020, 1, 10, tzinfo=UTC)

        bars_aapl = _flat_bars("AAPL", start, n=10)
        bars_msft = _flat_bars("MSFT", start, n=10)

        cal = ConstituentCalendar([
            (date(2020, 1, 1), ["AAPL", "MSFT"]),
        ])

        ub = self._build_ub(
            {"AAPL": bars_aapl, "MSFT": bars_msft},
            cal, start, end
        )
        result = ub.run()

        assert isinstance(result, UniverseResult)
        assert result.universe_size == 2
        assert "AAPL" in result.membership_windows
        assert "MSFT" in result.membership_windows

    def test_universe_size_includes_removed_ticker(self):
        start = datetime(2020, 1, 1, tzinfo=UTC)
        end = datetime(2020, 1, 10, tzinfo=UTC)

        bars_aapl = _flat_bars("AAPL", start, n=10)
        bars_gone = _flat_bars("GONE", start, n=5)  # only 5 days of data

        cal = ConstituentCalendar([
            (date(2020, 1, 1), ["AAPL", "GONE"]),
            (date(2020, 1, 6), ["AAPL"]),  # GONE removed
        ])

        ub = self._build_ub(
            {"AAPL": bars_aapl, "GONE": bars_gone},
            cal, start, end
        )
        result = ub.run()

        assert result.universe_size == 2  # AAPL + GONE
        assert "GONE" in result.membership_windows
        # GONE's window should end before overall end
        _, gone_last = result.membership_windows["GONE"]
        assert gone_last < end.date()

    def test_empty_calendar_raises(self):
        start = datetime(2020, 1, 1, tzinfo=UTC)
        end = datetime(2020, 1, 10, tzinfo=UTC)

        # Calendar with no overlapping tickers in the source
        cal = ConstituentCalendar([(date(2025, 1, 1), ["XYZ"])])

        class EmptySource:
            name = "memory"
            def get_instrument(self, symbol): ...
            def history(self, *a): return []
            def supported_timeframes(self): return {D1}

        ub = UniverseBacktest(
            strategy_factory=_AlwaysFlat,
            source=EmptySource(),
            calendar=cal,
            timeframes=[D1],
            start=start,
            end=end,
        )
        with pytest.raises(ValueError, match="no legs"):
            ub.run()

    def test_summary_rows_delegates(self):
        start = datetime(2020, 1, 1, tzinfo=UTC)
        end = datetime(2020, 1, 10, tzinfo=UTC)
        bars = _flat_bars("AAPL", start, n=10)
        cal = ConstituentCalendar([(date(2020, 1, 1), ["AAPL"])])
        ub = self._build_ub({"AAPL": bars}, cal, start, end)
        result = ub.run()
        rows = result.summary_rows()
        assert isinstance(rows, list)
        assert any(r["symbol"].endswith("AAPL") for r in rows)
