"""Adapter that wraps a third-party TA library (pandas-ta / TA-Lib) as an
Indicator, so authors get breadth without giving up the Forming-Bar/warmup
semantics Hermes owns.
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
    ) -> None:
        super().__init__(timeframe)
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


def _clean(x) -> float | None:
    import math

    if x is None:
        return None
    xf = float(x)
    return None if math.isnan(xf) else xf
