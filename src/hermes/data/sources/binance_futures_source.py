"""Binance USD-M perpetual futures DataSource.

Same public REST shape as spot (klines pagination, cache) — only the host, endpoint
paths, and the Instrument differ. Reuses everything from :class:`BinanceSource` and
returns a :class:`CryptoPerpetual` (leveraged, shortable) instead of a spot pair.
Its `name` ("binance-futures") keeps the Parquet cache separate from spot.
"""

from __future__ import annotations

from ...core import CryptoPerpetual, Symbol
from ..cache import BarCache
from .binance_source import BinanceSource


class BinanceFuturesSource(BinanceSource):
    name = "binance-futures"
    base_url = "https://fapi.binance.com"
    klines_path = "/fapi/v1/klines"
    exchange_info_path = "/fapi/v1/exchangeInfo"

    def __init__(self, cache: BarCache | None = None, *, leverage: float = 10.0) -> None:
        super().__init__(cache)
        self.leverage = leverage

    def _make_instrument(
        self, symbol: Symbol, base_asset: str, quote: str, tick_size: float
    ) -> CryptoPerpetual:
        return CryptoPerpetual(
            symbol,
            base_asset=base_asset,
            quote_currency=quote,
            tick_size=tick_size,
            leverage=self.leverage,
        )
