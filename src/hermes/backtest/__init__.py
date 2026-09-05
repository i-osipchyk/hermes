"""Backtesting: the engine, the result, and reporting."""

from .batch import BatchResult, run_batch
from .engine import Backtest
from .portfolio import PortfolioBacktest, PortfolioResult
from .reporting import plot_equity, plot_trades, tearsheet
from .result import BacktestResult, Metrics

__all__ = [
    "Backtest",
    "BacktestResult",
    "Metrics",
    "BatchResult",
    "run_batch",
    "PortfolioBacktest",
    "PortfolioResult",
    "plot_equity",
    "plot_trades",
    "tearsheet",
]
