"""Binance-backed DataSource for crypto spot pairs.

Uses the public REST klines endpoint (no auth needed for historical data) via the
stdlib, so basic fetching has no third-party dependency. Normalizes to UTC ``Bar``s,
24/7 Session Calendar, real volume, last-price basis, and caches fetch-once.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from ...core import Bar, CryptoPair, Instrument, Symbol, Timeframe
from .._ssl import ensure_system_trust
from ..cache import BarCache
from ..source import DataSource

_SUPPORTED = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w")


class BinanceSource(DataSource):
    name = "binance"
    base_url = "https://api.binance.com"
    klines_path = "/api/v3/klines"
    exchange_info_path = "/api/v3/exchangeInfo"

    def __init__(self, cache: BarCache | None = None) -> None:
        self.cache = cache or BarCache()

    def get_instrument(self, symbol: Symbol) -> Instrument:
        tick_size, base_asset, quote = 0.01, "", "USDT"
        try:
            info = self._get(self.exchange_info_path, {"symbol": symbol.ticker})
            s = info["symbols"][0]
            base_asset, quote = s["baseAsset"], s["quoteAsset"]
            for f in s["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    tick_size = float(f["tickSize"])
        except Exception:
            pass  # best-effort metadata; defaults are fine for backtesting
        return self._make_instrument(symbol, base_asset, quote, tick_size)

    def _make_instrument(self, symbol: Symbol, base_asset: str, quote: str, tick_size: float):
        return CryptoPair(symbol, base_asset=base_asset, quote_currency=quote, tick_size=tick_size)

    def history(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        for gap_start, gap_end in self.cache.missing_ranges(instrument, timeframe, start, end):
            fetched = self._fetch(instrument.symbol.ticker, timeframe, gap_start, gap_end)
            self.cache.write(instrument, timeframe, fetched)
        return self.cache.read(instrument, timeframe, start, end)

    def supported_timeframes(self) -> set[Timeframe]:
        return {Timeframe.parse(t) for t in _SUPPORTED}

    # --- internals -------------------------------------------------------------

    def _fetch(self, ticker: str, timeframe: Timeframe, start: datetime, end: datetime) -> list[Bar]:
        interval = str(timeframe)
        start_ms, end_ms = _ms(start), _ms(end)
        out: list[Bar] = []
        while start_ms < end_ms:
            rows = self._get(
                self.klines_path,
                {
                    "symbol": ticker,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            if not rows:
                break
            for r in rows:
                out.append(
                    Bar(
                        datetime.fromtimestamp(r[0] / 1000, tz=UTC),
                        timeframe,
                        float(r[1]),
                        float(r[2]),
                        float(r[3]),
                        float(r[4]),
                        float(r[5]),
                    )
                )
            last_open = rows[-1][0]
            start_ms = last_open + timeframe.seconds * 1000
            if len(rows) < 1000:
                break
        return out

    def _get(self, path: str, params: dict) -> object:
        ensure_system_trust()  # trust the OS store (corporate proxies) before any fetch
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (public API)
            return json.loads(resp.read().decode())


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)
