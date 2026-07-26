from datetime import UTC, datetime, timedelta

from hermes import Backtest, CryptoPair, Parameter, Strategy, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.webui import discovery

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _btc():
    return CryptoPair(Symbol("BTCUSDT", "binance"), base_asset="BTC", quote_currency="USDT", tick_size=0.01)


def _bars(close, n=4):
    return [Bar(T0 + timedelta(hours=i), H1, close, close, close, close, 1.0) for i in range(n)]


class Configurable(Strategy):
    def setup(self):
        self.threshold = self.param(
            Parameter("threshold", 100.0, bounds=(0.0, 1000.0), description="Buy above")
        )
        self.bought = False

    def on_bar(self, bar):
        if not self.bought and self.venue.position().is_flat and bar.close > self.threshold:
            self.buy(1)
            self.bought = True


# --- declaration & override ------------------------------------------------

def test_declared_parameters_exposed_after_setup():
    s = Configurable()
    s.setup()
    specs = s.declared_parameters()
    assert [p.name for p in specs] == ["threshold"]
    assert specs[0].default == 100.0 and specs[0].bounds == (0.0, 1000.0)


def _run(threshold=None) -> Configurable:
    strat = Configurable()
    params = {"threshold": threshold} if threshold is not None else {}
    Backtest(
        strategy=strat, source=InMemorySource(_btc(), {H1: _bars(60)}),
        symbol=Symbol("BTCUSDT", "binance"), timeframes=[H1],
        start=T0, end=T0 + timedelta(hours=3), params=params,
    ).run()
    return strat


def test_param_override_changes_behavior():
    assert _run().bought is False                    # default 100 > close 60 -> no buy
    assert _run(threshold=50.0).bought is True        # override 50 < 60 -> buys


# --- discovery introspection (what the UI renders) -------------------------

_PARAM_STRATEGY = '''
from datetime import datetime, timezone, timedelta
from hermes import Backtest, Parameter, Strategy, Symbol, Timeframe
from hermes.core import Bar, CryptoPair
from hermes.data import InMemorySource

H1 = Timeframe.parse("1h")


class S(Strategy):
    def setup(self):
        self.rr = self.param(Parameter("rr", 2.0, bounds=(1.0, 5.0), description="Reward:risk"))

    def on_bar(self, bar): ...


def build_backtest(**overrides):
    inst = CryptoPair(Symbol("BTCUSDT", "binance"), base_asset="BTC", quote_currency="USDT", tick_size=0.01)
    t0 = datetime(2023, 1, 1, tzinfo=timezone.utc)
    bars = [Bar(t0 + timedelta(hours=i), H1, 1, 1, 1, 1, 1.0) for i in range(3)]
    bt = Backtest(strategy=S(), source=InMemorySource(inst, {H1: bars}),
                  symbol=Symbol("BTCUSDT", "binance"), timeframes=[H1])
    for k, v in overrides.items():
        setattr(bt, k, v)
    return bt
'''


def test_discovery_declared_parameters(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "s.py").write_text(_PARAM_STRATEGY)
    entry = discovery.discover(sdir)[0]
    specs = discovery.declared_parameters(entry)
    assert [p.name for p in specs] == ["rr"]
    assert specs[0].bounds == (1.0, 5.0) and specs[0].description == "Reward:risk"
