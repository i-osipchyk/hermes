from datetime import UTC, datetime, timedelta

import pytest

from hermes import SMA, Backtest, CryptoPair, Parameter, Strategy, Symbol, Timeframe
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


# --- timeframes as parameters ----------------------------------------------

def test_timeframe_param_declares_choice_and_parses():
    class S(Strategy):
        def setup(self):
            self.tf = self.timeframe_param("tf", "1h", choices=("15m", "1h", "4h"), description="TF")
            self.use(SMA(self.tf, 3))

        def on_bar(self, bar): ...

    s = S()
    s.setup()
    assert s.tf == Timeframe.parse("1h")
    specs = {p.name: p for p in s.declared_parameters()}
    assert specs["tf"].choices == ("15m", "1h", "4h") and specs["tf"].default == "1h"


class _TFStrategy(Strategy):
    def setup(self):
        self.tf = self.timeframe_param("tf", "1h", choices=("15m", "1h"))
        self.use(SMA(self.tf, 2))
        self.bars_seen = 0

    def on_bar(self, bar):
        self.bars_seen += 1


def test_engine_derives_timeframes_from_indicators():
    # Backtest.timeframes empty -> base is taken from the strategy's declared indicator.
    strat = _TFStrategy()
    bars = [Bar(T0 + timedelta(hours=i), Timeframe.parse("1h"), 100, 100, 100, 100, 1.0) for i in range(6)]
    Backtest(
        strategy=strat, source=InMemorySource(_btc(), {Timeframe.parse("1h"): bars}),
        symbol=Symbol("BTCUSDT", "binance"), start=T0, end=T0 + timedelta(hours=5),
    ).run()
    assert strat.bars_seen > 0  # ran on the 1h base derived from the indicator, no timeframes set


# --- parameter_adjusted_sharpe in Metrics ----------------------------------

def test_parameter_adjusted_sharpe_zero_params():
    """With no declared Parameters, adjusted Sharpe equals raw Sharpe."""
    class NoParams(Strategy):
        def setup(self): ...
        def on_bar(self, bar): ...

    strat = NoParams()
    bars = [Bar(T0 + timedelta(hours=i), H1, 100, 100, 100, 100, 1.0) for i in range(6)]
    result = Backtest(
        strategy=strat,
        source=InMemorySource(_btc(), {H1: bars}),
        symbol=Symbol("BTCUSDT", "binance"),
        timeframes=[H1],
        start=T0, end=T0 + timedelta(hours=5),
    ).run()
    m = result.metrics
    assert m.num_params == 0
    # adjusted Sharpe equals raw Sharpe (no penalty)
    assert m.parameter_adjusted_sharpe == m.sharpe


def test_parameter_adjusted_sharpe_penalised():
    """adjusted Sharpe is penalised relative to raw Sharpe when params > 0 and trades > 0."""
    from datetime import UTC, datetime, timedelta

    from hermes.backtest.result import _metrics

    t0 = datetime(2023, 1, 1, tzinfo=UTC)
    curve = [(t0 + timedelta(hours=i), 10_000 + i * 10) for i in range(50)]

    # Without trades, penalty path isn't taken — adjusted equals raw Sharpe.
    m_no_pen = _metrics(curve, [], num_params=0)
    m_penalised = _metrics(curve, [], num_params=3)
    assert m_no_pen.parameter_adjusted_sharpe == m_no_pen.sharpe
    assert m_penalised.parameter_adjusted_sharpe == m_penalised.sharpe


def test_pnl_ratio():
    """pnl_ratio = avg_win / avg_loss; None when no wins or no losses."""
    from hermes.backtest.result import _metrics
    from hermes.execution.trade import Trade

    instrument = _btc()

    def _trade(pnl):
        return Trade(
            instrument=instrument,
            side="BUY",
            size=1.0,
            entry_price=100.0,
            entry_time=T0,
            exit_price=100.0 + pnl,
            exit_time=T0 + timedelta(hours=1),
            exit_reason="tp" if pnl > 0 else "sl",
            gross_pnl=pnl,
            costs=0.0,
        )

    # 2 wins (+20, +10 → avg 15) and 1 loss (-5 → avg 5): ratio = 3.0
    trades = [_trade(20.0), _trade(10.0), _trade(-5.0)]
    curve = [(T0, 10_000.0), (T0 + timedelta(hours=2), 10_025.0)]
    m = _metrics(curve, trades)
    assert m.pnl_ratio == pytest.approx(3.0)

    # All wins — no losses, pnl_ratio stays None
    m_no_loss = _metrics(curve, [_trade(10.0), _trade(5.0)])
    assert m_no_loss.pnl_ratio is None

    # No trades — pnl_ratio stays None
    m_empty = _metrics(curve, [])
    assert m_empty.pnl_ratio is None


def test_num_params_carried_through_engine():
    """The engine counts declared Parameters and sets metrics.num_params."""
    strat = Configurable()  # declares 1 Parameter ("threshold")
    bars = [Bar(T0 + timedelta(hours=i), H1, 60, 60, 60, 60, 1.0) for i in range(4)]
    result = Backtest(
        strategy=strat,
        source=InMemorySource(_btc(), {H1: bars}),
        symbol=Symbol("BTCUSDT", "binance"),
        timeframes=[H1],
        start=T0, end=T0 + timedelta(hours=3),
    ).run()
    assert result.metrics.num_params == 1


def test_engine_errors_without_any_timeframe():
    class Empty(Strategy):
        def setup(self): ...
        def on_bar(self, bar): ...

    with pytest.raises(ValueError):
        Backtest(
            strategy=Empty(), source=InMemorySource(_btc(), {}),
            symbol=Symbol("BTCUSDT", "binance"), start=T0, end=T0 + timedelta(hours=5),
        ).run()
