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


class Fractals(Indicator):
    """Williams-style fractals over a 3-bar window (user-defined conditions).

    For a candidate candle ``i`` (the middle of the last triple), with ``i-1`` and
    ``i+1`` as its neighbours:

    * **up** fractal (a swing high, based on highs):
      ``high[i-1] < high[i]`` and ``high[i+1] <= high[i]``
    * **down** fractal (a swing low, based on lows):
      ``low[i-1] > low[i]`` and ``low[i+1] >= low[i]``

    Both are local extremes — strict against the left neighbour, ties allowed against the
    right. Because a fractal needs its right neighbour, the candidate is ``bars[-2]`` and
    the result confirms with a **1-bar lag** (using the possibly-forming ``bars[-1]`` as
    ``i+1``, per the repaint-but-parity-safe model, ADR-0002). Outputs are 1.0/0.0.
    """

    @property
    def lookback(self) -> int:
        return 3

    @property
    def outputs(self) -> tuple[str, ...]:
        return ("up", "down")

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        if len(bars) < 3:
            return {"up": None, "down": None}
        prev, cand, nxt = bars[-3], bars[-2], bars[-1]  # i-1, i, i+1
        up = prev.high < cand.high and nxt.high <= cand.high
        down = prev.low > cand.low and nxt.low >= cand.low
        return {"up": 1.0 if up else 0.0, "down": 1.0 if down else 0.0}


class FairValueGap(Indicator):
    """Fair Value Gap (3-candle imbalance).

    Over the last triple (``c1``, ``c2``, ``c3`` = ``bars[-3:]``), the gap is defined by
    the outer candles and located at the middle candle ``c2``:

    * **bullish** FVG when ``c1.high < c3.low`` — an up-gap zone ``[c1.high, c3.low]``
    * **bearish** FVG when ``c1.low > c3.high`` — a down-gap zone ``[c3.high, c1.low]``

    Confirms with a **1-bar lag** (needs ``c3``; ``bars[-1]`` may be the Forming Bar).
    Outputs: ``bullish``/``bearish`` (1.0/0.0) and the gap boundaries ``top``/``bottom``
    (``None`` when no gap), so a strategy can react to the zone.
    """

    @property
    def lookback(self) -> int:
        return 3

    @property
    def outputs(self) -> tuple[str, ...]:
        return ("bullish", "bearish", "top", "bottom")

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        none = {"bullish": None, "bearish": None, "top": None, "bottom": None}
        if len(bars) < 3:
            return none
        c1, _c2, c3 = bars[-3], bars[-2], bars[-1]
        if c1.high < c3.low:
            return {"bullish": 1.0, "bearish": 0.0, "top": c3.low, "bottom": c1.high}
        if c1.low > c3.high:
            return {"bullish": 0.0, "bearish": 1.0, "top": c1.low, "bottom": c3.high}
        return {"bullish": 0.0, "bearish": 0.0, "top": None, "bottom": None}


# `fvg` is the common shorthand.
FVG = FairValueGap


class ADX(Indicator):
    """Wilder's Average Directional Index.

    Measures **trend strength** (0–100, higher = stronger trend) together with
    directional bias via ``plus_di`` and ``minus_di``.

    Algorithm (Wilder smoothing throughout, matching ATR/RSI):
    * **True Range** = max(H-L, |H-prev_close|, |L-prev_close|)
    * **+DM** = H - prev_H when that is larger than prev_L - L and > 0, else 0
    * **-DM** = prev_L - L when that is larger than H - prev_H and > 0, else 0
    * **Smoothed ATR/+DM/-DM** over ``period`` bars (Wilder: seed = SMA, then
      rolling ``(prev * (n-1) + cur) / n``)
    * **+DI / -DI** = 100 × smoothed_DM / smoothed_ATR
    * **DX** = 100 × |+DI - -DI| / (+DI + -DI)
    * **ADX** = Wilder smooth of DX over ``period`` steps

    Lookback is ``2 * period``: one period warms the DI lines, another warms ADX.
    Outputs: ``adx``, ``plus_di``, ``minus_di``.
    """

    def __init__(self, timeframe: Timeframe, period: int = 14) -> None:
        super().__init__(timeframe)
        self.period = period

    @property
    def lookback(self) -> int:
        return 2 * self.period

    @property
    def outputs(self) -> tuple[str, ...]:
        return ("adx", "plus_di", "minus_di")

    @staticmethod
    def _wilder(values: list[float], period: int) -> list[float]:
        """Wilder smoothing: seed = SMA of first `period` values, then rolling."""
        out = [sum(values[:period]) / period]
        for v in values[period:]:
            out.append((out[-1] * (period - 1) + v) / period)
        return out

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        null: dict[str, float | None] = {"adx": None, "plus_di": None, "minus_di": None}
        p = self.period
        if len(bars) < 2 * p:
            return null

        # Step 1: TR, +DM, -DM for each successive pair (len(bars)-1 values).
        trs: list[float] = []
        plus_dms: list[float] = []
        minus_dms: list[float] = []
        for prev, cur in zip(bars, bars[1:]):
            tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
            up = cur.high - prev.high
            dn = prev.low - cur.low
            trs.append(tr)
            plus_dms.append(up if up > dn and up > 0 else 0.0)
            minus_dms.append(dn if dn > up and dn > 0 else 0.0)

        # Step 2: Wilder-smooth each series.  With len(bars) = 2p we get
        # len(trs) = 2p-1, and _wilder produces p smoothed values.
        smooth_tr = self._wilder(trs, p)
        smooth_plus = self._wilder(plus_dms, p)
        smooth_minus = self._wilder(minus_dms, p)

        # Step 3: DX at each smoothed step.
        dx_vals: list[float] = []
        for atr, pdm, mdm in zip(smooth_tr, smooth_plus, smooth_minus):
            if atr == 0:
                dx_vals.append(0.0)
                continue
            pdi = 100.0 * pdm / atr
            mdi = 100.0 * mdm / atr
            di_sum = pdi + mdi
            dx_vals.append(100.0 * abs(pdi - mdi) / di_sum if di_sum != 0 else 0.0)

        # Step 4: ADX = Wilder smooth of DX.  At len(bars) = 2p we have exactly p
        # dx values, which seeds the first ADX value.
        adx_vals = self._wilder(dx_vals, p)
        adx = adx_vals[-1]

        last_atr = smooth_tr[-1]
        if last_atr == 0:
            return {"adx": adx, "plus_di": 0.0, "minus_di": 0.0}
        plus_di = 100.0 * smooth_plus[-1] / last_atr
        minus_di = 100.0 * smooth_minus[-1] / last_atr
        return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}
