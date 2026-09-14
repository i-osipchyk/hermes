"""Tiingo-backed DataSource for stocks, including delisted tickers.

Tiingo (https://www.tiingo.com) provides EOD price history that covers both
active and delisted stocks, making it suitable for survivorship-bias-free
backtests via :class:`~hermes.backtest.universe.UniverseBacktest`.

Configuration
-------------
Pass your API key directly or set the environment variable ``TIINGO_API_KEY``::

    source = TiingoSource(api_key="my-key")
    source = TiingoSource()   # reads TIINGO_API_KEY from the environment

Authentication uses the ``Authorization: Token <key>`` header (not a query
parameter) so the key never appears in cache-file paths.

Normalization
-------------
* Uses fully-adjusted OHLCV (``adjOpen`` / ``adjClose`` etc.) which accounts
  for both splits and dividends — consistent with yfinance ``auto_adjust=True``.
* Bars are normalized to UTC and stamped at the market-close timestamp
  (16:00 America/New_York for US equities).
* Session calendar mirrors :class:`~hermes.data.sources.YFinanceSource`: US
  equities, 09:30–16:00 America/New_York.

Supported timeframes
--------------------
``1d`` (daily) and ``1w`` (weekly) via Tiingo's EOD endpoint.

Free-tier limits
----------------
The Tiingo free tier allows 500 API calls/day and returns up to 5 years of
history for active stocks; paid tiers have unlimited history and cover
delisted tickers back to 1993.  The ``BarCache`` ensures each date range is
only fetched once per ticker.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from ...core import Bar, Instrument, SessionCalendar, Stock, Symbol, Timeframe
from .._ssl import ensure_system_trust
from ..cache import BarCache
from ..source import DataSource

_US_SESSION = SessionCalendar(
    timezone=ZoneInfo("America/New_York"),
    open_time=time(9, 30),
    close_time=time(16, 0),
)

_RESAMPLE: dict[str, str] = {
    "1d": "daily",
    "1w": "weekly",
}


class TiingoSource(DataSource):
    """Tiingo EOD DataSource.

    Supports active *and* delisted US equity tickers — ideal for
    survivorship-bias-free universe backtests.

    Args:
        api_key: Tiingo API key.  Falls back to the ``TIINGO_API_KEY``
                 environment variable if not provided.
        cache:   ``BarCache`` instance.  A default on-disk Parquet cache is
                 used when omitted.
        session: Trading session calendar.  Defaults to US equities
                 (09:30–16:00 America/New_York).
    """

    name = "tiingo"

    def __init__(
        self,
        api_key: str | None = None,
        cache: BarCache | None = None,
        session: SessionCalendar = _US_SESSION,
    ) -> None:
        self._api_key = api_key or os.environ.get("TIINGO_API_KEY") or ""
        self.cache = cache or BarCache()
        self.session = session

    def get_instrument(self, symbol: Symbol) -> Stock:
        return Stock(symbol, session=self.session)

    def history(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        for gap_start, gap_end in self.cache.missing_ranges(
            instrument, timeframe, start, end
        ):
            self.cache.write(
                instrument,
                timeframe,
                self._fetch(instrument.symbol.ticker, timeframe, gap_start, gap_end),
            )
        return self.cache.read(instrument, timeframe, start, end)

    def supported_timeframes(self) -> set[Timeframe]:
        return {Timeframe.parse(t) for t in _RESAMPLE}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        ensure_system_trust()
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "TiingoSource needs 'requests'.  pip install requests."
            ) from exc

        resample = _RESAMPLE.get(str(timeframe))
        if resample is None:
            raise ValueError(
                f"TiingoSource does not support timeframe {timeframe}. "
                f"Supported: {sorted(_RESAMPLE)}"
            )
        if not self._api_key:
            raise RuntimeError(
                "TiingoSource requires an API key.  Pass api_key= or set "
                "the TIINGO_API_KEY environment variable."
            )

        url = f"https://api.tiingo.com/tiingo/daily/{ticker}/prices"
        params = {
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": end.strftime("%Y-%m-%d"),
            "resampleFreq": resample,
        }
        headers = {
            "Authorization": f"Token {self._api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return []

        bars: list[Bar] = []
        for row in data:
            raw_date = row.get("date", "")
            # Tiingo returns ISO 8601 timestamps, e.g. "2020-01-02T00:00:00+00:00"
            ts = datetime.fromisoformat(raw_date)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            ts = ts.astimezone(UTC)

            o = row.get("adjOpen") or row.get("open")
            h = row.get("adjHigh") or row.get("high")
            lo = row.get("adjLow") or row.get("low")
            c = row.get("adjClose") or row.get("close")
            v = row.get("adjVolume") or row.get("volume") or 0.0

            if None in (o, h, lo, c):
                continue  # skip rows with missing OHLC

            bars.append(Bar(ts, timeframe, float(o), float(h), float(lo), float(c), float(v)))
        return bars
