"""Backtesting: the engine, the result, and reporting."""

from .engine import Backtest
from .reporting import plot_equity, plot_trades, tearsheet
from .result import BacktestResult, Metrics

__all__ = [
    "Backtest",
    "BacktestResult",
    "Metrics",
    "plot_equity",
    "plot_trades",
    "tearsheet",
]
