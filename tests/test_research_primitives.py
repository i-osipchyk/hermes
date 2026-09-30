"""Offline smoke coverage for the "running many" primitives.

``WalkForward``, ``split_isoos``, ``cost_sensitivity``, ``regime_analysis``,
``run_batch`` and ``run_random_simulations`` had no tests and — before ADR-0010 —
were not reachable from the top-level package at all. These runs are deliberately
tiny (synthetic bars, ``InMemorySource``); the point is that each composes over the
engine and returns the shape the analyse-results rubric expects, not that any
number is meaningful.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import hermes
from hermes import (
    SMA,
    Backtest,
    CostModel,
    CryptoPair,
    FinancingModel,
    InMemorySource,
    Parameter,
    PercentCommission,
    SlippageModel,
    SpreadModel,
    Strategy,
    Symbol,
    Timeframe,
    Units,
)
from hermes.core import Bar

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)
SYMBOL = Symbol("BTCUSDT", "binance")


def _btc():
    return CryptoPair(SYMBOL, base_asset="BTC", quote_currency="USDT", tick_size=0.01)


def _bars(n: int = 400) -> list[Bar]:
    """A gently oscillating, mildly upward series — enough for SMA crossings."""
    out = []
    price = 100.0
    for i in range(n):
        price += 0.05 + (1.5 if i % 20 < 10 else -1.4)
        o = price
        c = price + (0.4 if i % 3 else -0.3)
        out.append(Bar(T0 + timedelta(hours=i), H1, o, max(o, c) + 0.5, min(o, c) - 0.5, c, 10.0))
    return out


BARS = _bars()


def _costs(mult: float = 1.0) -> CostModel:
    return CostModel(
        PercentCommission(0.001 * mult),
        SpreadModel(0.0005 * mult),
        SlippageModel(0.0, 0.0002 * mult),
        FinancingModel(0.0),
    )


class SmaCross(Strategy):
    """Two declared Parameters, so WalkForward has a grid to discover."""

    def setup(self) -> None:
        fast = self.param(Parameter("fast", 5, bounds=(3, 9)))
        slow = self.param(Parameter("slow", 20, bounds=(15, 30)))
        self.fast = self.use(SMA(H1, fast))
        self.slow = self.use(SMA(H1, slow))

    def on_bar(self, bar) -> None:
        f = self.indicator_value(self.fast)["value"]
        s = self.indicator_value(self.slow)["value"]
        if f is None or s is None:
            return
        flat = self.venue.position().is_flat
        if f > s and flat:
            self.buy(Units(1), stop_loss=bar.close * 0.95, take_profit=bar.close * 1.05)
        elif f < s and not flat:
            for t in self.venue.open_trades():
                self.close(t)


def _backtest(**over) -> Backtest:
    kw = dict(
        strategy=SmaCross(),
        source=InMemorySource(_btc(), {H1: BARS}),
        symbol=SYMBOL,
        timeframes=[H1],
        start=BARS[0].timestamp,
        end=BARS[-1].timestamp,
        starting_cash=10_000.0,
        cost_model=_costs(),
    )
    kw.update(over)
    return Backtest(**kw)


def test_the_baseline_run_actually_trades():
    """Guards the fixture: every primitive below is meaningless on a zero-trade run."""
    result = _backtest().run()
    assert result.metrics.num_trades > 0
    assert result.metrics.num_params == 2


# --- cost sensitivity ------------------------------------------------------

def test_cost_sensitivity_returns_a_result_per_multiplier():
    results = hermes.cost_sensitivity(_backtest(), multipliers=(0.0, 1.0, 2.0))
    assert sorted(results) == [0.0, 1.0, 2.0]
    for r in results.values():
        assert r.metrics.num_trades > 0
    # Zeroed costs cannot be worse than doubled costs on identical fills.
    assert results[0.0].metrics.total_return >= results[2.0].metrics.total_return


def test_cost_sensitivity_defaults_the_cost_model_when_none_is_set():
    """The backtest need not carry an explicit CostModel — the asset-class default
    is scaled instead."""
    results = hermes.cost_sensitivity(_backtest(cost_model=None), multipliers=(0.0, 1.0))
    assert set(results) == {0.0, 1.0}


# --- in-sample / out-of-sample --------------------------------------------

def test_split_isoos_partitions_the_window():
    is_result, oos_result = hermes.split_isoos(_backtest(), is_frac=0.7)
    assert is_result.equity_curve and oos_result.equity_curve
    # IS ends no later than OOS begins.
    assert is_result.equity_curve[-1][0] <= oos_result.equity_curve[0][0]


def test_walk_forward_optimises_in_sample_and_scores_out_of_sample():
    wf = hermes.WalkForward(
        template=_backtest(),
        total_start=BARS[0].timestamp,
        total_end=BARS[-1].timestamp,
        is_frac=0.6,
        param_grid={"fast": [4, 6], "slow": [18, 24]},
    )
    res = wf.run()
    assert isinstance(res, hermes.WalkForwardResult)
    assert res.windows, "expected at least one IS/OOS window"
    for w in res.windows:
        assert w.is_end <= w.oos_start
        # the chosen params must come from the grid we supplied
        assert w.best_params["fast"] in (4, 6)
        assert w.best_params["slow"] in (18, 24)
    assert res.oos_result.equity_curve, "OOS blocks should stitch into a curve"


def test_walk_forward_with_an_empty_grid_runs_fixed_params():
    wf = hermes.WalkForward(
        template=_backtest(),
        total_start=BARS[0].timestamp,
        total_end=BARS[-1].timestamp,
        param_grid={},
    )
    res = wf.run()
    assert all(w.best_params == {} for w in res.windows)


# --- batch ------------------------------------------------------------------

def test_run_batch_collects_per_symbol_errors_without_aborting():
    def build(ticker: str) -> Backtest:
        if ticker == "BROKEN":
            raise ValueError("no data for BROKEN")
        return _backtest()

    batch = hermes.run_batch(["BTCUSDT", "BROKEN", "ETHUSDT"], build)
    assert sorted(batch.results) == ["BTCUSDT", "ETHUSDT"]
    assert "no data for BROKEN" in batch.errors["BROKEN"]


# --- regime -----------------------------------------------------------------

def test_regime_analysis_buckets_trades_by_trend_and_volatility():
    """A run carries its own ``benchmark_equity``, so regimes are labelled without
    passing a benchmark explicitly. Buckets may legitimately be empty."""
    result = _backtest().run()
    analysis = hermes.regime_analysis(result)
    assert isinstance(analysis, hermes.RegimeAnalysis)
    labels = [r.label for r in analysis.regimes]
    assert labels == ["Bull/High Vol", "Bull/Low Vol", "Bear/High Vol", "Bear/Low Vol"]
    assert all(isinstance(r, hermes.RegimeStats) for r in analysis.regimes)

    # Every attributed trade lands in exactly one bucket, and the buckets cannot
    # claim more trades than the run produced. Trades inside the rolling-lookback
    # warmup have no defined regime, so the total is allowed to be lower.
    attributed = sum(r.n_trades for r in analysis.regimes)
    assert 0 < attributed <= result.metrics.num_trades


def test_regime_analysis_accepts_an_external_benchmark():
    result = _backtest().run()
    bench = [(b.timestamp, b.close) for b in BARS]
    analysis = hermes.regime_analysis(result, external_benchmark=bench)
    assert sum(r.n_trades for r in analysis.regimes) > 0


def test_regime_analysis_degrades_when_there_is_no_benchmark_at_all():
    """Called on a bare result (no benchmark series): return no regimes, don't raise."""
    from hermes.backtest.result import BacktestResult

    bare = BacktestResult.compute([(b.timestamp, 10_000.0) for b in BARS[:5]], [])
    assert hermes.regime_analysis(bare).regimes == []


# --- statistical validation -------------------------------------------------

def test_validate_is_deterministic_for_a_fixed_seed():
    result = _backtest().run()
    a = hermes.validate(result, seed=7)
    b = hermes.validate(result, seed=7)
    assert isinstance(a, hermes.StatValidation)
    assert a.to_dict() == b.to_dict()


# --- random baseline --------------------------------------------------------

def test_run_random_simulations_returns_a_distribution():
    """The AI-gate control: N runs whose gate approves with probability p."""
    sims = hermes.run_random_simulations(_backtest, n=4, p=0.5, seed=1, workers=1)
    assert len(sims) == 4
    assert all("total_return" in s for s in sims)


def test_random_simulations_are_reproducible_for_a_fixed_seed():
    a = hermes.run_random_simulations(_backtest, n=3, p=0.5, seed=99, workers=1)
    b = hermes.run_random_simulations(_backtest, n=3, p=0.5, seed=99, workers=1)
    assert [s["total_return"] for s in a] == pytest.approx([s["total_return"] for s in b])
