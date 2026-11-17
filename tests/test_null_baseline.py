"""Phase 2 step 4 (docs/phase-2-validation-plan.md, audit G1): a null test for rule-based
strategies. Entries are replayed through the real engine at a randomly shifted time, so
costs and fills are the engine's own; the observed Sharpe is ranked against that null.

Seam: ``run_null_test(backtest, result, n, seed)``. Planted answers: a random-signal strategy
is not extreme; a strategy keyed to a real conditional drift is."""

import json
import random
from datetime import UTC, datetime, timedelta

from hermes import Backtest, CryptoPair, Strategy, Symbol, Timeframe
from hermes.backtest.null_baseline import run_null_test
from hermes.cli import EXIT_OK, main
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.execution import CostModel, FinancingModel, PercentCommission, SlippageModel, SpreadModel
from hermes.research.analysis import ALL_AXES, DEFAULT_AXES, analyze
from tests.test_cli import STRATEGY

D1 = Timeframe.parse("1d")
T0 = datetime(2020, 1, 1, tzinfo=UTC)
SYM = Symbol("X", "memory")
HOLD = 3


def _inst():
    return CryptoPair(SYM, base_asset="X", quote_currency="USD", tick_size=0.01)


def _costs():
    return CostModel(PercentCommission(0.0005), SpreadModel(0.0), SlippageModel(0.0, 0.0),
                     FinancingModel(0.0))


def _events(n=500):
    """Irregularly spaced event bars (a periodic pattern would re-align under some shifts)."""
    rng, out, i = random.Random(123), set(), 5
    while i < n - HOLD - 2:
        out.add(i)
        i += rng.randint(6, 18)
    return out


EVENTS = _events()


def _bars(seed, n=500, planted=False):
    """A random walk. With ``planted``, each event bar is followed by three +1.5% bars."""
    rng = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        drift = 0.015 if planted and any(i - e in range(1, HOLD + 1) for e in EVENTS) else 0.0
        o, c = price, price * (1 + drift + rng.gauss(0, 0.01))
        out.append(Bar(T0 + timedelta(days=i), D1, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 1.0))
        price = c
    return out


class _Holds(Strategy):
    """Enters on ``signal(i)`` and leaves ``HOLD`` bars later."""

    def setup(self):
        self.i, self.age = -1, {}

    def signal(self, i):
        raise NotImplementedError

    def on_bar(self, bar):
        self.i += 1
        for trade in self.venue.open_trades():
            self.age[id(trade)] = self.age.get(id(trade), 0) + 1
            if self.age[id(trade)] >= HOLD:
                self.close(trade)
        if self.venue.position().is_flat and self.signal(self.i):
            self.buy(1.0)


class _RandomSignal(_Holds):
    def setup(self):
        super().setup()
        self.rng = random.Random(99)

    def signal(self, i):
        return self.rng.random() < 0.15


class _EventSignal(_Holds):
    def signal(self, i):
        return i in EVENTS


def _backtest(strategy, bars):
    return Backtest(
        strategy=strategy, source=InMemorySource(_inst(), {D1: bars}), symbol=SYM,
        timeframes=[D1], start=bars[0].timestamp, end=bars[-1].timestamp,
        starting_cash=100_000, cost_model=_costs(),
    )


def _null(strategy, bars, *, n=40, seed=1):
    bt = _backtest(strategy, bars)
    return run_null_test(bt, bt.run(), n=n, seed=seed)


def test_a_random_signal_is_not_extreme_against_its_own_null():
    p_values = [_null(_RandomSignal(), _bars(seed), n=40, seed=seed).p_value
                for seed in range(12)]
    assert sum(p < 0.05 for p in p_values) <= 3, p_values      # ~5% nominal, 12 draws


def test_a_strategy_keyed_to_a_real_conditional_drift_beats_its_null():
    null = _null(_EventSignal(), _bars(5, planted=True), n=60)
    assert null.p_value < 0.05, null.p_value
    assert null.observed_sharpe > max(null.null_sharpes)


def test_the_null_replays_the_observed_trades_not_some_other_number_of_them():
    null = _null(_EventSignal(), _bars(5, planted=True), n=30)
    assert null.n_trades > 30
    assert max(null.null_trade_counts) <= null.n_trades
    assert sorted(null.null_trade_counts)[len(null.null_trade_counts) // 2] >= null.n_trades - 2


def test_a_seeded_null_is_deterministic_and_a_different_seed_differs():
    bars = _bars(5, planted=True)
    a = _null(_EventSignal(), bars, n=20, seed=3)
    b = _null(_EventSignal(), bars, n=20, seed=3)
    c = _null(_EventSignal(), bars, n=20, seed=4)
    assert a.null_sharpes == b.null_sharpes and a.p_value == b.p_value
    assert a.null_sharpes != c.null_sharpes


# --- the `null` axis ----------------------------------------------------------



def _axis(bt, result, **kw):
    (axis,) = analyze(bt, result, axes=("null",), seed=1, **kw).axes
    return axis


def test_null_is_a_rubric_axis_but_not_a_default_one():
    assert "null" in ALL_AXES and "null" not in DEFAULT_AXES


def test_the_null_axis_reports_where_the_observed_sharpe_sits():
    bt = _backtest(_EventSignal(), _bars(5, planted=True))
    axis = _axis(bt, bt.run(), null_n=30)
    assert axis.ran is True
    d = axis.data
    assert d["n"] == 30 and d["n_trades"] > 30
    assert d["p_value"] < 0.05
    assert d["null_sharpe"]["p5"] <= d["null_sharpe"]["p50"] <= d["null_sharpe"]["p95"]
    assert d["observed_sharpe"] > d["null_sharpe"]["p95"]
    assert "p=" in axis.note


def test_the_null_axis_needs_a_backtest_to_replay_and_says_so():
    bt = _backtest(_EventSignal(), _bars(5, planted=True))
    axis = _axis(None, bt.run())
    assert axis.ran is False and "Backtest" in axis.error


def test_a_run_with_too_few_trades_is_not_nulled():
    bars = _bars(5)

    class _Rare(_Holds):
        def signal(self, i):
            return i in (10, 100, 200)

    bt = _backtest(_Rare(), bars)
    axis = _axis(bt, bt.run(), null_n=10)
    assert axis.ran is False and "10 trades" in axis.error


# --- `hermes analyze --axes null` ---------------------------------------------



def test_hermes_analyze_runs_the_null_axis(tmp_path, capsys):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "buyhold.py").write_text(STRATEGY.replace("bar.close * 1.01", "bar.close * 1.0001"))
    common = ["--dir", str(sdir), "--runs-dir", str(tmp_path / "runs")]
    main(["run", "buyhold", *common, "--json", "--note", "t"])
    key = json.loads(capsys.readouterr().out)["run_key"]

    assert main(["analyze", key, *common, "--axes", "null", "--null-n", "12", "--json"]) == EXIT_OK

    (axis,) = json.loads(capsys.readouterr().out)["axes"]
    assert axis["axis"] == "null" and axis["ran"] is True, axis
    assert axis["data"]["n"] == 12
