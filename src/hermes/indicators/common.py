"""Built-in indicators shipped with Hermes.

Each indicator implements the full incremental path:

* ``precompute(bars)`` — single vectorised pass over the lead-in history to
  seed running state.  O(N) once.
* ``on_bar_closed(bars)`` — O(1) Wilder/EMA/rolling step using ``bars[-1]``
  and stored state.  Called every time this Timeframe's bar seals.
* ``on_forming_bar(bars)`` — tentative value using the forming bar's data
  *without* mutating running state.  Called each Base step in ``latest`` mode.

``compute(bars)`` is retained for unit tests and the ``LibraryIndicator``
fallback path.
"""

from __future__ import annotations

import math
from collections import deque

from ..core import Bar, Timeframe
from .base import Indicator


# ---------------------------------------------------------------------------
# EMA helpers (shared by EMA, MACD)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------

class SMA(Indicator):
    def __init__(
        self, timeframe: Timeframe, period: int, source: str = "close", *, mode: str | None = None
    ) -> None:
        super().__init__(timeframe, mode=mode)
        self.period = period
        self.source = source
        self._window: deque[float] = deque(maxlen=period)

    @property
    def lookback(self) -> int:
        return self.period

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        if len(bars) < self.period:
            return {"value": None}
        window = bars[-self.period:]
        return {"value": sum(getattr(b, self.source) for b in window) / self.period}

    def precompute(self, bars: list[Bar]) -> None:
        self._window.clear()
        for b in bars:
            self._window.append(getattr(b, self.source))
        self._current = self._value_from_window()

    def on_bar_closed(self, bars: list[Bar]) -> None:
        self._window.append(getattr(bars[-1], self.source))
        self._current = self._value_from_window()

    def on_forming_bar(self, bars: list[Bar]) -> None:
        # bars[-1] is the forming bar; use last (period-1) closed values + it.
        if len(self._window) < self.period - 1:
            self._current = {"value": None}
            return
        closed_part = list(self._window)[-(self.period - 1):]
        forming_val = getattr(bars[-1], self.source)
        self._current = {"value": (sum(closed_part) + forming_val) / self.period}

    def _value_from_window(self) -> dict[str, float | None]:
        if len(self._window) < self.period:
            return {"value": None}
        return {"value": sum(self._window) / self.period}


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

class EMA(Indicator):
    def __init__(
        self, timeframe: Timeframe, period: int, source: str = "close", *, mode: str | None = None
    ) -> None:
        super().__init__(timeframe, mode=mode)
        self.period = period
        self.source = source
        self._ema: float | None = None
        self._k: float = 2.0 / (period + 1)

    @property
    def lookback(self) -> int:
        return self.period

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        values = [getattr(b, self.source) for b in bars]
        return {"value": _ema_last(values, self.period)}

    def precompute(self, bars: list[Bar]) -> None:
        values = [getattr(b, self.source) for b in bars]
        self._ema = _ema_last(values, self.period)
        self._current = {"value": self._ema}

    def on_bar_closed(self, bars: list[Bar]) -> None:
        val = getattr(bars[-1], self.source)
        if self._ema is None:
            if len(bars) >= self.period:
                self._ema = _ema_last(
                    [getattr(b, self.source) for b in bars], self.period
                )
        else:
            self._ema = val * self._k + self._ema * (1 - self._k)
        self._current = {"value": self._ema}

    def on_forming_bar(self, bars: list[Bar]) -> None:
        if self._ema is None:
            self._current = {"value": None}
            return
        val = getattr(bars[-1], self.source)
        self._current = {"value": val * self._k + self._ema * (1 - self._k)}
        # _ema NOT mutated


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

