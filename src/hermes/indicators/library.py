"""Adapter that wraps a third-party TA library (pandas-ta / TA-Lib) as an
Indicator, so authors get breadth without giving up the Forming-Bar/warmup
semantics Hermes owns.

The incremental path (``precompute`` / ``on_bar_closed`` / ``on_forming_bar``)
falls back to calling the wrapped function each time — still correct, and
avoids bespoke incremental state for arbitrary library calls.
"""

from __future__ import annotations

from collections.abc import Callable

from ..core import Bar, Timeframe
from .base import Indicator


class LibraryIndicator(Indicator):
    """Wrap any ``fn(bars_df) -> Series/DataFrame`` (e.g. a pandas-ta call).

    The wrapper is responsible for feeding the visible series (incl. the Forming
    Bar) as a DataFrame and reading back the last row as the current value(s).
    """

    def __init__(
        self,
        timeframe: Timeframe,
        fn: Callable,
        lookback: int,
        outputs: tuple[str, ...] = ("value",),
        *,
        mode: str | None = None,
    ) -> None:
        super().__init__(timeframe, mode=mode)
        self._fn = fn
        self._lookback = lookback
        self._outputs = outputs

    @property
    def lookback(self) -> int:
        return self._lookback

    @property
    def outputs(self) -> tuple[str, ...]:
        return self._outputs

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        if len(bars) < self._lookback:
            return dict.fromkeys(self._outputs, None)
        import pandas as pd

        df = pd.DataFrame(
            {
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            },
            index=[b.timestamp for b in bars],
        )
        result = self._fn(df)
        if isinstance(result, pd.DataFrame):
            last = result.iloc[-1]
            return {name: _clean(last.iloc[i]) for i, name in enumerate(self._outputs)}
        # Series / scalar
        value = result.iloc[-1] if hasattr(result, "iloc") else result
        return {self._outputs[0]: _clean(value)}

    # Incremental path: re-invoke the library function each time (correct but O(N)).
    # This is acceptable because library indicators are typically used for breadth,
    # not in tight inner loops.  Built-in indicators have proper O(1) paths.

    def on_bar_closed(self, bars: list[Bar]) -> None:
        self._current = self.compute(bars)

    def on_forming_bar(self, bars: list[Bar]) -> None:
        self._current = self.compute(bars)


def _clean(x) -> float | None:
    import math

    if x is None:
        return None
    xf = float(x)
    return None if math.isnan(xf) else xf
