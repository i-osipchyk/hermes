"""Discover runnable strategies for the web UI.

Convention (emitted by the hermes-strategy skill): each ``strategies/<family>/<name>.py``
exposes a ``build_backtest(**overrides) -> Backtest`` factory — or, for a universe-scale
run, ``build_universe_backtest(**overrides) -> UniverseBacktest`` — and, when written by
Claude, a ``GENERATED_BY = "hermes-strategy"`` provenance marker. This module is
Streamlit-free so it stays unit-testable without the ``[ui]`` extra.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ..backtest import Backtest
from ..core import Symbol

DEFAULT_STRATEGIES_DIR = Path("strategies")


@dataclass(frozen=True, slots=True)
class BrokenStrategy:
    """A strategy file that could not be imported (or whose name is taken).

    Discovery never raises on a bad file: one unimportable strategy must not hide
    every working one. Callers (the UI, and any agent enumerating runnable work)
    render these alongside the healthy entries so the failure is visible and
    attributable instead of silent.
    """
    name: str
    path: Path
    error: str          # "ExcType: message"
    traceback: str


@dataclass(frozen=True, slots=True)
class StrategyEntry:
    name: str
    path: Path
    build_backtest: object | None   # callable(**overrides) -> Backtest; None if universe-only
    generated_by: str | None        # provenance marker, e.g. "hermes-strategy"
    # callable(**overrides) -> UniverseBacktest, for a file that defines its own universe
    build_universe_backtest: object | None = None

    @property
    def is_ai_generated(self) -> bool:
        return self.generated_by == "hermes-strategy"

    @property
    def is_universe(self) -> bool:
        """True for a file that only builds a universe run (no single-symbol factory)."""
        return self.build_backtest is None


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"hermes_strategy_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses/typing resolve a class's module via
    # sys.modules[cls.__module__], which is None if we never register it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def discover(directory: Path = DEFAULT_STRATEGIES_DIR) -> list[StrategyEntry]:
    """Return the strategies in ``directory`` that expose ``build_backtest``.

    Universe-only files (``build_universe_backtest`` alone) are left out, since every
    caller of this function builds a single-symbol Backtest; :func:`discover_all`
    includes them. Import failures are skipped, not raised.
    """
    entries, _ = discover_all(directory)
    return [e for e in entries if e.build_backtest is not None]


def discover_all(
    directory: Path = DEFAULT_STRATEGIES_DIR,
) -> tuple[list[StrategyEntry], list[BrokenStrategy]]:
    """Return ``(working, broken)`` for every ``*.py`` under ``directory``.

    "Working" includes universe-only files (``entry.is_universe``); a file that
    defines both factories is one entry carrying both.

    Loading a strategy executes its module, so a syntax error or a bad import in
    one file used to abort the whole scan. Each file is now isolated: failures
    become :class:`BrokenStrategy` records and the rest still load.

    Strategies may be grouped into family folders (``strategies/fx_sunday_gap/…``);
    a file or folder starting with ``_`` is skipped. The name is the file stem
    wherever the file lives — it keys the run ledger — so a stem that appears twice
    is reported broken in both places rather than letting one shadow the other.
    """
    directory = Path(directory)
    entries: list[StrategyEntry] = []
    broken: list[BrokenStrategy] = []
    if not directory.exists():
        return entries, broken
    paths = [
        p for p in sorted(directory.rglob("*.py"))
        if not any(part.startswith("_") for part in p.relative_to(directory).parts)
    ]
    stems = Counter(p.stem for p in paths)
    for path in paths:
        if stems[path.stem] > 1:
            others = ", ".join(str(p) for p in paths if p.stem == path.stem and p != path)
            broken.append(
                BrokenStrategy(
                    name=path.stem,
                    path=path,
                    error=f"DuplicateName: '{path.stem}' is also defined in {others}",
                    traceback="",
                )
            )
            continue
        try:
            module = _load_module(path)
        except Exception as e:  # noqa: BLE001 — any import-time failure is a broken file
            broken.append(
                BrokenStrategy(
                    name=path.stem,
                    path=path,
                    error=f"{type(e).__name__}: {e}",
                    traceback=traceback.format_exc(),
                )
            )
            continue
        factory = getattr(module, "build_backtest", None)
        universe_factory = getattr(module, "build_universe_backtest", None)
        if not callable(factory) and not callable(universe_factory):
            continue
        entries.append(
            StrategyEntry(
                name=path.stem,
                path=path,
                build_backtest=factory if callable(factory) else None,
                generated_by=getattr(module, "GENERATED_BY", None),
                build_universe_backtest=universe_factory if callable(universe_factory) else None,
            )
        )
    return entries, broken


def _single(entry: StrategyEntry) -> Callable[..., Backtest]:
    """The single-symbol factory, or a clear error for a universe-only file."""
    if entry.build_backtest is None:
        raise ValueError(f"'{entry.name}' only defines build_universe_backtest; "
                         f"run it as a universe")
    return cast(Callable[..., Backtest], entry.build_backtest)


def default_config(entry: StrategyEntry) -> Backtest:
    """A fresh Backtest with the strategy's defaults — introspect for form values."""
    return _single(entry)()


