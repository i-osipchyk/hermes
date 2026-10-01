"""Correlation between strategy return streams — the input to portfolio building.

A portfolio of strategies is not a pile of good strategies. Ray Dalio's "Holy Grail"
point is that risk-adjusted return scales with the number of **uncorrelated** streams:
*N* streams of equal quality and zero mutual correlation cut portfolio volatility by
roughly √N, while *N* copies of the same edge cut it by nothing. So "is this strategy
good?" is the wrong second question. The right one is "is it good **and** does it do
something my existing strategies don't?"

Two strategies that are both long-only trend-following on correlated instruments will
show ~0.9 here no matter how different their code looks. That is the finding that stops
you from building four versions of one bet and calling it diversification.

Mechanics: equity curves are resampled to a common frequency, converted to returns,
aligned on their shared dates, and correlated pairwise (Pearson). Only overlapping
observations count — two strategies that never traded in the same period have no
measurable correlation, and that is reported as ``None`` rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Below this many shared observations, a correlation is noise, not a measurement.
MIN_OVERLAP = 20
# At or above this, treat two streams as the same bet for portfolio purposes.
REDUNDANT_ABOVE = 0.7
# At or below this, a stream genuinely diversifies.
DIVERSIFYING_BELOW = 0.3


@dataclass(slots=True)
class CorrelationMatrix:
    """Pairwise correlation of several return streams, plus what it implies."""

    labels: list[str]
    matrix: list[list[float | None]]
    overlap: list[list[int]] = field(default_factory=list)
    frequency: str = "D"
    # Volatility of an equal-weight blend vs the mean of the individual vols.
    diversification_ratio: float | None = None

    def _idx(self, label: str) -> int:
        try:
            return self.labels.index(label)
        except ValueError as e:
            raise KeyError(f"unknown stream {label!r}") from e

    def pair(self, a: str, b: str) -> float | None:
        return self.matrix[self._idx(a)][self._idx(b)]

    def pairs(self) -> list[tuple[str, str, float | None, int]]:
        """Every unordered pair as ``(a, b, correlation, shared_observations)``."""
        out = []
        for i in range(len(self.labels)):
            for j in range(i + 1, len(self.labels)):
                n = self.overlap[i][j] if self.overlap else 0
                out.append((self.labels[i], self.labels[j], self.matrix[i][j], n))
        return out

    @property
    def redundant(self) -> list[tuple[str, str, float]]:
        """Pairs correlated enough to be one bet, worst first."""
        hits = [(a, b, c) for a, b, c, _ in self.pairs()
                if c is not None and c >= REDUNDANT_ABOVE]
        return sorted(hits, key=lambda t: -t[2])

    @property
    def diversifying(self) -> list[tuple[str, str, float]]:
        """Pairs uncorrelated enough to genuinely add to a portfolio."""
        hits = [(a, b, c) for a, b, c, _ in self.pairs()
                if c is not None and abs(c) <= DIVERSIFYING_BELOW]
        return sorted(hits, key=lambda t: abs(t[2]))

    @property
    def unmeasurable(self) -> list[tuple[str, str, int]]:
        """Pairs with too little shared history to correlate."""
        return [(a, b, n) for a, b, c, n in self.pairs() if c is None]

    def to_dict(self) -> dict:
        return {
            "labels": self.labels,
            "matrix": self.matrix,
            "overlap": self.overlap,
            "frequency": self.frequency,
            "diversification_ratio": self.diversification_ratio,
            "thresholds": {"redundant_above": REDUNDANT_ABOVE,
                           "diversifying_below": DIVERSIFYING_BELOW,
                           "min_overlap": MIN_OVERLAP},
            "redundant": [{"a": a, "b": b, "correlation": c} for a, b, c in self.redundant],
            "diversifying": [{"a": a, "b": b, "correlation": c} for a, b, c in self.diversifying],
            "unmeasurable": [{"a": a, "b": b, "shared_observations": n}
                             for a, b, n in self.unmeasurable],
        }


def _returns_frame(curves: dict[str, list[tuple[datetime, float]]], frequency: str):
    """Align every equity curve onto one calendar and return periodic returns."""
    import pandas as pd

    series = {}
    for label, curve in curves.items():
        if not curve or len(curve) < 2:
            continue
        idx = pd.DatetimeIndex([pd.Timestamp(ts) for ts, _ in curve])
        s = pd.Series([float(eq) for _, eq in curve], index=idx).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        # Last equity value in each period, then period-over-period return.
        s = s.resample(frequency).last().dropna()
        series[label] = s.pct_change().dropna()
    return pd.DataFrame(series) if series else None


def correlate_curves(
    curves: dict[str, list[tuple[datetime, float]]],
    *,
    frequency: str = "D",
    min_overlap: int = MIN_OVERLAP,
) -> CorrelationMatrix:
    """Correlate equity curves keyed by label.

    ``frequency`` is a pandas offset alias — ``"D"`` daily, ``"W"`` weekly, ``"ME"``
    month-end. Coarser frequencies are steadier but need a longer shared history.
    """
    labels = list(curves)
    frame = _returns_frame(curves, frequency)
    n = len(labels)
    matrix: list[list[float | None]] = [[None] * n for _ in range(n)]
    overlap: list[list[int]] = [[0] * n for _ in range(n)]

    if frame is None or frame.empty:
        return CorrelationMatrix(labels=labels, matrix=matrix, overlap=overlap,
                                 frequency=frequency)

    for i, a in enumerate(labels):
        for j, b in enumerate(labels):
            if a not in frame or b not in frame:
                continue
            both = frame[[a, b]].dropna() if a != b else frame[[a]].dropna()
            shared = len(both)
            overlap[i][j] = shared
            if i == j:
                matrix[i][j] = 1.0 if shared else None
                continue
            if shared < min_overlap:
                continue
            # A stream that never moves has no correlation, not a zero one. Catch it
            # here rather than letting the divide-by-zero surface as a numpy warning.
            if both[a].std() == 0 or both[b].std() == 0:
                continue
            c = both[a].corr(both[b])
            matrix[i][j] = None if c != c else round(float(c), 4)

    return CorrelationMatrix(
        labels=labels, matrix=matrix, overlap=overlap, frequency=frequency,
        diversification_ratio=_diversification_ratio(frame),
    )


def _diversification_ratio(frame: Any) -> float | None:
    """Equal-weight blend volatility ÷ mean individual volatility.

    This is the Holy Grail number, stated as a measurement rather than an aspiration:
    how much blending these particular streams actually reduced risk.

    1.0 means the streams are identical and blending bought nothing. Perfectly
    uncorrelated streams of equal volatility give ~1/√N. Values *below* 1/√N are real
    and mean some pair is negatively correlated — the streams are actively cancelling,
    not merely independent — so 1/√N is the zero-correlation reference point, not a
    floor.
    """
    usable = frame.dropna(axis=1, how="all")
    if usable.shape[1] < 2:
        return None
    aligned = usable.dropna()
    if len(aligned) < MIN_OVERLAP:
        return None
    vols = aligned.std()
    mean_vol = float(vols.mean())
    if mean_vol <= 0:
        return None
    blend_vol = float(aligned.mean(axis=1).std())
    return round(blend_vol / mean_vol, 4)


def correlate_results(
    results: dict[str, Any],
    *,
    frequency: str = "D",
    min_overlap: int = MIN_OVERLAP,
) -> CorrelationMatrix:
    """Correlate anything exposing ``equity_curve`` — ``BacktestResult`` or a restored run."""
    curves: dict[str, list[tuple[datetime, float]]] = {}
    for label, r in results.items():
        curve = getattr(r, "equity_curve", None)
        if curve is None and isinstance(r, dict):
            curve = [(datetime.fromisoformat(ts), eq) for ts, eq in r.get("equity_curve", [])]
        curves[label] = list(curve or [])
    return correlate_curves(curves, frequency=frequency, min_overlap=min_overlap)
