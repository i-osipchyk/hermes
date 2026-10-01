"""Parameter sensitivity: does the result survive nudging its own parameters?

Robustness testing has two halves. :func:`~hermes.backtest.cost_sensitivity` asks
whether an edge survives friction. This asks the other, sharper question: **is the
reported result a plateau or a spike?**

A backtest reports one point in parameter space. If the metric collapses when a
parameter moves one notch, that point was found, not earned — the search located a
lucky corner of the data and the live result will be the neighbourhood average, not the
peak. A strategy whose Sharpe is 1.4 at `fast=12` and 0.2 at `fast=11` and `fast=13` has
no edge at 12; it has an artifact. One whose Sharpe sits near 1.0 across the whole
neighbourhood has something.

So the headline number here is **neighbour degradation**: the base metric against the
mean of its immediate neighbours. Everything else is detail.

Cost: one backtest per parameter per step, parameters varied **one at a time** (the base
value held for all others). That is deliberately not a grid — a grid answers "what is the
best combination", which is optimisation and belongs in
:class:`~hermes.backtest.WalkForward`. This answers "is the combination I have stable",
which is a different question and must not be conflated with it.
"""

from __future__ import annotations

import copy
import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any

from .result import BacktestResult

# Below this, a parameter's metric curve is treated as a spike rather than a plateau.
PLATEAU_RATIO = 0.6


@dataclass(slots=True)
class ParamPoint:
    """One parameter value and the metric it produced."""

    value: Any
    metric: float | None
    num_trades: int
    is_base: bool = False


@dataclass(slots=True)
class ParamCurve:
    """How one parameter's metric behaves around its configured value."""

    name: str
    base_value: Any
    base_metric: float | None
    points: list[ParamPoint] = field(default_factory=list)

    @property
    def neighbour_metrics(self) -> list[float]:
        """The metrics immediately either side of the base point."""
        idx = next((i for i, p in enumerate(self.points) if p.is_base), None)
        if idx is None:
            return []
        out = []
        for j in (idx - 1, idx + 1):
            if 0 <= j < len(self.points) and self.points[j].metric is not None:
                out.append(self.points[j].metric)
        return out

    @property
    def neighbour_mean(self) -> float | None:
        ns = self.neighbour_metrics
        return sum(ns) / len(ns) if ns else None

    @property
    def degradation(self) -> float | None:
        """``neighbour_mean / base_metric`` — the plateau-vs-spike number.

        ~1.0 means the neighbourhood performs like the chosen point (a plateau).
        Near 0, or negative, means the chosen point is a spike and the reported
        metric is not reproducible one notch away. ``None`` when the base metric is
        absent or too close to zero for a ratio to mean anything.
        """
        base, nm = self.base_metric, self.neighbour_mean
        if base is None or nm is None or abs(base) < 1e-9:
            return None
        return nm / base

    @property
    def is_plateau(self) -> bool | None:
        d = self.degradation
        if d is None:
            return None
        return d >= PLATEAU_RATIO

    @property
    def spread(self) -> float | None:
        """Max minus min metric across the swept values — the range at stake."""
        ms = [p.metric for p in self.points if p.metric is not None]
        return (max(ms) - min(ms)) if len(ms) > 1 else None


@dataclass(slots=True)
class ParamSensitivity:
    """Per-parameter metric curves for one backtest."""

    metric: str
    curves: list[ParamCurve] = field(default_factory=list)

    def __getitem__(self, name: str) -> ParamCurve:
        for c in self.curves:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def fragile(self) -> list[str]:
        """Parameters whose chosen value is a spike, not a plateau."""
        return [c.name for c in self.curves if c.is_plateau is False]

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "plateau_ratio": PLATEAU_RATIO,
            "fragile": self.fragile,
            "curves": [
                {
                    "name": c.name,
                    "base_value": c.base_value,
                    "base_metric": c.base_metric,
                    "neighbour_mean": c.neighbour_mean,
                    "degradation": c.degradation,
                    "is_plateau": c.is_plateau,
                    "spread": c.spread,
                    "points": [
                        {"value": p.value, "metric": p.metric,
                         "num_trades": p.num_trades, "is_base": p.is_base}
                        for p in c.points
                    ],
                }
                for c in self.curves
            ],
        }


def _metric_of(result: BacktestResult, metric: str) -> float | None:
    v = getattr(result.metrics, metric, None)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if not (math.isnan(f) or math.isinf(f)) else None


def _sweep_values(spec: Any, base: Any, steps: int, relative: float) -> list[Any]:
    """The values to try for one parameter, always including ``base``.

    Discrete ``choices`` are swept as given. Otherwise the sweep is a band around the
    *configured* value — not across the whole of ``bounds``, because the question is
    whether this point is stable, not what the best point is. Integers stay integers,
    so a lookback never becomes 12.5 bars.
    """
    if spec.choices:
        return list(spec.choices)

    if not isinstance(base, (int, float)) or isinstance(base, bool):
        return [base]

    lo_b, hi_b = spec.bounds if spec.bounds is not None else (None, None)
    span = abs(base) * relative if base else max(1.0, (hi_b or 1.0) * relative)
    lo, hi = base - span, base + span
    if lo_b is not None:
        lo = max(lo, lo_b)
    if hi_b is not None:
        hi = min(hi, hi_b)

    half = max(1, steps // 2)
    out: list[Any] = []
    for i in range(-half, half + 1):
        v = base + (span * i / half)
        v = min(max(v, lo), hi)
        out.append(int(round(v)) if isinstance(base, int) else round(float(v), 6))

    # Dedupe (clamping and integer rounding collapse values) while keeping order,
    # and guarantee the base survives so there is always a point to compare against.
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    if base not in uniq:
        uniq.append(base)
    return sorted(uniq)


def param_sensitivity(
    backtest: Any,
    *,
    metric: str = "sharpe",
    params: list[str] | None = None,
    steps: int = 4,
    relative: float = 0.25,
) -> ParamSensitivity:
    """Sweep each declared Parameter around its configured value, one at a time.

    Parameters
    ----------
    backtest:  A configured ``Backtest``.
    metric:    Name of a ``Metrics`` field to track (default ``"sharpe"``).
    params:    Restrict to these parameter names; default is every declared one.
    steps:     Roughly how many values per parameter (the base is always included).
    relative:  Half-width of the sweep as a fraction of the configured value.
    """
    bt = backtest
    instrument = bt.source.get_instrument(bt.symbol)
    strat = bt.strategy
    strat.instrument = instrument
    strat._params = dict(bt.params)
    strat.run_setup()
    specs = [s for s in strat.declared_parameters()
             if params is None or s.name in params]

    out = ParamSensitivity(metric=metric)
    for spec in specs:
        base = bt.params.get(spec.name, spec.default)
        curve = ParamCurve(name=spec.name, base_value=base, base_metric=None)
        for value in _sweep_values(spec, base, steps, relative):
            run = dataclasses.replace(
                bt,
                strategy=copy.deepcopy(bt.strategy),
                params={**bt.params, spec.name: value},
            )
            try:
                result = run.run()
            except Exception:  # noqa: BLE001 — a bad value must not end the sweep
                curve.points.append(ParamPoint(value=value, metric=None, num_trades=0,
                                               is_base=(value == base)))
                continue
            m = _metric_of(result, metric)
            is_base = value == base
            curve.points.append(ParamPoint(value=value, metric=m,
                                           num_trades=result.metrics.num_trades,
                                           is_base=is_base))
            if is_base:
                curve.base_metric = m
        out.curves.append(curve)
    return out
