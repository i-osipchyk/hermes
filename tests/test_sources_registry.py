import pytest

from hermes.data import BinanceFuturesSource, BinanceSource, PepperstoneSource, YFinanceSource
from hermes.webui import discovery, sources

_STRATEGY = '''
from datetime import datetime, timezone, timedelta
from hermes import Backtest, Strategy, Symbol, Timeframe
from hermes.core import Bar, CryptoPerpetual
from hermes.data import BinanceFuturesSource, InMemorySource

H1 = Timeframe.parse("1h")


class S(Strategy):
    def setup(self): ...
    def on_bar(self, bar): ...


def build_backtest(**overrides):
    bt = Backtest(strategy=S(), source=BinanceFuturesSource(),
                  symbol=Symbol("BTCUSDT", "binance-futures"), timeframes=[H1])
    for k, v in overrides.items():
        setattr(bt, k, v)
    return bt
'''


def test_registry_lists_and_builds_sources():
    names = sources.source_names()
    assert {"binance", "binance-futures", "yfinance", "pepperstone"} <= set(names)
    assert isinstance(sources.build_source("binance"), BinanceSource)
    assert isinstance(sources.build_source("binance-futures"), BinanceFuturesSource)
    assert isinstance(sources.build_source("yfinance"), YFinanceSource)
    # cTrader builds without credentials (errors only on fetch).
    assert isinstance(sources.build_source("pepperstone"), PepperstoneSource)


def test_unknown_source_raises():
    with pytest.raises(ValueError):
        sources.build_source("nope")


def test_configured_backtest_switches_source(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "s.py").write_text(_STRATEGY)
    entry = discovery.discover(sdir)[0]

    # Strategy ships on binance-futures; switch it to binance spot.
    from datetime import UTC, datetime

    bt = discovery.configured_backtest(
        entry, source_name="binance", ticker="BTCUSDT",
        start=datetime(2024, 1, 1, tzinfo=UTC), end=datetime(2024, 2, 1, tzinfo=UTC),
        starting_cash=1_000,
    )
    assert isinstance(bt.source, BinanceSource)
    assert bt.symbol.source == "binance"  # symbol re-namespaced to the new source
