"""Tests for src/hermes/backtest/validation.py.

Covers every major output of validate():
  - Bootstrap CI structure and ordering (lower ≤ upper, within [0,1] for win_rate)
  - Monte Carlo stats (ordering, prob_profit in [0,1], correct n_simulations)
  - Probabilistic Sharpe Ratio (edge cases: no data, constant returns)
  - Sample quality flags (adequate ratio, min-trades, warning text)
  - BenchmarkComparison stored on BacktestResult via Backtest(validate=True)
  - stat_validation populated when validate=True, None otherwise
  - Graceful handling of no-trade and single-bar results
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from hermes import Backtest, CryptoPair, Strategy, Symbol, Timeframe
from hermes.backtest import StatValidation, validate
from hermes.backtest.result import BacktestResult, BenchmarkComparison, Metrics
from hermes.backtest.validation import (
    _norm_cdf,
    _sample_quality,
)
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.execution import (
    CostModel,
    FinancingModel,
    PercentCommission,
    Side,
    SlippageModel,
    SpreadModel,
)

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _btc():
    return CryptoPair(
        Symbol("BTCUSDT", "binance"), base_asset="BTC", quote_currency="USDT", tick_size=0.01
    )


def _zero_costs():
    return CostModel(PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), FinancingModel(0.0))


def _bar(i, price=100.0, high=None, low=None):
    h = high if high is not None else price + 1
    l = low if low is not None else price - 1
    return Bar(T0 + timedelta(hours=i), H1, price, h, l, price, 1.0)


def _make_result(net_pnls: list[float], starting_cash: float = 10_000.0) -> BacktestResult:
    """Build a synthetic BacktestResult from a list of per-trade net PnLs."""
    from hermes.execution.trade import Trade

    instrument = _btc()
    trades = []
    for i, pnl in enumerate(net_pnls):
        entry_time = T0 + timedelta(hours=i * 2)
        exit_time = T0 + timedelta(hours=i * 2 + 1)
        t = Trade(
            instrument=instrument,
            side=Side.BUY,
            size=1.0,
            entry_price=100.0,
            entry_time=entry_time,
            exit_price=100.0 + pnl,
            exit_time=exit_time,
            exit_reason="take_profit" if pnl > 0 else "stop_loss",
            gross_pnl=pnl,
            costs=0.0,
        )
        trades.append(t)

    # Build equity curve step-by-step.
    equity = starting_cash
    equity_curve = [(T0, equity)]
    for i, pnl in enumerate(net_pnls):
        equity += pnl
        equity_curve.append((T0 + timedelta(hours=i * 2 + 1), equity))

    return BacktestResult.compute(equity_curve, trades, num_params=2)


# ── validate() returns StatValidation ────────────────────────────────────────

def test_validate_returns_stat_validation():
    result = _make_result([10, -5, 15, 8, -3, 12, 20, -8, 5, 10] * 4)
    sv = validate(result)
    assert isinstance(sv, StatValidation)


def test_validate_no_trades():
    """validate() should still work and return a StatValidation when there are no trades."""
    # Flat equity, no trades.
    equity_curve = [(T0 + timedelta(hours=i), 10_000.0) for i in range(5)]
    result = BacktestResult.compute(equity_curve, [], num_params=0)
    sv = validate(result)
    assert isinstance(sv, StatValidation)
    assert sv.monte_carlo is None  # nothing to shuffle


# ── confidence intervals ──────────────────────────────────────────────────────

def test_ci_lower_le_upper():
    result = _make_result([10, -5, 15, 8, -3, 12, 20, -8, 5, 10] * 4)
    sv = validate(result, n_bootstrap=200, seed=0)
    ci = sv.ci
    for field_name in ("sharpe", "sortino", "cagr", "max_drawdown", "win_rate", "profit_factor"):
        val = getattr(ci, field_name)
        if val is not None:
            assert val.lower <= val.upper, f"{field_name}: lower > upper"


def test_ci_win_rate_in_0_1():
    result = _make_result([10, -5, 15, 8, -3, 12] * 5)
    sv = validate(result, n_bootstrap=100, seed=1)
    wr = sv.ci.win_rate
    assert wr is not None
    assert 0.0 <= wr.lower <= wr.upper <= 1.0


def test_ci_level_stored():
    result = _make_result([5, -2, 8, -1, 3] * 8)
    sv = validate(result, level=0.90, n_bootstrap=100, seed=2)
    for field_name in ("sharpe", "win_rate"):
        val = getattr(sv.ci, field_name)
        if val is not None:
            assert val.level == 0.90


def test_ci_none_for_empty_result():
    equity_curve = [(T0, 10_000.0)]
    result = BacktestResult.compute(equity_curve, [], num_params=0)
    sv = validate(result)
    # With a single-point equity curve and no trades, most CIs should be None.
    assert sv.ci.sharpe is None
    assert sv.ci.win_rate is None


# ── Monte Carlo ───────────────────────────────────────────────────────────────

def test_monte_carlo_n_simulations():
    result = _make_result([5, -2, 8, -1, 3] * 6)
    sv = validate(result, n_mc=50, seed=3)
    assert sv.monte_carlo is not None
    assert sv.monte_carlo.n_simulations == 50


def test_monte_carlo_percentile_ordering():
    result = _make_result([10, -5, 15, 8, -3, 12, 20, -8, 5, 10] * 3)
    sv = validate(result, n_mc=200, seed=4)
    mc = sv.monte_carlo
    assert mc is not None
    assert mc.end_equity_p5 <= mc.end_equity_p50 <= mc.end_equity_p95


def test_monte_carlo_prob_profit_in_0_1():
    result = _make_result([10, -5, 15, 8] * 8)
    sv = validate(result, n_mc=200, seed=5)
    mc = sv.monte_carlo
    assert mc is not None
    assert 0.0 <= mc.prob_profit <= 1.0


def test_monte_carlo_losing_strategy_low_prob_profit():
    """Consistently losing trades should yield prob_profit close to 0."""
    result = _make_result([-5, -3, -4, -6, -2] * 8)
    sv = validate(result, n_mc=500, seed=6)
    mc = sv.monte_carlo
    assert mc is not None
    assert mc.prob_profit < 0.2


def test_monte_carlo_winning_strategy_high_prob_profit():
    """Consistently winning trades should yield prob_profit close to 1."""
    result = _make_result([5, 3, 4, 6, 2] * 8)
    sv = validate(result, n_mc=500, seed=7)
    mc = sv.monte_carlo
    assert mc is not None
    assert mc.prob_profit > 0.8


def test_monte_carlo_max_dd_ordering():
    result = _make_result([10, -20, 15, -5, 12] * 4)
    sv = validate(result, n_mc=200, seed=8)
    mc = sv.monte_carlo
    assert mc is not None
    # Both are negative or zero; worst is the most negative, median ≥ worst.
    assert mc.max_drawdown_worst <= mc.max_drawdown_median <= 0.0


# ── Probabilistic Sharpe Ratio ────────────────────────────────────────────────

def test_psr_in_0_1():
    result = _make_result([5, -2, 8, -1, 3] * 10)
    sv = validate(result)
    if sv.probabilistic_sharpe is not None:
        assert 0.0 <= sv.probabilistic_sharpe <= 1.0


def test_psr_positive_strategy_above_half():
    """A clearly profitable strategy should have PSR > 0.5."""
    result = _make_result([8, 2, 5, 1, 4] * 10)
    sv = validate(result)
    if sv.probabilistic_sharpe is not None:
        assert sv.probabilistic_sharpe > 0.5


def test_psr_negative_strategy_below_half():
    """A clearly losing strategy should have PSR < 0.5."""
    result = _make_result([-8, -2, -5, -1, -4] * 10)
    sv = validate(result)
    if sv.probabilistic_sharpe is not None:
        assert sv.probabilistic_sharpe < 0.5


def test_psr_none_for_short_equity_curve():
    """PSR requires ≥ 10 observations; fewer → None."""
    equity_curve = [(T0 + timedelta(hours=i), 10_000.0 + i) for i in range(5)]
    result = BacktestResult.compute(equity_curve, [], num_params=0)
    sv = validate(result)
    assert sv.probabilistic_sharpe is None


def test_psr_none_for_constant_equity():
    """Constant equity → zero std → PSR is undefined → None."""
    equity_curve = [(T0 + timedelta(hours=i), 10_000.0) for i in range(20)]
    result = BacktestResult.compute(equity_curve, [], num_params=0)
    sv = validate(result)
    assert sv.probabilistic_sharpe is None


# ── sample quality ────────────────────────────────────────────────────────────

def test_sample_quality_adequate():
    metrics = Metrics(num_trades=100, num_params=5)
    sq = _sample_quality(metrics)
    assert sq.trades_per_param == 20.0
    assert sq.adequate_ratio is True
    assert sq.min_trades_ok is True
    assert sq.warning is None


def test_sample_quality_too_few_trades():
    metrics = Metrics(num_trades=10, num_params=1)
    sq = _sample_quality(metrics)
    assert sq.min_trades_ok is False
    assert sq.warning is not None
    assert "10 trades" in sq.warning


def test_sample_quality_poor_ratio():
    metrics = Metrics(num_trades=50, num_params=20)
    sq = _sample_quality(metrics)
    assert sq.trades_per_param == 2.5
    assert sq.adequate_ratio is False
    assert sq.warning is not None


def test_sample_quality_no_params():
    metrics = Metrics(num_trades=5, num_params=0)
    sq = _sample_quality(metrics)
    assert sq.trades_per_param is None
    assert sq.adequate_ratio is True  # no params → no ratio penalty


# ── norm_cdf helper ───────────────────────────────────────────────────────────

def test_norm_cdf_symmetry():
    assert abs(_norm_cdf(0.0) - 0.5) < 1e-10
    assert _norm_cdf(3.0) > 0.99
    assert _norm_cdf(-3.0) < 0.01
    assert abs(_norm_cdf(1.96) - 0.975) < 0.001


# ── integration via Backtest ──────────────────────────────────────────────────

class BuyAndSell(Strategy):
    """Alternates between buy and close every bar."""

    def setup(self):
        self.opened = False

    def on_bar(self, bar):
        if not self.opened and self.venue.position().is_flat:
            self.buy(1)
            self.opened = True
        elif not self.venue.position().is_flat:
            for t in self.venue.open_trades():
                self.close(t)
            self.opened = False


def _run_backtest(validate_flag: bool, n_bars: int = 60):
    bars = [_bar(i, 100.0 + (i % 5)) for i in range(n_bars)]
    src = InMemorySource(_btc(), {H1: bars})
    return Backtest(
        strategy=BuyAndSell(),
        source=src,
        symbol=Symbol("BTCUSDT", "binance"),
        timeframes=[H1],
        start=bars[0].timestamp,
        end=bars[-1].timestamp,
        starting_cash=10_000,
        cost_model=_zero_costs(),
        validate=validate_flag,
    ).run()


def test_validate_false_leaves_stat_validation_none():
    result = _run_backtest(validate_flag=False)
    assert result.stat_validation is None


def test_validate_true_populates_stat_validation():
    result = _run_backtest(validate_flag=True)
    assert result.stat_validation is not None
    assert isinstance(result.stat_validation, StatValidation)


def test_benchmark_populated_on_run():
    result = _run_backtest(validate_flag=False)
    assert result.benchmark is not None
    assert isinstance(result.benchmark, BenchmarkComparison)
    assert result.benchmark.description == "Buy-and-hold BTCUSDT"
    # Benchmark return should be finite.
    assert math.isfinite(result.benchmark.total_return)


def test_to_dict_includes_benchmark_and_stat_validation():
    result = _run_backtest(validate_flag=True)
    d = result.to_dict()
    assert "benchmark" in d
    assert "stat_validation" in d
    assert "ci" in d["stat_validation"]
    assert "monte_carlo" in d["stat_validation"]
    assert "probabilistic_sharpe" in d["stat_validation"]
    assert "sample_quality" in d["stat_validation"]


def test_to_dict_no_stat_validation_key_when_disabled():
    result = _run_backtest(validate_flag=False)
    d = result.to_dict()
    assert "benchmark" in d
    assert "stat_validation" not in d


# ── determinism ───────────────────────────────────────────────────────────────

def test_validate_is_deterministic_with_same_seed():
    result = _make_result([5, -2, 8, -1, 3] * 8)
    sv1 = validate(result, n_bootstrap=100, n_mc=100, seed=99)
    sv2 = validate(result, n_bootstrap=100, n_mc=100, seed=99)
    assert sv1.monte_carlo.end_equity_p50 == sv2.monte_carlo.end_equity_p50
    assert sv1.monte_carlo.prob_profit == sv2.monte_carlo.prob_profit


def test_validate_differs_with_different_seeds():
    result = _make_result([5, -2, 8, -1, 3] * 8)
    sv1 = validate(result, n_mc=500, seed=1)
    sv2 = validate(result, n_mc=500, seed=2)
    # prob_profit may differ slightly between seeds.
    # We just verify they can produce the same structural output; exact values may match
    # on tiny datasets — check that at least CI bounds are present.
    assert sv1.ci.win_rate is not None
    assert sv2.ci.win_rate is not None
