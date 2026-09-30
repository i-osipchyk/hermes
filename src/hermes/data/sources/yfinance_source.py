"""yfinance-backed DataSource for stocks.

Normalization (see ADR/CONTEXT corporate actions):
  * **fully adjusted** OHLC via ``auto_adjust=True`` — both split factors AND
    dividend back-adjustment are applied, so price LEVELS are total-return levels
    rather than the prices actually printed on the day. This matches
    :class:`~hermes.data.sources.TiingoSource` (``adjOpen``/``adjClose``), so the
    two are interchangeable in a portfolio;
  * consequently dividends are already embedded in the price series — they are
    **not** credited separately as cash, and no ex-date feed is exposed. Do not
    add cash-dividend handling on top of these bars or it double-counts;
  * build a Session Calendar (US equities default: 09:30-16:00 America/New_York).

Fine history is limited (1m ~30d, 15m ~60d); ``history`` surfaces whatever the
provider returns and the engine warns if Lead-in is short.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ...core import Bar, Instrument, SessionCalendar, Stock, Symbol, Timeframe
from .._ssl import ensure_system_trust
from ..cache import BarCache, checked_through, complete_bars
from ..source import DataSource

_INTERVAL = {
    "1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "60m", "1d": "1d", "1w": "1wk",
}
_US_SESSION = SessionCalendar(
    timezone=ZoneInfo("America/New_York"), open_time=time(9, 30), close_time=time(16, 0)
)


class YFinanceSource(DataSource):
    name = "yfinance"

    def __init__(
        self,
        cache: BarCache | None = None,
        session: SessionCalendar = _US_SESSION,
        shortable: bool = False,
        leverage: float = 1.0,
    ):
        self.cache = cache or BarCache()
        self.session = session
        self.shortable = shortable
        self.leverage = leverage

    def get_instrument(self, symbol: Symbol) -> Stock:
        return Stock(symbol, session=self.session, shortable=self.shortable, leverage=self.leverage)

    def history(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        if self.cache.is_nodata(instrument, timeframe):
            return []
        file_existed = self.cache._path(instrument, timeframe).exists()
        for gap_start, gap_end in self.cache.missing_ranges(instrument, timeframe, start, end):
            fetched = complete_bars(
                self._fetch(instrument.symbol.ticker, timeframe, gap_start, gap_end), timeframe
            )
            self.cache.write(instrument, timeframe, fetched)
            self.cache.mark_checked(
                instrument, timeframe, gap_start,
                checked_through(fetched, timeframe, gap_start, gap_end),
            )
        bars = self.cache.read(instrument, timeframe, start, end)
        if not bars and not file_existed:
            self.cache.mark_nodata(instrument, timeframe)
        return bars

    def supported_timeframes(self) -> set[Timeframe]:
        return {Timeframe.parse(t) for t in _INTERVAL}

    # --- internals -------------------------------------------------------------

    def _fetch(self, ticker: str, timeframe: Timeframe, start: datetime, end: datetime) -> list[Bar]:
        ensure_system_trust()  # trust the OS store (corporate proxies) before any fetch
        try:
            import yfinance as yf
        except ImportError as e:  # pragma: no cover
            raise ImportError("YFinanceSource needs 'yfinance'. pip install 'hermes[yfinance]'.") from e

        interval = _INTERVAL.get(str(timeframe))
        if interval is None:
            raise ValueError(f"yfinance does not support timeframe {timeframe}")

        # yfinance documents `end` as EXCLUSIVE, so asking for [start, end] drops
        # the bar at `end` -- and mark_checked then seals that hole permanently.
        # Push the boundary out by one bar to make the range inclusive.
        df = yf.Ticker(ticker).history(
            start=start,
            end=end + timedelta(seconds=timeframe.seconds),
            interval=interval,
            auto_adjust=True,
        )
        if df.empty:
            return []
        # yfinance can return a stray NaN row (e.g. a partial current/holiday day);
        # dropping it keeps NaN prices out of the backtest.
        df = df.dropna(subset=["Open", "High", "Low", "Close"])

        bars = []
        for ts, row in df.iterrows():
            when = ts.to_pydatetime()
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            bars.append(
                Bar(
                    when.astimezone(UTC),
                    timeframe,
                    float(row["Open"]),
                    float(row["High"]),
                    float(row["Low"]),
                    float(row["Close"]),
                    float(row["Volume"]),
                )
            )
        return bars


