"""Universe runs: one strategy over many symbols, as one ledger row.

A universe run is a shared-capital :class:`~hermes.backtest.PortfolioBacktest` across
many legs, so it needs a different builder from a single-symbol run. There are three
ways to say which symbols:

* **The strategy's own universe**: a file exposing ``build_universe_backtest`` (e.g.
  ``strategies/gap_breakout/gap_breakout_sp500.py``). Recorded with ``universe="(own)"``.
* **A calendar universe** (``sp500``, from :mod:`~hermes.research.universes`): a
  ``build_backtest`` strategy wrapped in a :class:`~hermes.backtest.UniverseBacktest`,
  so each leg is clipped to the symbol's point-in-time membership (no survivorship
  bias, no look-ahead).
* **A static ticker list** (``tickers/<name>.json``): every leg built through the
  strategy's own factory for that symbol (:func:`~hermes.research.discovery.run_universe`).

Whichever it is, :func:`universe_run` returns a :class:`UniverseRun` that knows its
ledger key, its :class:`~hermes.research.ledger.RunMeta` and how to save itself. The
key is built from the same inputs as the web UI's (``ticker=None``, ``universe=<name>``),
so the two share runs as long as the source they key on agrees.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .discovery import StrategyEntry, run_universe
from .ledger import RunLedger, RunMeta, cache_key, strategy_hash
from .universes import (
    TICKERS_DIR,
    calendar_universe_names,
    is_calendar_universe,
    load_calendar,
    load_universe,
    universe_names,
)

# RunMeta.universe for a strategy file that defines its own universe.
OWN_UNIVERSE = "(own)"


@dataclass
class UniverseRun:
    """A configured universe run: its identity in the ledger, and how to execute it."""

    entry: StrategyEntry
    universe: str                  # a universe name, or OWN_UNIVERSE
    source: str
    start: datetime
    end: datetime
    starting_cash: float
    params: dict[str, Any]
    unconstrained: bool
    sizer: str                     # repr(sizer), as the ledger records it
    has_advisor: bool              # an AI Advisor makes one billed call per signal per leg
    size_hint: int | None          # number of symbols, when known before running
    _execute: Callable[[Any], Any] = field(repr=False)

    @property
    def key(self) -> str:
        return cache_key(
            strategy=self.entry.name,
            strategy_hash=strategy_hash(self.entry.path),
            source=self.source,
            ticker=None,
            universe=self.universe,
            start=self.start.isoformat(),
            end=self.end.isoformat(),
            params=self.params,
            sizer=self.sizer,
            unconstrained=self.unconstrained,
        )

    def run(self, progress_callback: Any = None) -> Any:
        """Execute it. Returns a :class:`~hermes.backtest.UniverseResult`."""
        return self._execute(progress_callback)

    def meta(self, note: str = "") -> RunMeta:
        return RunMeta(
            strategy=self.entry.name,
            strategy_hash=strategy_hash(self.entry.path),
            source=self.source,
            ticker=None,
            universe=self.universe,
            start=self.start.isoformat(),
            end=self.end.isoformat(),
            params=self.params,
            sizer=self.sizer,
            unconstrained=self.unconstrained,
            created_at=datetime.now(UTC).isoformat(),
            note=note,
        )

    def save(self, ledger: RunLedger, universe_result: Any, note: str = "") -> dict[str, Any]:
        """Record the run and its per-symbol breakdown. Returns the stored result dict."""
        pr = universe_result.portfolio_result
        result_dict: dict[str, Any] = pr.result.to_dict()
        ledger.save(self.key, result_dict, self.meta(note),
                    llm_log=getattr(pr.result, "llm_log", None))
        ledger.save_universe_meta(
            self.key,
            universe_size=universe_result.universe_size,
            summary_rows=pr.summary_rows(),
            per_symbol_counts={k: len(v) for k, v in pr.per_symbol.items()},
        )
        return result_dict


def known_universes(tickers_dir: Path = TICKERS_DIR) -> list[str]:
    """Universe names a ``build_backtest`` strategy can be run over, from the cwd."""
    return sorted({*universe_names(tickers_dir), *calendar_universe_names()})


def universe_run(
    entry: StrategyEntry,
    *,
    universe: str | None = None,
    source_name: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    starting_cash: float | None = None,
    params: dict[str, Any] | None = None,
    unconstrained: bool = False,
    tickers_dir: Path = TICKERS_DIR,
) -> UniverseRun:
    """Configure a universe run of ``entry``. ``None`` means "the strategy's default".

    ``universe=None`` (or :data:`OWN_UNIVERSE`) uses the file's own
    ``build_universe_backtest``; a name runs its ``build_backtest`` over that universe.
    Raises :class:`ValueError` when the combination can't be run.
    """
    from .sources import build_source

    params = dict(params or {})
    if universe in (None, OWN_UNIVERSE):
        if entry.build_universe_backtest is None:
            raise ValueError(
                f"'{entry.name}' is a single-symbol strategy; name a universe to run it "
                f"over (known: {', '.join(known_universes(tickers_dir)) or 'none'})"
            )
        factory = cast(Callable[..., Any], entry.build_universe_backtest)
        overrides: dict[str, Any] = {"unconstrained": unconstrained}
        if source_name and source_name != factory().source.name:
            overrides["source"] = build_source(source_name)
        for k, v in (("start", start), ("end", end), ("starting_cash", starting_cash)):
            if v is not None:
                overrides[k] = v
        if params:
            overrides["params"] = params
        ub = factory(**overrides)

        def _own(cb: Any) -> Any:
            fresh = factory(**overrides)
            fresh.progress_callback = cb
            return fresh.run()

        return UniverseRun(
            entry=entry, universe=OWN_UNIVERSE, source=ub.source.name, start=ub.start,
            end=ub.end, starting_cash=ub.starting_cash, params=dict(ub.params),
            unconstrained=ub.unconstrained, sizer=repr(ub.sizer),
            has_advisor=ub.advisor is not None,
            size_hint=len(ub.calendar.ever_member(ub.start, ub.end)), _execute=_own,
        )

    if entry.build_backtest is None:
        raise ValueError(
            f"'{entry.name}' defines its own universe (build_universe_backtest only); "
            f"run it without naming one"
        )
    build = cast(Callable[..., Any], entry.build_backtest)
    template = build()
    start = start or template.start
    end = end or template.end
    cash = starting_cash if starting_cash is not None else template.starting_cash

    if is_calendar_universe(universe):
        from ..backtest import UniverseBacktest

        source = build_source(source_name) if source_name and source_name != \
            template.source.name else template.source
        calendar = load_calendar(universe)

        def _calendar(cb: Any) -> Any:
            return UniverseBacktest(
                strategy_factory=lambda: build().strategy,
                source=source, calendar=calendar, timeframes=list(template.timeframes),
                start=start, end=end, starting_cash=cash,
                # One cost model for every leg: the strategy's own. A model priced in the
                # default symbol's ticks would be wrong here — use a static universe then.
                cost_model=template.cost_model, params=params,
                unconstrained=unconstrained, sizer=template.sizer,
                advisor=template.advisor, progress_callback=cb,
            ).run()

        return UniverseRun(
            entry=entry, universe=universe, source=source.name, start=start, end=end,
            starting_cash=cash, params=params, unconstrained=unconstrained,
            sizer=repr(template.sizer), has_advisor=template.advisor is not None,
            size_hint=len(calendar.ever_member(start, end)), _execute=_calendar,
        )

    if universe in universe_names(tickers_dir):
        pinned, tickers = load_universe(universe, tickers_dir)
        effective = pinned or source_name or template.source.name

        def _static(cb: Any) -> Any:
            return run_universe(
                entry, tickers=tickers, source_name=effective, start=start, end=end,
                starting_cash=cash, params=params, unconstrained=unconstrained,
                progress_callback=cb,
            )

        return UniverseRun(
            entry=entry, universe=universe, source=effective, start=start, end=end,
            starting_cash=cash, params=params, unconstrained=unconstrained,
            sizer=repr(template.sizer), has_advisor=template.advisor is not None,
            size_hint=len(tickers), _execute=_static,
        )

    raise ValueError(
        f"no universe '{universe}'. Known: {', '.join(known_universes(tickers_dir)) or 'none'} "
        f"(static lists live in {tickers_dir}/*.json)"
    )