class RSI(Indicator):
    """Wilder's RSI."""

    def __init__(
        self, timeframe: Timeframe, period: int = 14, *, mode: str | None = None
    ) -> None:
        super().__init__(timeframe, mode=mode)
        self.period = period
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._prev_close: float | None = None

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
        for g, l in zip(gains[self.period:], losses[self.period:]):
            avg_gain = (avg_gain * (self.period - 1) + g) / self.period
            avg_loss = (avg_loss * (self.period - 1) + l) / self.period
        if avg_loss == 0:
            return {"value": 100.0}
        rs = avg_gain / avg_loss
        return {"value": 100.0 - 100.0 / (1.0 + rs)}

    def precompute(self, bars: list[Bar]) -> None:
        if len(bars) < self.period + 1:
            self._prev_close = bars[-1].close if bars else None
            self._current = {"value": None}
            return
        closes = [b.close for b in bars]
        gains, losses = [], []
        for prev, cur in zip(closes, closes[1:]):
            change = cur - prev
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        avg_gain = sum(gains[: self.period]) / self.period
        avg_loss = sum(losses[: self.period]) / self.period
        for g, l in zip(gains[self.period:], losses[self.period:]):
            avg_gain = (avg_gain * (self.period - 1) + g) / self.period
            avg_loss = (avg_loss * (self.period - 1) + l) / self.period
        self._avg_gain = avg_gain
        self._avg_loss = avg_loss
        self._prev_close = closes[-1]
        self._current = {"value": self._rsi(avg_gain, avg_loss)}

    def on_bar_closed(self, bars: list[Bar]) -> None:
        bar = bars[-1]
        if self._prev_close is None:
            self._prev_close = bar.close
            self._current = {"value": None}
            return
        if self._avg_gain is None:
            # Not yet warm — fall back to full recompute once
            self.precompute(bars)
            return
        change = bar.close - self._prev_close
        self._avg_gain = (self._avg_gain * (self.period - 1) + max(change, 0.0)) / self.period
        self._avg_loss = (self._avg_loss * (self.period - 1) + max(-change, 0.0)) / self.period
        self._prev_close = bar.close
        self._current = {"value": self._rsi(self._avg_gain, self._avg_loss)}

    def on_forming_bar(self, bars: list[Bar]) -> None:
        if self._avg_gain is None or self._prev_close is None:
            self._current = {"value": None}
            return
        change = bars[-1].close - self._prev_close
        avg_gain = (self._avg_gain * (self.period - 1) + max(change, 0.0)) / self.period
        avg_loss = (self._avg_loss * (self.period - 1) + max(-change, 0.0)) / self.period
        self._current = {"value": self._rsi(avg_gain, avg_loss)}
        # state NOT mutated

    @staticmethod
    def _rsi(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

class ATR(Indicator):
    """Wilder's Average True Range — handy for volatility-based stops/Sizers."""

    def __init__(
        self, timeframe: Timeframe, period: int = 14, *, mode: str | None = None
    ) -> None:
        super().__init__(timeframe, mode=mode)
        self.period = period
        self._atr: float | None = None
        self._prev_close: float | None = None
        self._warmup_trs: list[float] = []

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
        for tr in trs[self.period:]:
            atr = (atr * (self.period - 1) + tr) / self.period
        return {"value": atr}

    def precompute(self, bars: list[Bar]) -> None:
        self._atr = None
        self._prev_close = None
        self._warmup_trs = []
        if not bars:
            self._current = {"value": None}
            return
        trs = [
            max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
            for p, c in zip(bars, bars[1:])
        ]
        if len(trs) < self.period:
            self._warmup_trs = list(trs)
            self._prev_close = bars[-1].close
            self._current = {"value": None}
            return
        atr = sum(trs[: self.period]) / self.period
        for tr in trs[self.period:]:
            atr = (atr * (self.period - 1) + tr) / self.period
        self._atr = atr
        self._prev_close = bars[-1].close
        self._current = {"value": atr}

    def on_bar_closed(self, bars: list[Bar]) -> None:
        bar = bars[-1]
        if self._prev_close is None:
            self._prev_close = bar.close
            self._current = {"value": None}
            return
        tr = max(
            bar.high - bar.low,
            abs(bar.high - self._prev_close),
            abs(bar.low - self._prev_close),
        )
        if self._atr is None:
            self._warmup_trs.append(tr)
            if len(self._warmup_trs) >= self.period:
                self._atr = sum(self._warmup_trs) / self.period
                self._warmup_trs = []
        else:
            self._atr = (self._atr * (self.period - 1) + tr) / self.period
        self._prev_close = bar.close
        self._current = {"value": self._atr}

    def on_forming_bar(self, bars: list[Bar]) -> None:
        if self._atr is None or self._prev_close is None:
            self._current = {"value": None}
            return
        bar = bars[-1]  # forming bar
        tr = max(
            bar.high - bar.low,
            abs(bar.high - self._prev_close),
            abs(bar.low - self._prev_close),
        )
        self._current = {"value": (self._atr * (self.period - 1) + tr) / self.period}
        # _atr and _prev_close NOT mutated


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

class MACD(Indicator):
    def __init__(
        self,
        timeframe: Timeframe,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        *,
        mode: str | None = None,
    ):
        super().__init__(timeframe, mode=mode)
        self.fast, self.slow, self.signal = fast, slow, signal
        self._fast_ema: float | None = None
        self._slow_ema: float | None = None
        self._signal_ema: float | None = None
        self._k_fast = 2.0 / (fast + 1)
        self._k_slow = 2.0 / (slow + 1)
        self._k_signal = 2.0 / (signal + 1)

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

    def precompute(self, bars: list[Bar]) -> None:
        closes = [b.close for b in bars]
        fast_s = _ema_running(closes, self.fast)
        slow_s = _ema_running(closes, self.slow)
        self._fast_ema = fast_s[-1]
        self._slow_ema = slow_s[-1]
        macd_vals = [
            f - s for f, s in zip(fast_s, slow_s) if f is not None and s is not None
        ]
        if len(macd_vals) >= self.signal:
            sig_s = _ema_running(macd_vals, self.signal)
            self._signal_ema = sig_s[-1]
            macd_last = (
                (fast_s[-1] - slow_s[-1])
                if fast_s[-1] is not None and slow_s[-1] is not None
                else None
            )
            hist = (
                (macd_last - self._signal_ema)
                if macd_last is not None and self._signal_ema is not None
                else None
            )
            self._current = {"macd": macd_last, "signal": self._signal_ema, "hist": hist}
        else:
            self._signal_ema = None
            self._current = {"macd": None, "signal": None, "hist": None}

    def on_bar_closed(self, bars: list[Bar]) -> None:
        close = bars[-1].close
        if self._fast_ema is None or self._slow_ema is None:
            self._current = self.compute(bars)
            return
        self._fast_ema = close * self._k_fast + self._fast_ema * (1 - self._k_fast)
        self._slow_ema = close * self._k_slow + self._slow_ema * (1 - self._k_slow)
        macd = self._fast_ema - self._slow_ema
        if self._signal_ema is None:
            self._current = self.compute(bars)
            return
        self._signal_ema = macd * self._k_signal + self._signal_ema * (1 - self._k_signal)
        self._current = {
            "macd": macd,
            "signal": self._signal_ema,
            "hist": macd - self._signal_ema,
        }

    def on_forming_bar(self, bars: list[Bar]) -> None:
        if self._fast_ema is None or self._slow_ema is None or self._signal_ema is None:
            self._current = {"macd": None, "signal": None, "hist": None}
            return
        close = bars[-1].close
        fast = close * self._k_fast + self._fast_ema * (1 - self._k_fast)
        slow = close * self._k_slow + self._slow_ema * (1 - self._k_slow)
        macd = fast - slow
        signal = macd * self._k_signal + self._signal_ema * (1 - self._k_signal)
        self._current = {"macd": macd, "signal": signal, "hist": macd - signal}
        # state NOT mutated


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

class BollingerBands(Indicator):
    def __init__(
        self,
        timeframe: Timeframe,
        period: int = 20,
        num_std: float = 2.0,
        *,
        mode: str | None = None,
    ):
        super().__init__(timeframe, mode=mode)
        self.period, self.num_std = period, num_std
        self._window: deque[float] = deque(maxlen=period)

    @property
    def lookback(self) -> int:
        return self.period

    @property
    def outputs(self) -> tuple[str, ...]:
        return ("upper", "middle", "lower")

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        if len(bars) < self.period:
            return {"upper": None, "middle": None, "lower": None}
        window = [b.close for b in bars[-self.period:]]
        return self._bb(window)

    def precompute(self, bars: list[Bar]) -> None:
        self._window.clear()
        for b in bars:
            self._window.append(b.close)
        self._current = self._value_from_window()

    def on_bar_closed(self, bars: list[Bar]) -> None:
        self._window.append(bars[-1].close)
        self._current = self._value_from_window()

    def on_forming_bar(self, bars: list[Bar]) -> None:
        if len(self._window) < self.period - 1:
            self._current = {"upper": None, "middle": None, "lower": None}
            return
        window = list(self._window)[-(self.period - 1):] + [bars[-1].close]
        self._current = self._bb(window)

    def _value_from_window(self) -> dict[str, float | None]:
        if len(self._window) < self.period:
            return {"upper": None, "middle": None, "lower": None}
        return self._bb(list(self._window))

    def _bb(self, window: list[float]) -> dict[str, float | None]:
        mean = sum(window) / self.period
        variance = sum((x - mean) ** 2 for x in window) / self.period
        std = math.sqrt(variance)
        return {
            "upper": mean + self.num_std * std,
            "middle": mean,
            "lower": mean - self.num_std * std,
        }


# ---------------------------------------------------------------------------
# Fractals
# ---------------------------------------------------------------------------

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

    def __init__(self, timeframe: Timeframe, *, mode: str | None = None) -> None:
        super().__init__(timeframe, mode=mode)

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

    def precompute(self, bars: list[Bar]) -> None:
        self._current = self.compute(bars)

    def on_bar_closed(self, bars: list[Bar]) -> None:
        self._current = self.compute(bars[-3:] if len(bars) >= 3 else bars)

    def on_forming_bar(self, bars: list[Bar]) -> None:
        self._current = self.compute(bars[-3:] if len(bars) >= 3 else bars)


# ---------------------------------------------------------------------------
# Fair Value Gap
# ---------------------------------------------------------------------------

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

    def __init__(self, timeframe: Timeframe, *, mode: str | None = None) -> None:
        super().__init__(timeframe, mode=mode)

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

    def precompute(self, bars: list[Bar]) -> None:
        self._current = self.compute(bars)

    def on_bar_closed(self, bars: list[Bar]) -> None:
        self._current = self.compute(bars[-3:] if len(bars) >= 3 else bars)

    def on_forming_bar(self, bars: list[Bar]) -> None:
        self._current = self.compute(bars[-3:] if len(bars) >= 3 else bars)


# `fvg` is the common shorthand.
FVG = FairValueGap


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------

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

    def __init__(
        self, timeframe: Timeframe, period: int = 14, *, mode: str | None = None
    ) -> None:
        super().__init__(timeframe, mode=mode)
        self.period = period
        self._smooth_tr: float | None = None
        self._smooth_plus: float | None = None
        self._smooth_minus: float | None = None
        self._adx: float | None = None
        self._prev_bar: Bar | None = None

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

        smooth_tr = self._wilder(trs, p)
        smooth_plus = self._wilder(plus_dms, p)
        smooth_minus = self._wilder(minus_dms, p)

        dx_vals: list[float] = []
        for atr, pdm, mdm in zip(smooth_tr, smooth_plus, smooth_minus):
            if atr == 0:
                dx_vals.append(0.0)
                continue
            pdi = 100.0 * pdm / atr
            mdi = 100.0 * mdm / atr
            di_sum = pdi + mdi
            dx_vals.append(100.0 * abs(pdi - mdi) / di_sum if di_sum != 0 else 0.0)

        adx_vals = self._wilder(dx_vals, p)
        adx = adx_vals[-1]

        last_atr = smooth_tr[-1]
        if last_atr == 0:
            return {"adx": adx, "plus_di": 0.0, "minus_di": 0.0}
        plus_di = 100.0 * smooth_plus[-1] / last_atr
        minus_di = 100.0 * smooth_minus[-1] / last_atr
        return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}

    def precompute(self, bars: list[Bar]) -> None:
        p = self.period
        if len(bars) < 2 * p:
            self._prev_bar = bars[-1] if bars else None
            self._current = {"adx": None, "plus_di": None, "minus_di": None}
            return
        trs, plus_dms, minus_dms = [], [], []
        for prev, cur in zip(bars, bars[1:]):
            tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
            up = cur.high - prev.high
            dn = prev.low - cur.low
            trs.append(tr)
            plus_dms.append(up if up > dn and up > 0 else 0.0)
            minus_dms.append(dn if dn > up and dn > 0 else 0.0)
        smooth_tr = self._wilder(trs, p)
        smooth_plus = self._wilder(plus_dms, p)
        smooth_minus = self._wilder(minus_dms, p)
        self._smooth_tr = smooth_tr[-1]
        self._smooth_plus = smooth_plus[-1]
        self._smooth_minus = smooth_minus[-1]
        dx_vals = []
        for atr, pdm, mdm in zip(smooth_tr, smooth_plus, smooth_minus):
            if atr == 0:
                dx_vals.append(0.0)
                continue
            pdi = 100.0 * pdm / atr
            mdi = 100.0 * mdm / atr
            di_sum = pdi + mdi
            dx_vals.append(100.0 * abs(pdi - mdi) / di_sum if di_sum != 0 else 0.0)
        adx_vals = self._wilder(dx_vals, p)
        self._adx = adx_vals[-1]
        self._prev_bar = bars[-1]
        self._current = self._output(self._smooth_tr, self._smooth_plus, self._smooth_minus, self._adx)

    def on_bar_closed(self, bars: list[Bar]) -> None:
        if self._smooth_tr is None or self._prev_bar is None:
            self._current = self.compute(bars)
            return
        bar = bars[-1]
        tr, plus, minus, adx, plus_di, minus_di = self._step(bar, self._prev_bar)
        self._smooth_tr = tr
        self._smooth_plus = plus
        self._smooth_minus = minus
        self._adx = adx
        self._prev_bar = bar
        self._current = {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}

    def on_forming_bar(self, bars: list[Bar]) -> None:
        if self._smooth_tr is None or self._prev_bar is None:
            self._current = {"adx": None, "plus_di": None, "minus_di": None}
            return
        _, _, _, adx, plus_di, minus_di = self._step(bars[-1], self._prev_bar)
        self._current = {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}
        # state NOT mutated

    def _step(
        self, bar: Bar, prev: Bar
    ) -> tuple[float, float, float, float, float, float]:
        """One Wilder step. Returns (smooth_tr, smooth_plus, smooth_minus, adx, plus_di, minus_di)."""
        p = self.period
        tr = max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close))
        up = bar.high - prev.high
        dn = prev.low - bar.low
        plus_dm = up if up > dn and up > 0 else 0.0
        minus_dm = dn if dn > up and dn > 0 else 0.0
        new_tr = (self._smooth_tr * (p - 1) + tr) / p
        new_plus = (self._smooth_plus * (p - 1) + plus_dm) / p
        new_minus = (self._smooth_minus * (p - 1) + minus_dm) / p
        if new_tr == 0:
            plus_di, minus_di, dx = 0.0, 0.0, 0.0
        else:
            plus_di = 100.0 * new_plus / new_tr
            minus_di = 100.0 * new_minus / new_tr
            di_sum = plus_di + minus_di
            dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum != 0 else 0.0
        new_adx = (self._adx * (p - 1) + dx) / p
        return new_tr, new_plus, new_minus, new_adx, plus_di, minus_di

    def _output(self, tr, plus, minus, adx) -> dict[str, float | None]:
        if tr == 0:
            return {"adx": adx, "plus_di": 0.0, "minus_di": 0.0}
        return {
            "adx": adx,
            "plus_di": 100.0 * plus / tr,
            "minus_di": 100.0 * minus / tr,
        }
