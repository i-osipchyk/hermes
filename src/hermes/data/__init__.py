"""Market-data side of the framework: sources, cache, and forming-bar aggregation."""

from .aggregation import MultiTimeframeView, TimeframeSeries
from .cache import BarCache
from .source import DataSource
from .sources import (
    BinanceFuturesSource,
    BinanceSource,
    CTraderSource,
    InMemorySource,
    PepperstoneSource,
    YFinanceSource,
)

__all__ = [
    "DataSource",
    "BarCache",
    "MultiTimeframeView",
    "TimeframeSeries",
    "YFinanceSource",
    "BinanceSource",
    "BinanceFuturesSource",
    "CTraderSource",
    "PepperstoneSource",
    "InMemorySource",
]
