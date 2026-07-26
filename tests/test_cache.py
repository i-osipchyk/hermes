from datetime import UTC, datetime, timedelta

from hermes import CryptoPair, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import BarCache

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _btc():
    return CryptoPair(Symbol("BTCUSDT", "binance"), base_asset="BTC", quote_currency="USDT", tick_size=0.01)


def _bars(n):
    return [Bar(T0 + timedelta(hours=i), H1, 100 + i, 101 + i, 99 + i, 100 + i, 1.0) for i in range(n)]


def test_write_then_read(tmp_path):
    cache = BarCache(tmp_path)
    inst = _btc()
    cache.write(inst, H1, _bars(5))
    got = cache.read(inst, H1, T0, T0 + timedelta(hours=4))
    assert len(got) == 5
    assert got[0].close == 100 and got[-1].close == 104


def test_missing_ranges(tmp_path):
    cache = BarCache(tmp_path)
    inst = _btc()
    assert cache.missing_ranges(inst, H1, T0, T0 + timedelta(hours=10)) == [
        (T0, T0 + timedelta(hours=10))
    ]
    cache.write(inst, H1, _bars(5))  # covers hours 0..4
    gaps = cache.missing_ranges(inst, H1, T0, T0 + timedelta(hours=10))
    # Only the tail after the cached max is missing.
    assert len(gaps) == 1
    assert gaps[0][1] == T0 + timedelta(hours=10)


def test_dedupe_on_rewrite(tmp_path):
    cache = BarCache(tmp_path)
    inst = _btc()
    cache.write(inst, H1, _bars(3))
    cache.write(inst, H1, _bars(3))  # same timestamps again
    assert len(cache.read(inst, H1, T0, T0 + timedelta(hours=2))) == 3
