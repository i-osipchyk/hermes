"""The cache backstop for the holdout (ADR-0012, audit follow-up): with HERMES_RESEARCH=1 a
direct ``BarCache.read`` cannot reach held-out bars, nor read an instrument whose holdout has
not been frozen yet. The sanctioned paths (carving, the unlock) suspend it in-process."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from hermes import CryptoPair, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import BarCache
from hermes.data.cache import guard_suspended
from hermes.research.holdout import HoldoutPolicy, HoldoutSource, HoldoutViolation

D1 = Timeframe.parse("1d")
SYM = Symbol("BTCUSDT", "binance")
DAY0 = datetime(2018, 1, 1, tzinfo=UTC)
HELD = datetime(2021, 12, 30, tzinfo=UTC)


def _inst():
    return CryptoPair(SYM, base_asset="BTC", quote_currency="USDT", tick_size=0.01)


def _filled(tmp_path, days=5 * 365):
    cache = BarCache(tmp_path / "bars")
    cache.write(_inst(), D1, [Bar(DAY0 + timedelta(days=i), D1, 100, 101, 99, 100, 1.0)
                              for i in range(days)])
    return cache


def _freeze(tmp_path, start: datetime | None):
    path = tmp_path / "holdouts.json"      # conftest points HERMES_HOLDOUTS_PATH here
    path.write_text(json.dumps({f"binance/BTCUSDT/{D1}": {
        "holdout_start": start.isoformat() if start else None,
        "frozen_at": "2026-01-01T00:00:00+00:00", "looks": []}}))


@pytest.fixture
def research(monkeypatch):
    monkeypatch.setenv("HERMES_RESEARCH", "1")


def test_without_the_flag_nothing_changes(tmp_path):
    cache = _filled(tmp_path)
    assert cache.read(_inst(), D1, DAY0, DAY0 + timedelta(days=5 * 365))   # no holdouts.json


def test_research_read_into_the_holdout_raises(tmp_path, research):
    cache = _filled(tmp_path)
    _freeze(tmp_path, HELD)
    with pytest.raises(HoldoutViolation, match="holdout"):
        cache.read(_inst(), D1, DAY0, HELD + timedelta(days=30))


def test_research_read_before_the_holdout_is_allowed(tmp_path, research):
    cache = _filled(tmp_path)
    _freeze(tmp_path, HELD)
    got = cache.read(_inst(), D1, DAY0, HELD)
    assert got and max(b.timestamp for b in got) <= HELD


def test_unfrozen_instrument_is_refused_not_assumed_open(tmp_path, research):
    cache = _filled(tmp_path)
    with pytest.raises(HoldoutViolation, match="not frozen"):
        cache.read(_inst(), D1, DAY0, DAY0 + timedelta(days=10))


def test_an_instrument_frozen_without_holdout_is_readable(tmp_path, research):
    cache = _filled(tmp_path)
    _freeze(tmp_path, None)                  # history too short to carve
    assert cache.read(_inst(), D1, DAY0, DAY0 + timedelta(days=5 * 365))


def test_suspension_is_scoped(tmp_path, research):
    cache = _filled(tmp_path)
    with guard_suspended():
        assert cache.read(_inst(), D1, DAY0, DAY0 + timedelta(days=100))
    with pytest.raises(HoldoutViolation):
        cache.read(_inst(), D1, DAY0, DAY0 + timedelta(days=100))


def test_first_touch_through_holdout_source_carves_then_reads(tmp_path, research):
    """The ordinary path must still work under the flag: HoldoutSource carves (reading the
    whole history under suspension), freezes, then reads only before the holdout."""
    from hermes.data.source import DataSource

    cache = _filled(tmp_path)
    inst = _inst()

    class CacheSource(DataSource):
        name = "binance"

        def get_instrument(self, symbol):
            return inst

        def supported_timeframes(self):
            return {D1}

        def history(self, instrument, timeframe, start, end):
            return cache.read(instrument, timeframe, start, end)

    guarded = HoldoutSource(CacheSource(), HoldoutPolicy())
    assert guarded.holdout_start(SYM, D1) == HELD
    assert guarded.history(inst, D1, DAY0, HELD)
    with pytest.raises(HoldoutViolation):
        guarded.history(inst, D1, DAY0, HELD + timedelta(days=5))
    with pytest.raises(HoldoutViolation):                    # bypassing the wrapper
        cache.read(inst, D1, DAY0, HELD + timedelta(days=5))
