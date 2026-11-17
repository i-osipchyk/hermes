"""Audit F13: a bounded integer Parameter must stay integer in the walk-forward grid
(a period of 37.5 is not a period)."""

from datetime import UTC, datetime, timedelta

from hermes import Backtest, CryptoPair, Parameter, Strategy, Symbol, Timeframe, WalkForward
from hermes.core import Bar
from hermes.data import InMemorySource

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)
SYM = Symbol("BTCUSDT", "binance")


class _S(Strategy):
    def setup(self):
        self.n = self.param(Parameter("n", 20, bounds=(10, 60)))
        self.f = self.param(Parameter("f", 0.5, bounds=(0.0, 1.0)))

    def on_bar(self, bar):
        pass


def _grid(strategy):
    inst = CryptoPair(SYM, base_asset="BTC", quote_currency="USDT", tick_size=0.01)
    bars = [Bar(T0 + timedelta(hours=i), H1, 100, 100, 100, 100, 1.0) for i in range(50)]
    bt = Backtest(strategy=strategy, source=InMemorySource(inst, {H1: bars}), symbol=SYM,
                  timeframes=[H1], start=bars[0].timestamp, end=bars[-1].timestamp,
                  starting_cash=1000)
    wf = WalkForward(template=bt, total_start=bt.start, total_end=bt.end)
    return wf._discover_grid()


def test_a_bounded_integer_parameter_is_searched_over_integers():
    grid = _grid(_S())
    assert all(isinstance(v, int) for v in grid["n"]), grid["n"]
    assert grid["n"][0] == 10 and grid["n"][-1] == 60
    assert len(set(grid["n"])) == len(grid["n"])


def test_a_float_parameter_is_still_searched_over_floats():
    assert _grid(_S())["f"] == [0.0, 0.25, 0.5, 0.75, 1.0]
