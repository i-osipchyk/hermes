"""Walk-forward analysis for out-of-sample validation."""

from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .result import BacktestResult


@dataclass(slots=True)
class WalkForwardWindow:
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime
    oos_result: BacktestResult
    best_params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WalkForwardResult:
    windows: list[WalkForwardWindow]
    oos_result: BacktestResult   # stitched OOS equity curve + metrics
    anchored: bool               # True = expanding IS, False = rolling


@dataclass(slots=True)
class WalkForward:
    """Run walk-forward analysis by splitting the period into in-sample (IS) and out-of-sample (OOS) windows.

    Parameters
    ----------
    template:            A Backtest instance with strategy/source/symbol/cost_model set.
                         start/end will be overridden per window.
    total_start:         Start of the entire analysis period.
    total_end:           End of the entire analysis period.
    is_frac:             Fraction of each window used as IS (e.g. 0.7 → 70% IS, 30% OOS).
    anchored:            False = rolling IS window; True = expanding IS (anchored start).
    param_grid:          Explicit parameter search grid as ``{name: [v1, v2, ...]}``.
                         If *None* (default), the grid is auto-discovered from each
                         Parameter's ``choices`` (discrete) or ``bounds`` (continuous,
                         discretised into ``bounds_steps`` linearly-spaced values).
                         Pass an empty dict ``{}`` to disable IS optimisation entirely
                         and run fixed-param OOS only (original behaviour).
    optimization_metric: Name of a ``Metrics`` field to *maximise* when selecting the
                         best IS parameter combination. Default: ``"parameter_adjusted_sharpe"``.
    bounds_steps:        How many linearly-spaced values to sample from a Parameter's
                         ``bounds`` range when ``param_grid`` is None. Default: 5.
    """
    template: object  # Backtest — avoid circular import
    total_start: datetime
    total_end: datetime
    is_frac: float = 0.7
    anchored: bool = False
    param_grid: dict[str, list] | None = None
    optimization_metric: str = "parameter_adjusted_sharpe"
    bounds_steps: int = 5

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _discover_grid(self) -> dict[str, list]:
        """Call strategy.setup() with the source instrument to read declared
        Parameter specs, then build a grid from choices / bounds.

        No full backtest is run — only the cheap setup() call is made.
        """
        bt = self.template  # type: ignore[assignment]
        instrument = bt.source.get_instrument(bt.symbol)
        strat = bt.strategy
        strat.instrument = instrument
        strat._params = dict(bt.params)
        strat.setup()

        grid: dict[str, list] = {}
        steps = max(2, self.bounds_steps)
        for spec in strat.declared_parameters():
            if spec.choices:
                grid[spec.name] = list(spec.choices)
            elif spec.bounds is not None:
                lo, hi = spec.bounds
                grid[spec.name] = [
                    lo + (hi - lo) * i / (steps - 1) for i in range(steps)
                ]
        return grid

    def _build_combos(self, grid: dict[str, list]) -> list[dict[str, Any]]:
        """Cartesian product of all grid values."""
        if not grid:
            return []
        keys = list(grid.keys())
        return [dict(zip(keys, combo)) for combo in itertools.product(*[grid[k] for k in keys])]

    def _metric_value(self, result: BacktestResult) -> float:
        """Extract the optimisation metric; return -inf when absent or NaN."""
        value = getattr(result.metrics, self.optimization_metric, None)
        if value is None:
            return float("-inf")
        try:
            f = float(value)
        except (TypeError, ValueError):
            return float("-inf")
        return f if f == f else float("-inf")  # NaN guard

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> WalkForwardResult:
        from .engine import _as_utc

        total_start = _as_utc(self.total_start)
        total_end = _as_utc(self.total_end)
        total_seconds = (total_end - total_start).total_seconds()

        oos_frac = 1.0 - self.is_frac
        oos_seconds = total_seconds * oos_frac
        is_seconds = total_seconds * self.is_frac

        if oos_seconds <= 0 or is_seconds <= 0:
            raise ValueError("is_frac must be between 0 and 1 exclusive.")

        # Build search grid -------------------------------------------------
        if self.param_grid is None:
            grid = self._discover_grid()
        else:
            grid = self.param_grid
        combos = self._build_combos(grid)

        # Build non-overlapping OOS windows stepping by oos_seconds ----------
        windows: list[WalkForwardWindow] = []
        oos_start = total_start + timedelta(seconds=is_seconds)

        while oos_start < total_end:
            oos_end = min(oos_start + timedelta(seconds=oos_seconds), total_end)
            if self.anchored:
                is_start = total_start
            else:
                is_start = oos_start - timedelta(seconds=is_seconds)
            is_end = oos_start

            # IS optimisation: grid-search parameter combinations ----------
            base_params: dict[str, Any] = dict(self.template.params)  # type: ignore[union-attr]
            best_params = base_params

            if combos:
                best_metric = float("-inf")
                for combo in combos:
                    merged = {**base_params, **combo}
                    is_bt = dataclasses.replace(
                        self.template, params=merged, start=is_start, end=is_end
                    )
                    try:
                        is_result = is_bt.run()  # type: ignore[union-attr]
                    except Exception:
                        continue
                    score = self._metric_value(is_result)
                    if score > best_metric:
                        best_metric = score
                        best_params = merged

            # OOS: run with params selected on IS --------------------------
            oos_bt = dataclasses.replace(
                self.template, params=best_params, start=oos_start, end=oos_end
            )
            oos_result = oos_bt.run()  # type: ignore[union-attr]

            windows.append(WalkForwardWindow(
                is_start=is_start,
                is_end=is_end,
                oos_start=oos_start,
                oos_end=oos_end,
                oos_result=oos_result,
                best_params=best_params,
            ))

            oos_start = oos_end
            if oos_start >= total_end:
                break

        if not windows:
            raise ValueError("No walk-forward windows could be constructed.")

        # Stitch OOS equity curves: normalize each to start at previous ending equity
        stitched_curve: list[tuple[datetime, float]] = []
        stitched_trades = []
        prev_end_equity = self.template.starting_cash  # type: ignore[union-attr]

        for w in windows:
            oos_curve = w.oos_result.equity_curve
            if not oos_curve:
                continue

            oos_start_equity = oos_curve[0][1]
            scale = prev_end_equity / oos_start_equity if oos_start_equity > 0 else 1.0

            for t, e in oos_curve:
                stitched_curve.append((t, e * scale))

            for trade in w.oos_result.trades:
                stitched_trades.append(trade)

            if stitched_curve:
                prev_end_equity = stitched_curve[-1][1]

        combined_result = BacktestResult.compute(stitched_curve, stitched_trades)
        return WalkForwardResult(
            windows=windows,
            oos_result=combined_result,
            anchored=self.anchored,
        )


def split_isoos(
    backtest: object,
    is_frac: float = 0.7,
) -> tuple[BacktestResult, BacktestResult]:
    """Run a single fixed IS / OOS split and return ``(is_result, oos_result)``.

    The date range ``[backtest.start, backtest.end)`` is divided at
    ``start + is_frac * (end - start)``.  Each half is run as an independent
    backtest using the same strategy, source, and parameters.

    Parameters
    ----------
    backtest: A configured ``Backtest`` instance.
    is_frac:  Fraction of the date range to use as in-sample. Default 0.7.
    """
    from .engine import _as_utc

    bt = backtest
    start = _as_utc(bt.start)
    end = _as_utc(bt.end)
    split_point = start + (end - start) * is_frac

    is_bt = dataclasses.replace(bt, end=split_point)
    oos_bt = dataclasses.replace(bt, start=split_point)
    return is_bt.run(), oos_bt.run()  # type: ignore[union-attr]
