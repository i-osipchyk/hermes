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


def test_missing_ranges_detects_internal_gap(tmp_path):
    """Two disjoint writes (e.g. a recent window fetched first, an older window
    fetched later) must not make the hole between them look covered just
    because *some* row exists before it and *some* row exists after it."""
    cache = BarCache(tmp_path)
    inst = _btc()
    early = _bars(3)  # hours 0..2
    late = [
        Bar(T0 + timedelta(hours=100 + i), H1, 100 + i, 101 + i, 99 + i, 100 + i, 1.0)
        for i in range(3)
    ]  # hours 100..102
    cache.write(inst, H1, early)
    cache.write(inst, H1, late)

    # A query spanning both writes must report the middle as missing, even
    # though it starts after `early`'s min and ends before `late`'s max.
    gaps = cache.missing_ranges(inst, H1, T0, T0 + timedelta(hours=102))
    assert len(gaps) == 1
    gap_start, gap_end = gaps[0]
    assert gap_start == T0 + timedelta(hours=2)
    assert gap_end == T0 + timedelta(hours=100)


def test_dedupe_on_rewrite(tmp_path):
    cache = BarCache(tmp_path)
    inst = _btc()
    cache.write(inst, H1, _bars(3))
    cache.write(inst, H1, _bars(3))  # same timestamps again
    assert len(cache.read(inst, H1, T0, T0 + timedelta(hours=2))) == 3


def test_mark_checked_suppresses_confirmed_empty_gap(tmp_path):
    """A legitimately empty span (e.g. a weekend) that's already been fetched and
    found empty must not keep showing up as "missing" on every later call --
    otherwise every such gap gets re-queried against the provider forever."""
    cache = BarCache(tmp_path)
    inst = _btc()
    # bars either side of an empty hour-100..hour-102 span
    cache.write(inst, H1, _bars(3))  # hours 0..2
    cache.write(
        inst,
        H1,
        [Bar(T0 + timedelta(hours=100 + i), H1, 100, 101, 99, 100, 1.0) for i in range(3)],
    )  # hours 100..102

    gaps = cache.missing_ranges(inst, H1, T0, T0 + timedelta(hours=102))
    assert len(gaps) == 1
    gap_start, gap_end = gaps[0]

    # Simulate the source fetching that gap and finding nothing there.
    cache.mark_checked(inst, H1, gap_start, gap_end)

    assert cache.missing_ranges(inst, H1, T0, T0 + timedelta(hours=102)) == []