def declared_parameters(entry: StrategyEntry):
    """The strategy's tunable Parameters (specs), by running ``setup()`` on a fresh
    instance. Returns ``list[Parameter]`` the UI renders as editable controls."""
    strat = _single(entry)().strategy
    strat.run_setup()
    return strat.declared_parameters()


def configured_backtest(
    entry: StrategyEntry,
    *,
    ticker: str,
    start: datetime,
    end: datetime,
    starting_cash: float,
    source_name: str | None = None,
    params: dict | None = None,
    unconstrained: bool = False,
    sizer=None,
    advisor=None,
    leverage: float | None = None,
) -> Backtest:
    """Build a fresh Backtest (new Strategy instance) with the form's overrides —
    source, ticker, dates, cash, and strategy Parameter overrides."""
    from .sources import build_source

    factory = _single(entry)
    bt = factory()
    if ticker != bt.symbol.ticker:
        # Rebuild through the factory so symbol-dependent parts (a cost model priced in
        # ticks, an instrument-specific default) are derived for THIS symbol; a bare
        # replace(symbol=...) would keep the default symbol's.
        try:
            bt = factory(symbol=Symbol(ticker, bt.symbol.source))
        except TypeError:       # a factory that takes no overrides keeps the old behaviour
            pass
    source = (
        build_source(source_name, leverage=leverage)
        if source_name and (source_name != bt.source.name or leverage is not None)
        else bt.source
    )
    kw: dict[str, Any] = dict(
        source=source,
        symbol=Symbol(ticker, source.name),
        start=start,
        end=end,
        starting_cash=starting_cash,
        params=params or {},
        unconstrained=unconstrained,
    )
    if sizer is not None:
        kw["sizer"] = sizer
    if advisor is not None:
        kw["advisor"] = advisor
    return replace(bt, **kw)


def run_universe(
    entry: StrategyEntry,
    *,
    tickers: list[str],
    source_name: str | None,
    start: datetime,
    end: datetime,
    starting_cash: float,
    params: dict | None = None,
    unconstrained: bool = False,
    sizer=None,
    advisor=None,
    progress_callback=None,
    leverage: float | None = None,
):
    """Run the strategy across all tickers as a single shared-capital portfolio.

    All legs draw from one ``starting_cash`` pool via :class:`PortfolioBacktest` —
    identical to how calendar universes (e.g. sp500) are run."""
    from ..backtest.portfolio import PortfolioBacktest
    from ..backtest.universe import UniverseResult

    legs = [
        configured_backtest(
            entry, source_name=source_name, ticker=ticker,
            start=start, end=end, starting_cash=starting_cash,
            params=params, unconstrained=unconstrained, sizer=sizer,
            advisor=advisor, leverage=leverage,
        )
        for ticker in tickers
    ]
    pr = PortfolioBacktest(legs=legs, starting_cash=starting_cash, unconstrained=unconstrained,
                           progress_callback=progress_callback).run()
    return UniverseResult(portfolio_result=pr, universe_size=len(tickers))
