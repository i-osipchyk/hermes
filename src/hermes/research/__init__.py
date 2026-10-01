"""The research loop: enumerate strategies, run them, record them, judge them.

Everything here is **UI-free** — it is what a CLI, an agent, or the Streamlit app all
sit on top of (ADR-0010). Four pieces:

* :mod:`~hermes.research.discovery` — find the runnable strategies in a directory and
  build configured backtests from them.
* :mod:`~hermes.research.ledger` — the input-keyed record of every run, so "have I
  tried this?" and "what have I tried?" are answerable.
* :mod:`~hermes.research.analysis` — execute the analyse-results rubric as real
  computations and emit machine-readable evidence.
* :mod:`~hermes.research.review` — turn that evidence into a written verdict by
  driving Claude Code headlessly.

Plus :mod:`~hermes.research.sources` (a DataSource registry, so a strategy can be
pointed at another provider without editing it) and
:mod:`~hermes.research.universes` (ticker lists and constituent calendars).
"""

from __future__ import annotations

from .analysis import ALL_AXES, DEFAULT_AXES, Analysis, AxisResult, analyze
from .discovery import (
    BrokenStrategy,
    StrategyEntry,
    configured_backtest,
    declared_parameters,
    default_config,
    discover,
    discover_all,
    run_universe,
)
from .ideas import SOURCES, STATUSES, Idea, IdeaBook
from .ledger import (
    LedgerEntry,
    RestoredResult,
    RunLedger,
    RunMeta,
    RunRecord,
    cache_key,
    restore_result,
    strategy_hash,
)
from .review import ReviewStatus
from .sources import build_source, source_names

__all__ = [
    # discovery
    "discover",
    "discover_all",
    "StrategyEntry",
    "BrokenStrategy",
    "default_config",
    "declared_parameters",
    "configured_backtest",
    "run_universe",
    # ideas
    "IdeaBook",
    "Idea",
    "SOURCES",
    "STATUSES",
    # ledger
    "RunLedger",
    "RunMeta",
    "RunRecord",
    "LedgerEntry",
    "RestoredResult",
    "restore_result",
    "cache_key",
    "strategy_hash",
    # analysis
    "analyze",
    "Analysis",
    "AxisResult",
    "DEFAULT_AXES",
    "ALL_AXES",
    # review
    "ReviewStatus",
    # sources
    "build_source",
    "source_names",
]
