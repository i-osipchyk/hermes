"""Indicator interface (CONTEXT.md / ADR-0002).

An Indicator is bound to one Timeframe's series and recomputes every Base step,
**including that Timeframe's Forming Bar** as its latest data point. It declares
its ``lookback`` so the engine can size the Lead-in and suppress ``on_bar`` until
every declared Indicator is warm.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core import Bar, Timeframe


class Indicator(ABC):
    """Base class for all indicators.

    Subclasses may emit one or more named output lines (e.g. MACD -> macd/signal/
    hist). ``value()`` returns the current value(s) computed over the visible series
    up to and including the Forming Bar.
    """

    def __init__(self, timeframe: Timeframe) -> None:
        self.timeframe = timeframe

    @property
    @abstractmethod
    def lookback(self) -> int:
        """Bars required before the Indicator produces a valid value (warmup)."""

    @property
    def outputs(self) -> tuple[str, ...]:
        """Names of the output lines. Single-line indicators return one name."""
        return ("value",)

    @abstractmethod
    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        """Compute current output(s) from ``bars`` (closed history + Forming Bar).

        Return ``None`` for a line while still within its lookback. The engine
        caches/recomputes this each Base step; keep it a pure function of ``bars``.
        """

    @property
    def is_ready_len(self) -> int:
        return self.lookback
