from datetime import UTC, datetime, timedelta

from hermes import (
    ADX,
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
    # highs 10, 12, 11 -> a peak: prev.high < cand.high and next.high <= cand.high.
    bars = _series([(8, 10, 5, 8), (11, 12, 6, 11), (11, 11, 7, 11)])
    out = Fractals(TF).compute(bars)
    assert out["up"] == 1.0 and out["down"] == 0.0


def test_fractals_neither():
    # prev.high >= cand.high (no up) and prev.low <= cand.low (no down).
    bars = _series([(5, 10, 1, 5), (6, 8, 5, 6), (6, 7, 6, 6)])
    out = Fractals(TF).compute(bars)
    assert out["up"] == 0.0 and out["down"] == 0.0


def test_fractals_rising_highs_is_not_up():
    # A strictly-rising-highs triple is NOT a swing high (the right neighbour exceeds it).
    bars = _series([(1, 2, 0, 1), (1, 3, 1, 2), (1, 4, 2, 3)])
    assert Fractals(TF).compute(bars)["up"] == 0.0


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


# --- ADX (Wilder's Average Directional Index) --------------------------------

def test_adx_outputs():
    adx = ADX(TF, period=5)
    assert adx.outputs == ("adx", "plus_di", "minus_di")
    assert adx.lookback == 10


def test_adx_none_until_warm():
    adx = ADX(TF, period=5)
    # Need 2*period = 10 bars; fewer should return all None.
    bars = _series([(10, 11, 9, 10)] * 9)
    out = adx.compute(bars)
    assert out["adx"] is None and out["plus_di"] is None and out["minus_di"] is None


def test_adx_values_present_when_warm():
    adx = ADX(TF, period=5)
    # 10 bars is exactly the minimum (2 * period).
    bars = _series([(10 + i, 11 + i, 9 + i, 10 + i) for i in range(10)])
    out = adx.compute(bars)
    assert out["adx"] is not None
    assert out["plus_di"] is not None
    assert out["minus_di"] is not None


def test_adx_bounded_0_to_100():
    adx = ADX(TF, period=5)
    bars = _series([(10 + i, 11 + i, 9 + i, 10 + i) for i in range(20)])
    out = adx.compute(bars)
    assert 0.0 <= out["adx"] <= 100.0
    assert 0.0 <= out["plus_di"] <= 100.0
    assert 0.0 <= out["minus_di"] <= 100.0


def test_adx_uptrend_plus_di_dominates():
    # Consistently rising bars: each bar opens and closes higher, large up-moves.
    adx = ADX(TF, period=5)
    bars = _series([(10 + i * 2, 12 + i * 2, 9 + i * 2, 11 + i * 2) for i in range(20)])
    out = adx.compute(bars)
    assert out["plus_di"] > out["minus_di"]


def test_adx_downtrend_minus_di_dominates():
    # Consistently falling bars.
    adx = ADX(TF, period=5)
    bars = _series([(100 - i * 2, 102 - i * 2, 99 - i * 2, 100 - i * 2) for i in range(20)])
    out = adx.compute(bars)
    assert out["minus_di"] > out["plus_di"]


def test_adx_strong_trend_higher_than_choppy():
    # A sustained trend should produce a higher ADX than a flat/choppy series.
    p = 5
    adx = ADX(TF, period=p)
    trending = _series([(10 + i, 11 + i, 9 + i, 10 + i) for i in range(30)])
    choppy = _series([(10 + (i % 2), 11 + (i % 2), 9 + (i % 2), 10 + (i % 2)) for i in range(30)])
    adx_trend = adx.compute(trending)["adx"]
    adx_chop = adx.compute(choppy)["adx"]
    assert adx_trend > adx_chop
