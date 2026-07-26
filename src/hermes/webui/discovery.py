"""Discover runnable strategies for the web UI.

Convention (emitted by the hermes-strategy skill): each ``strategies/<name>.py``
exposes a ``build_backtest(**overrides) -> Backtest`` factory and, when written by
Claude, a ``GENERATED_BY = "hermes-strategy"`` provenance marker. This module is
Streamlit-free so it stays unit-testable without the ``[ui]`` extra.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ..backtest import Backtest
from ..core import Symbol

DEFAULT_STRATEGIES_DIR = Path("strategies")


@dataclass(frozen=True, slots=True)
class StrategyEntry:
    name: str
    path: Path
    build_backtest: object          # callable(**overrides) -> Backtest
    generated_by: str | None        # provenance marker, e.g. "hermes-strategy"

    @property
    def is_ai_generated(self) -> bool:
        return self.generated_by == "hermes-strategy"


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"hermes_strategy_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover(directory: Path = DEFAULT_STRATEGIES_DIR) -> list[StrategyEntry]:
    """Return the strategies in ``directory`` that expose ``build_backtest``."""
    directory = Path(directory)
    entries: list[StrategyEntry] = []
    if not directory.exists():
        return entries
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = _load_module(path)
        factory = getattr(module, "build_backtest", None)
        if not callable(factory):
            continue
        entries.append(
            StrategyEntry(
                name=path.stem,
                path=path,
                build_backtest=factory,
                generated_by=getattr(module, "GENERATED_BY", None),
            )
        )
    return entries


def default_config(entry: StrategyEntry) -> Backtest:
    """A fresh Backtest with the strategy's defaults — introspect for form values."""
    return entry.build_backtest()


def configured_backtest(
    entry: StrategyEntry,
    *,
    ticker: str,
    start: datetime,
    end: datetime,
    starting_cash: float,
) -> Backtest:
    """Build a fresh Backtest (new Strategy instance) with the form's overrides."""
    bt = entry.build_backtest()
    return replace(
        bt,
        symbol=Symbol(ticker, bt.source.name),
        start=start,
        end=end,
        starting_cash=starting_cash,
    )
