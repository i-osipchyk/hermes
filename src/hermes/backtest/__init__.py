"""Backtesting: the engine, the result, and reporting."""

from .batch import BatchResult, run_batch
from .engine import Backtest
from .portfolio import PortfolioBacktest, PortfolioResult
from .regime import RegimeAnalysis, RegimeStats, regime_analysis
from .reporting import plot_equity, plot_trades, tearsheet
from .result import BacktestResult, BenchmarkComparison, BenchmarkStats, Metrics
from .sensitivity import cost_sensitivity
from .validation import (
    ConfidenceInterval,
    MetricsCI,
    MonteCarloStats,
    SampleQuality,
    StatValidation,
    validate,
)
from .universe import UniverseBacktest, UniverseResult
from .walk_forward import WalkForward, WalkForwardResult, WalkForwardWindow, split_isoos

__all__ = [
    "Backtest",
    "BacktestResult",
    "Metrics",
    "BenchmarkComparison",
    "BenchmarkStats",
    "BatchResult",
    "run_batch",
    "PortfolioBacktest",
    "PortfolioResult",
    "plot_equity",
    "plot_trades",
    "tearsheet",
    "StatValidation",
    "MetricsCI",
    "ConfidenceInterval",
    "MonteCarloStats",
    "SampleQuality",
    "validate",
    "WalkForward",
    "WalkForwardResult",
    "WalkForwardWindow",
    "split_isoos",
    "cost_sensitivity",
    "RegimeAnalysis",
    "RegimeStats",
    "regime_analysis",
    "UniverseBacktest",
    "UniverseResult",
]
