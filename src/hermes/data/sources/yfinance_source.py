"""yfinance-backed DataSource for stocks.

Normalization (see ADR/CONTEXT corporate actions):
  * **split-adjust** OHLC but keep price LEVELS real (indicators see true prices) —
    i.e. apply split factors only, NOT dividend back-adjustment;
  * expose dividends/ex-dates for the engine's dividend-as-cash handling;
  * build a Session Calendar (US equities default: 09:30-16:00 America/New_York).

Fine history is limited (1m ~30d, 15m ~60d); ``history`` surfaces whatever the
provider returns and the engine warns if Lead-in is short.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from ...core import Bar, Instrument, SessionCalendar, Stock, Symbol, Timeframe
from .._ssl import ensure_system_trust
from ..cache import BarCache
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

    def __init__(self, cache: BarCache | None = None, session: SessionCalendar = _US_SESSION):
        self.cache = cache or BarCache()
        self.session = session

    def get_instrument(self, symbol: Symbol) -> Stock:
        return Stock(symbol, session=self.session)

    def history(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        for gap_start, gap_end in self.cache.missing_ranges(instrument, timeframe, start, end):
            self.cache.write(
                instrument, timeframe, self._fetch(instrument.symbol.ticker, timeframe, gap_start, gap_end)
            )
        return self.cache.read(instrument, timeframe, start, end)

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

        df = yf.Ticker(ticker).history(
            start=start, end=end, interval=interval, auto_adjust=False, actions=True
        )
        if df.empty:
            return []
        df = _split_adjust(df)

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


def _split_adjust(df):
    """Apply split factors only (dividends left as real price drops / cash events)."""
    if "Stock Splits" not in df.columns:
        return df
    splits = df["Stock Splits"].replace(0, 1.0)
    inclusive = splits[::-1].cumprod()[::-1]
    factor = inclusive / splits  # product of splits strictly AFTER each row
    for col in ("Open", "High", "Low", "Close"):
        df[col] = df[col] / factor
    df["Volume"] = df["Volume"] * factor
    return df
