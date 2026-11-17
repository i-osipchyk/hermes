"""Phase 2 step 1 (ADR-0012): the holdout is carved by policy, frozen at first touch, and
enforced in the data layer. Seams: ``HoldoutPolicy``, ``HoldoutSource``, the CLI."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from hermes import CryptoPair, Symbol, Timeframe
from hermes.cli import EXIT_ERR, EXIT_OK, main
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.research.holdout import HoldoutPolicy, HoldoutSource, HoldoutViolation

D1 = Timeframe.parse("1d")
SYM = Symbol("BTCUSDT", "memory")
DAY0 = datetime(2018, 1, 1, tzinfo=UTC)
_FAR_END = datetime(2100, 1, 1, tzinfo=UTC)


def _source(days: int, first: datetime = DAY0) -> InMemorySource:
    inst = CryptoPair(SYM, base_asset="BTC", quote_currency="USDT", tick_size=0.01)
    bars = [Bar(first + timedelta(days=i), D1, 100, 101, 99, 100, 1.0) for i in range(days)]
    return InMemorySource(inst, {D1: bars})


def test_holdout_is_the_last_fifth_of_a_long_history(tmp_path):
    src = _source(5 * 365)            # span = 1824 days; 20% = 364.8d > 6 months
    start = HoldoutPolicy(tmp_path / "h.json").holdout_start(src, SYM, D1)
    # last bar 2022-12-30; 364.8 days earlier is 2021-12-30 04:48, floored to the day.
    assert start == datetime(2021, 12, 30, tzinfo=UTC)


def test_holdout_is_never_shorter_than_six_months(tmp_path):
    src = _source(2 * 365)            # 20% of the span would be ~146 days
    start = HoldoutPolicy(tmp_path / "h.json").holdout_start(src, SYM, D1)
    last = DAY0 + timedelta(days=2 * 365 - 1)
    assert last - start >= timedelta(days=183)
    assert last - start < timedelta(days=184 + 1)


def test_a_fourteen_month_history_carves_no_holdout(tmp_path):
    src = _source(425)                # 183d held would leave 242d (< 12 months) in-sample
    assert HoldoutPolicy(tmp_path / "h.json").holdout_start(src, SYM, D1) is None


def test_holdout_start_is_frozen_at_first_touch_and_survives_new_data(tmp_path):
    path = tmp_path / "holdouts.json"
    first = HoldoutPolicy(path).holdout_start(_source(5 * 365), SYM, D1)

    # a year of new bars arrives; a fresh process (new policy object) asks again
    later = HoldoutPolicy(path).holdout_start(_source(6 * 365), SYM, D1)

    assert later == first                       # it did not slide
    entry = json.loads(path.read_text())["memory/BTCUSDT/1d"]
    assert entry["holdout_start"] == first.isoformat()
    assert entry["looks"] == [] and entry["frozen_at"]


def test_a_history_too_short_to_carve_is_frozen_as_no_holdout(tmp_path):
    path = tmp_path / "holdouts.json"
    assert HoldoutPolicy(path).holdout_start(_source(425), SYM, D1) is None
    # more data later does not retroactively carve one: the agent already saw all of it.
    assert HoldoutPolicy(path).holdout_start(_source(5 * 365), SYM, D1) is None


# --- HoldoutSource ------------------------------------------------------------

def _guarded(tmp_path, days=5 * 365):
    inner = _source(days)
    policy = HoldoutPolicy(tmp_path / "holdouts.json")
    return HoldoutSource(inner, policy), inner, policy.holdout_start(inner, SYM, D1)


def test_a_request_past_the_holdout_start_raises(tmp_path):
    src, inner, hs = _guarded(tmp_path)
    inst = src.get_instrument(SYM)
    with pytest.raises(HoldoutViolation, match="holdout"):
        src.history(inst, D1, DAY0, hs + timedelta(days=1))
    with pytest.raises(HoldoutViolation):          # no clipping: even the whole range raises
        src.history(inst, D1, DAY0, DAY0 + timedelta(days=5 * 365))


def test_a_request_up_to_the_holdout_start_never_returns_a_held_out_bar(tmp_path):
    src, _, hs = _guarded(tmp_path)
    bars = src.history(src.get_instrument(SYM), D1, DAY0, hs)
    assert bars and all(b.timestamp < hs for b in bars)
    assert bars[0].timestamp == DAY0


def test_a_history_with_no_holdout_passes_straight_through(tmp_path):
    src, inner, hs = _guarded(tmp_path, days=425)
    assert hs is None
    inst = src.get_instrument(SYM)
    assert src.history(inst, D1, DAY0, _FAR_END) == inner.history(inst, D1, DAY0, _FAR_END)


def test_the_guard_is_transparent_about_what_it_wraps(tmp_path):
    src, inner, _ = _guarded(tmp_path)
    assert src.name == inner.name
    assert src.supported_timeframes() == inner.supported_timeframes()
    assert src.data_epoch("BTCUSDT") == inner.data_epoch("BTCUSDT")


# --- the research CLI ---------------------------------------------------------

_LONG_STRATEGY = '''
from datetime import datetime, timedelta, timezone
from hermes import Backtest, Strategy, Symbol, Timeframe, Units
from hermes.core import Bar, CryptoPair
from hermes.data import InMemorySource

D1 = Timeframe.parse("1d")
T0 = datetime(2018, 1, 1, tzinfo=timezone.utc)
SYM = Symbol("BTCUSDT", "binance")


class Buy(Strategy):
    def setup(self): ...

    def on_bar(self, bar):
        if self.venue.position().is_flat:
            self.buy(Units(1), take_profit=bar.close * 1.01)


def build_backtest(**overrides):
    inst = CryptoPair(SYM, base_asset="BTC", quote_currency="USDT", tick_size=0.01)
    bars = [Bar(T0 + timedelta(days=i), D1, 100 + i % 7, 102 + i % 7, 99, 101 + i % 7, 5.0)
            for i in range(5 * 365)]
    kw = dict(strategy=Buy(), source=InMemorySource(inst, {D1: bars}), symbol=SYM,
              timeframes=[D1], start=bars[0].timestamp, end=bars[-1].timestamp,
              starting_cash=10_000)
    kw.update(overrides)
    return Backtest(**kw)
'''


@pytest.fixture
def long_project(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "buy.py").write_text(_LONG_STRATEGY)
    return ["--dir", str(sdir), "--runs-dir", str(tmp_path / "runs")]


def test_a_research_run_defaults_its_end_to_the_holdout_start(long_project, capsys):
    assert main(["run", "buy", *long_project, "--json", "--note", "t"]) == EXIT_OK
    d = json.loads(capsys.readouterr().out)
    assert d["end"] == "2021-12-30T00:00:00+00:00"


def test_a_research_run_with_an_end_inside_the_holdout_is_refused(long_project, capsys):
    code = main(["run", "buy", *long_project, "--end", "2022-06-01", "--json", "--note", "t"])
    assert code == EXIT_ERR
    assert "holdout" in capsys.readouterr().err.lower()


# --- real (cached) sources ----------------------------------------------------

def test_carving_a_holdout_does_not_poison_the_bar_cache_against_future_data(tmp_path):
    """The policy must learn the history's extent without telling a caching source that
    the empty future has been 'checked' — or bars published later are never fetched."""
    pytest.importorskip("pyarrow")
    from hermes.data import BarCache, DataSource

    class Provider(DataSource):
        name = "fake"

        def __init__(self, cache):
            self.cache, self.available = cache, 5 * 365
            self.inst = CryptoPair(SYM, base_asset="BTC", quote_currency="USDT", tick_size=0.01)

        def get_instrument(self, symbol):
            return self.inst

        def supported_timeframes(self):
            return {D1}

        def history(self, instrument, timeframe, start, end):
            for gs, ge in self.cache.missing_ranges(instrument, timeframe, start, end):
                got = [Bar(DAY0 + timedelta(days=i), D1, 100, 101, 99, 100, 1.0)
                       for i in range(self.available) if gs <= DAY0 + timedelta(days=i) <= ge]
                self.cache.write(instrument, timeframe, got)
                self.cache.mark_checked(instrument, timeframe, gs, ge)
            return self.cache.read(instrument, timeframe, start, end)

    cache = BarCache(tmp_path / "bars")
    provider = Provider(cache)
    # 'now' is the day after the last bar the provider has published
    now = DAY0 + timedelta(days=5 * 365)
    HoldoutPolicy(tmp_path / "h.json", now=now).holdout_start(provider, SYM, D1)

    provider.available += 30                      # a month of new bars is published
    later = provider.history(provider.inst, D1, now, now + timedelta(days=29))
    assert len(later) == 30
