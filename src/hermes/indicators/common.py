"""Built-in indicators shipped with Hermes.

These own the Forming-Bar / warmup semantics directly. They are pure functions of
the visible series (closed history + the current Forming Bar) that the engine
recomputes each Base step, returning ``None`` per line until warm. Breadth beyond
these comes from :mod:`hermes.indicators.library`.
"""

from __future__ import annotations

import math

from ..core import Bar, Timeframe
from .base import Indicator


def _ema_running(values: list[float], period: int) -> list[float | None]:
    """EMA at each index, seeded with the SMA of the first ``period`` values."""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1 - k)
        out[i] = ema
    return out


def _ema_last(values: list[float], period: int) -> float | None:
    running = _ema_running(values, period)
    return running[-1] if running else None


class SMA(Indicator):
    def __init__(self, timeframe: Timeframe, period: int, source: str = "close") -> None:
        super().__init__(timeframe)
        self.period = period
        self.source = source

    @property
    def lookback(self) -> int:
        return self.period

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        if len(bars) < self.period:
            return {"value": None}
        window = bars[-self.period :]
        return {"value": sum(getattr(b, self.source) for b in window) / self.period}


class EMA(Indicator):
    def __init__(self, timeframe: Timeframe, period: int, source: str = "close") -> None:
        super().__init__(timeframe)
        self.period = period
        self.source = source

    @property
    def lookback(self) -> int:
        return self.period

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        values = [getattr(b, self.source) for b in bars]
        return {"value": _ema_last(values, self.period)}


class RSI(Indicator):
    """Wilder's RSI."""

    def __init__(self, timeframe: Timeframe, period: int = 14) -> None:
        super().__init__(timeframe)
        self.period = period

    @property
    def lookback(self) -> int:
        return self.period + 1

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        if len(bars) < self.period + 1:
            return {"value": None}
        closes = [b.close for b in bars]
        gains, losses = [], []
        for prev, cur in zip(closes, closes[1:]):
            change = cur - prev
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        avg_gain = sum(gains[: self.period]) / self.period
        avg_loss = sum(losses[: self.period]) / self.period
        for g, l in zip(gains[self.period :], losses[self.period :]):
            avg_gain = (avg_gain * (self.period - 1) + g) / self.period
            avg_loss = (avg_loss * (self.period - 1) + l) / self.period
        if avg_loss == 0:
            return {"value": 100.0}
        rs = avg_gain / avg_loss
        return {"value": 100.0 - 100.0 / (1.0 + rs)}


class ATR(Indicator):
    """Wilder's Average True Range — handy for volatility-based stops/Sizers."""

    def __init__(self, timeframe: Timeframe, period: int = 14) -> None:
        super().__init__(timeframe)
        self.period = period

    @property
    def lookback(self) -> int:
        return self.period + 1

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        if len(bars) < self.period + 1:
            return {"value": None}
        trs = []
        for prev, cur in zip(bars, bars[1:]):
            trs.append(
                max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
            )
        atr = sum(trs[: self.period]) / self.period
        for tr in trs[self.period :]:
            atr = (atr * (self.period - 1) + tr) / self.period
        return {"value": atr}


class MACD(Indicator):
    def __init__(self, timeframe: Timeframe, fast: int = 12, slow: int = 26, signal: int = 9):
        super().__init__(timeframe)
        self.fast, self.slow, self.signal = fast, slow, signal

    @property
    def lookback(self) -> int:
        return self.slow + self.signal

    @property
    def outputs(self) -> tuple[str, ...]:
        return ("macd", "signal", "hist")

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        closes = [b.close for b in bars]
        fast = _ema_running(closes, self.fast)
        slow = _ema_running(closes, self.slow)
        macd_line = [
            (f - s) if (f is not None and s is not None) else None for f, s in zip(fast, slow)
        ]
        macd_vals = [m for m in macd_line if m is not None]
        signal = _ema_last(macd_vals, self.signal)
        macd_last = macd_line[-1] if macd_line else None
        hist = (
            macd_last - signal if (macd_last is not None and signal is not None) else None
        )
        return {"macd": macd_last, "signal": signal, "hist": hist}


class BollingerBands(Indicator):
    def __init__(self, timeframe: Timeframe, period: int = 20, num_std: float = 2.0):
        super().__init__(timeframe)
        self.period, self.num_std = period, num_std

    @property
    def lookback(self) -> int:
        return self.period

    @property
    def outputs(self) -> tuple[str, ...]:
        return ("upper", "middle", "lower")

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        if len(bars) < self.period:
            return {"upper": None, "middle": None, "lower": None}
        window = [b.close for b in bars[-self.period :]]
        mean = sum(window) / self.period
        variance = sum((x - mean) ** 2 for x in window) / self.period
        std = math.sqrt(variance)
        return {
            "upper": mean + self.num_std * std,
            "middle": mean,
            "lower": mean - self.num_std * std,
        }
