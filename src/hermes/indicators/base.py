"""Indicator interface (CONTEXT.md / ADR-0002).

An Indicator is bound to one Timeframe's series.  Two update modes are supported:

* ``latest_confirmed`` (default) — the indicator value only advances when the
  Timeframe's bar fully closes.  Between closes the cached value is frozen.
  This is the safe default: every value seen by ``on_bar`` is based on sealed,
  immutable data.

* ``latest`` — the indicator recomputes on every Base step using the most recent
  available close (the forming bar's close for higher Timeframes).  This matches
  what you'd see on a live chart where the forming candle's close updates the
  indicator in real time.

Each Indicator inherits its mode from the Strategy unless overridden explicitly
in the constructor.  The engine calls :meth:`precompute` once at the warmup
boundary, then :meth:`on_bar_closed` whenever the Timeframe's bar seals and
(for ``latest`` mode only) :meth:`on_forming_bar` on every other Base step.
:meth:`current_value` returns the pre-computed result so ``indicator_value()``
inside ``on_bar`` is an O(1) dict lookup.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core import Bar, Timeframe


class Indicator(ABC):
    """Base class for all indicators.

    Subclasses may emit one or more named output lines (e.g. MACD -> macd/signal/
    hist). ``compute()`` is the legacy single-call interface (still used for
    unit tests); the engine drives the incremental path via ``precompute`` →
    ``on_bar_closed`` / ``on_forming_bar``.
    """

    def __init__(self, timeframe: Timeframe, *, mode: str | None = None) -> None:
        self.timeframe = timeframe
        # None until Strategy.use() (or Reference.use()) resolves it to the
        # strategy's default mode.
        self.mode: str | None = mode
        self._current: dict[str, float | None] = {}

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

        Return ``None`` for a line while still within its lookback.  Kept for
        backwards compatibility and unit tests; the engine uses the incremental
        path instead.
        """

    # ------------------------------------------------------------------
    # Incremental path — override in subclasses for O(1) per-bar cost.
    # The defaults here fall back to ``compute()`` so any Indicator that
    # only implements ``compute`` still works correctly.
    # ------------------------------------------------------------------

    def precompute(self, bars: list[Bar]) -> None:
        """Vectorised warmup over all closed bars at the warmup boundary.

        ``bars`` contains every closed bar of this Indicator's Timeframe up to
        and including the first trading bar.  Built-in indicators run a single
        forward pass here to seed their running state; the default falls back
        to ``compute(bars)``.
        """
        self._current = self.compute(bars)

    def on_bar_closed(self, bars: list[Bar]) -> None:
        """Called when a new bar of this Timeframe closes during the trading window.

        ``bars`` is the full closed-bar list including the just-sealed bar.
        Built-in indicators do an O(1) Wilder/EMA step using stored state and
        ``bars[-1]``; the default falls back to ``compute(bars)``.
        """
        self._current = self.compute(bars)

    def on_forming_bar(self, bars: list[Bar]) -> None:
        """Called on every Base step when the forming bar updates (``latest`` mode only).

        ``bars`` = closed history + current Forming Bar.  Must update
        ``_current`` with a tentative value **without mutating running state** —
        the Forming Bar's data is not yet confirmed.  The default falls back to
        ``compute(bars)``.
        """
        self._current = self.compute(bars)

    def current_value(self) -> dict[str, float | None]:
        """Return the pre-computed current value.  O(1) — no recomputation."""
        return self._current

    @property
    def is_ready_len(self) -> int:
        return self.lookback
