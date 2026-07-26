from datetime import UTC, datetime, timedelta

from hermes import ATR, EMA, MACD, RSI, SMA, BollingerBands, Timeframe
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
