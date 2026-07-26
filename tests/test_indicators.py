from datetime import UTC, datetime, timedelta

from hermes import (
    ATR,
    EMA,
    FVG,
    MACD,
    RSI,
    SMA,
    BollingerBands,
    FairValueGap,
    Fractals,
    Timeframe,
)
from hermes.core import Bar

TF = Timeframe.parse("1h")


def _series(ohlc):
    """ohlc: list of (o,h,l,c). Timestamps auto-increment hourly."""
    t0 = datetime(2023, 1, 1, tzinfo=UTC)
    return [
        Bar(t0 + timedelta(hours=i), TF, o, h, l, c, 1.0)
        for i, (o, h, l, c) in enumerate(ohlc)
    ]


def _closes(closes):
    return _series([(c, c, c, c) for c in closes])


def test_sma():
    sma = SMA(TF, 3)
    assert sma.compute(_closes([1, 2]))["value"] is None
    assert sma.compute(_closes([1, 2, 3]))["value"] == 2.0
    assert sma.compute(_closes([1, 2, 3, 4]))["value"] == 3.0


def test_ema_matches_known_value():
    # EMA(3) seeded with SMA of first 3, then k=0.5.
    ema = EMA(TF, 3)
    # seed = mean(2,4,6)=4; next close 8 -> 8*0.5 + 4*0.5 = 6
    assert ema.compute(_closes([2, 4, 6]))["value"] == 4.0
    assert ema.compute(_closes([2, 4, 6, 8]))["value"] == 6.0


def test_rsi_all_gains_is_100():
    rsi = RSI(TF, 3)
    assert rsi.compute(_closes([1, 2, 3, 4, 5]))["value"] == 100.0


def test_rsi_none_until_warm():
    assert RSI(TF, 14).compute(_closes(list(range(10))))["value"] is None


def test_atr_constant_range():
    # Every bar has H-L = 2, no gaps -> ATR = 2.
    bars = _series([(10, 11, 9, 10)] * 6)
    assert ATR(TF, 3).compute(bars)["value"] == 2.0


def test_macd_lines_present_when_warm():
    out = MACD(TF, fast=3, slow=6, signal=2).compute(_closes(list(range(1, 30))))
    assert out["macd"] is not None and out["signal"] is not None and out["hist"] is not None


def test_bollinger_bands_ordering():
    out = BollingerBands(TF, period=5, num_std=2.0).compute(_closes([1, 2, 3, 4, 5]))
    assert out["lower"] < out["middle"] < out["upper"]
    assert out["middle"] == 3.0


# --- Fractals (candidate = middle of the last triple, bars[-2]) ------------

def test_fractals_warmup():
    assert Fractals(TF).compute(_series([(1, 2, 0, 1), (1, 2, 0, 1)]))["up"] is None
    assert Fractals(TF).lookback == 3


def test_fractals_down_swing_low():
    # lows 10, 8, 9 -> prev.low>cand.low and next.low>=cand.low; highs make `up` false.
    bars = _series([(15, 20, 10, 15), (15, 19, 8, 15), (15, 18, 9, 15)])
    out = Fractals(TF).compute(bars)
    assert out["down"] == 1.0 and out["up"] == 0.0


def test_fractals_up_swing_high():
    # highs 10, 12, 13 -> prev.high<cand.high and next.high>=cand.high (as specified).
    bars = _series([(8, 10, 5, 8), (11, 12, 6, 11), (13, 13, 7, 13)])
    out = Fractals(TF).compute(bars)
    assert out["up"] == 1.0 and out["down"] == 0.0


def test_fractals_neither():
    # prev.high >= cand.high (no up) and prev.low <= cand.low (no down).
    bars = _series([(5, 10, 1, 5), (6, 8, 5, 6), (6, 7, 6, 6)])
    out = Fractals(TF).compute(bars)
    assert out["up"] == 0.0 and out["down"] == 0.0


def test_fractals_up_condition_is_asymmetric():
    # Per the spec, a strictly-rising-highs triple IS an `up` fractal (right side is >=,
    # not a strict local max). Documented behaviour, pinned here so it can't drift.
    bars = _series([(1, 2, 0, 1), (1, 3, 1, 2), (1, 4, 2, 3)])
    assert Fractals(TF).compute(bars)["up"] == 1.0


# --- Fair Value Gap (gap defined by the outer candles of the last triple) ---

def test_fvg_warmup():
    assert FairValueGap(TF).compute(_series([(1, 2, 0, 1)]))["top"] is None
    assert FVG is FairValueGap


def test_fvg_bullish_zone():
    # c1.high (10) < c3.low (12) -> bullish gap [10, 12]
    bars = _series([(9, 10, 8, 9), (11, 13, 10, 12), (13, 15, 12, 14)])
    out = FairValueGap(TF).compute(bars)
    assert out["bullish"] == 1.0 and out["bearish"] == 0.0
    assert out["bottom"] == 10 and out["top"] == 12


def test_fvg_bearish_zone():
    # c1.low (20) > c3.high (18) -> bearish gap [18, 20]
    bars = _series([(21, 22, 20, 21), (18, 19, 17, 18), (17, 18, 16, 17)])
    out = FairValueGap(TF).compute(bars)
    assert out["bearish"] == 1.0 and out["bullish"] == 0.0
    assert out["top"] == 20 and out["bottom"] == 18


def test_fvg_no_gap():
    bars = _series([(10, 12, 8, 11), (10, 13, 9, 12), (11, 14, 10, 13)])  # overlapping
    out = FairValueGap(TF).compute(bars)
    assert out["bullish"] == 0.0 and out["bearish"] == 0.0
    assert out["top"] is None and out["bottom"] is None
