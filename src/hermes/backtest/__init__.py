"""Backtesting: the engine, the result, and reporting."""

from .batch import BatchResult, run_batch
from .correlation import (
    DIVERSIFYING_BELOW,
    MIN_OVERLAP,
    REDUNDANT_ABOVE,
    CorrelationMatrix,
    correlate_curves,
    correlate_results,
)
from .engine import Backtest
from .param_sensitivity import (
    ParamCurve,
    ParamPoint,
    ParamSensitivity,
    param_sensitivity,
)
from .portfolio import PortfolioBacktest, PortfolioResult
from .random_baseline import run_random_simulations, run_universe_random_simulations
from .regime import RegimeAnalysis, RegimeStats, regime_analysis
from .reporting import plot_equity, plot_trades, tearsheet
from .result import BacktestResult, BenchmarkComparison, BenchmarkStats, Metrics
from .sensitivity import cost_sensitivity
from .universe import UniverseBacktest, UniverseResult
from .validation import (
    TIER_CREDIBLE,
    TIER_DEPLOYABLE,
    TIER_EXPLORATORY,
    TIER_UNKNOWN,
    ConfidenceInterval,
    MetricsCI,
    MonteCarloStats,
    SampleQuality,
    StatValidation,
    validate,
)
from .walk_forward import WalkForward, WalkForwardResult, WalkForwardWindow, split_isoos

__all__ = [
    "Backtest",
    "BacktestResult",
    "Metrics",
    "BenchmarkComparison",
    "BenchmarkStats",
    "BatchResult",
    "run_batch",
    "correlate_curves",
    "correlate_results",
    "CorrelationMatrix",
    "REDUNDANT_ABOVE",
    "DIVERSIFYING_BELOW",
    "MIN_OVERLAP",
    "PortfolioBacktest",
    "PortfolioResult",
    "param_sensitivity",
    "ParamSensitivity",
    "ParamCurve",
    "ParamPoint",
    "run_random_simulations",
    "run_universe_random_simulations",
    "plot_equity",
    "plot_trades",
    "tearsheet",
    "StatValidation",
    "MetricsCI",
    "ConfidenceInterval",
    "MonteCarloStats",
    "SampleQuality",
    "TIER_EXPLORATORY",
    "TIER_CREDIBLE",
    "TIER_DEPLOYABLE",
    "TIER_UNKNOWN",
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
