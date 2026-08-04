"""Hermes — a framework for developing, backtesting, and deploying intraday/swing
trading strategies on candlestick data.

See ``CONTEXT.md`` for the ubiquitous language and ``docs/adr/`` for the load-bearing
architectural decisions. The public surface below is the intended entry point for
strategy authors.
"""

from __future__ import annotations

__version__ = "0.0.1"

from .ai import AdvisorDecision, AIAdvisor, AIProvider, ClaudeProvider
from .backtest import Backtest, BacktestResult
from .core import (
    AssetClass,
    Bar,
    Cfd,
    CryptoPair,
    CryptoPerpetual,
    Instrument,
    PriceBasis,
    SessionCalendar,
    Stock,
    Symbol,
    Timeframe,
)
from .data import (
    BinanceFuturesSource,
    BinanceSource,
    CTraderSource,
    DataSource,
    PepperstoneSource,
    YFinanceSource,
)
from .data._ssl import ensure_system_trust as use_system_certs
from .execution import Order, OrderType, Position, Side, Trade
from .indicators import (
    ADX,
    ATR,
    EMA,
    FVG,
    MACD,
    RSI,
    SMA,
    BollingerBands,
    FairValueGap,
    Fractals,
    Indicator,
)
from .skilltools import install_skills
from .strategy import (
    NotionalCash,
    Parameter,
    Reference,
    RiskCash,
    RiskPercent,
    Strategy,
    Units,
)

__all__ = [
    "__version__",
    # core
    "Bar",
    "Timeframe",
    "Symbol",
    "Instrument",
    "Stock",
    "CryptoPair",
    "CryptoPerpetual",
    "Cfd",
    "AssetClass",
    "PriceBasis",
    "SessionCalendar",
    # data
    "DataSource",
    "YFinanceSource",
    "BinanceSource",
    "BinanceFuturesSource",
    "CTraderSource",
    "PepperstoneSource",
    # indicators
    "Indicator",
    "SMA",
    "EMA",
    "RSI",
    "ATR",
    "MACD",
    "BollingerBands",
    "Fractals",
    "FairValueGap",
    "FVG",
    "ADX",
    # strategy
    "Strategy",
    "Parameter",
    "Reference",
    "Units",
    "NotionalCash",
    "RiskCash",
    "RiskPercent",
    # execution
    "Order",
    "OrderType",
    "Side",
    "Trade",
    "Position",
    # ai
    "AIAdvisor",
    "AIProvider",
    "AdvisorDecision",
    "ClaudeProvider",
    # backtest
    "Backtest",
    "BacktestResult",
    # tooling
    "install_skills",
    "use_system_certs",
]
