"""Statistical validation of a BacktestResult.

Adds to a completed run (all computed deterministically, no AI involvement):

* Bootstrap confidence intervals on every headline metric (Sharpe, Sortino,
  CAGR, max DD, win rate, profit factor) — bar-level resampling for
  curve-based metrics, trade-level for trade-based ones.
* Monte Carlo trade-reorder — permute the sequence of trade returns and
  measure the distribution of end-equity and max drawdown.
* Probabilistic Sharpe Ratio (Lopez de Prado 2012) — the probability that the
  observed Sharpe is greater than zero after correcting for non-normality
  (skewness and excess kurtosis), a frequentist multiple-testing guard.
* Sample-quality flags — trades-to-parameters ratio, minimum-trade warning,
  and a pre-computed trades-per-param ratio that the AI reviewer can cite.

Usage::

    result = Backtest(..., validate=True).run()
    sv = result.stat_validation          # StatValidation | None
    print(sv.probabilistic_sharpe)       # e.g. 0.94 → 94 % confident SR > 0
    print(sv.ci.sharpe)                  # ConfidenceInterval(lower=0.3, upper=1.8, level=0.95)
    print(sv.monte_carlo.prob_profit)    # e.g. 0.82

All arithmetic uses only the standard library (math + random + statistics).
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass

from .result import BacktestResult, _annualisation  # reuse existing helper

# ── data structures ──────────────────────────────────────────────────────────

@dataclass(slots=True)
class ConfidenceInterval:
    lower: float
    upper: float
    level: float  # 0.95 → 95 % CI


@dataclass(slots=True)
class MetricsCI:
    """Bootstrap confidence intervals for each headline metric."""
    sharpe: ConfidenceInterval | None = None
    sortino: ConfidenceInterval | None = None
    cagr: ConfidenceInterval | None = None
    max_drawdown: ConfidenceInterval | None = None
    win_rate: ConfidenceInterval | None = None
    profit_factor: ConfidenceInterval | None = None


@dataclass(slots=True)
class MonteCarloStats:
    """Distribution of equity-curve outcomes when trade order is randomised.

    Derived by permuting the sequence of per-trade fractional returns and
    compounding from the original starting equity.
    """
    n_simulations: int
    end_equity_p5: float    # 5th-percentile final equity
    end_equity_p50: float   # median final equity
    end_equity_p95: float   # 95th-percentile final equity
    max_drawdown_median: float   # median worst intra-path drawdown
    max_drawdown_worst: float    # worst (most negative) across all simulations
    prob_profit: float      # fraction of paths that end above the starting equity


@dataclass(slots=True)
class SampleQuality:
    """Computed statistics that flag overfitting and under-sampling risks."""
    num_trades: int
    num_params: int
    trades_per_param: float | None  # num_trades / num_params (None if no params)
    min_trades_ok: bool             # True when num_trades >= MIN_TRADES
    adequate_ratio: bool            # True when trades_per_param >= MIN_RATIO
    warning: str | None             # human-readable summary of any red flags


@dataclass(slots=True)
class StatValidation:
    """Full statistical validation report for one BacktestResult."""
    ci: MetricsCI
    monte_carlo: MonteCarloStats | None
    probabilistic_sharpe: float | None  # P(SR > 0) corrected for non-normality
    sample_quality: SampleQuality
    deflated_sharpe: float | None = None   # DSR: P(true SR > SR*) correcting for n_trials
    min_trl: int | None = None             # min observations for measured SR to be significant

    def to_dict(self) -> dict:
        return {
            "ci": {
                k: (asdict(v) if v is not None else None)
                for k, v in {
                    "sharpe": self.ci.sharpe,
                    "sortino": self.ci.sortino,
                    "cagr": self.ci.cagr,
                    "max_drawdown": self.ci.max_drawdown,
                    "win_rate": self.ci.win_rate,
                    "profit_factor": self.ci.profit_factor,
                }.items()
            },
            "monte_carlo": asdict(self.monte_carlo) if self.monte_carlo else None,
            "probabilistic_sharpe": self.probabilistic_sharpe,
            "sample_quality": asdict(self.sample_quality),
            "deflated_sharpe": self.deflated_sharpe,
            "min_trl": self.min_trl,
        }


# ── public entry point ────────────────────────────────────────────────────────

MIN_TRADES = 30        # below this, warn about under-sampling
MIN_TRADES_PER_PARAM = 10  # below this, warn about overfitting


def validate(
    result: BacktestResult,
    n_bootstrap: int = 1000,
    n_mc: int = 1000,
    level: float = 0.95,
    seed: int | None = 42,
    n_trials: int = 1,
) -> StatValidation:
    """Compute statistical validation for a completed BacktestResult.

    Parameters
    ----------
    result:      A completed BacktestResult.
    n_bootstrap: Number of bootstrap resamples for confidence intervals.
    n_mc:        Number of Monte Carlo trade-reorder simulations.
    level:       Confidence level (default 0.95 → 95 % CI).
    seed:        RNG seed for reproducibility (None = non-deterministic).
    n_trials:    Number of independent strategy variants tested (for DSR correction).
    """
    rng = random.Random(seed)
    trades = result.trades
    equity_curve = result.equity_curve
    metrics = result.metrics

    ci = _bootstrap_ci(equity_curve, trades, n_bootstrap, level, rng)
    mc = _monte_carlo(trades, equity_curve, n_mc, rng) if trades else None
    psr = _probabilistic_sharpe(equity_curve)
    sq = _sample_quality(metrics)
    dsr = _deflated_sharpe(equity_curve, n_trials)
    min_trl = _min_trl(equity_curve, level)

    return StatValidation(
        ci=ci,
        monte_carlo=mc,
        probabilistic_sharpe=psr,
        sample_quality=sq,
        deflated_sharpe=dsr,
        min_trl=min_trl,
    )


# ── bootstrap confidence intervals ───────────────────────────────────────────

def _bootstrap_ci(equity_curve, trades, n: int, level: float, rng: random.Random) -> MetricsCI:
    alpha = (1.0 - level) / 2.0

    # --- bar-level bootstrap for curve-based metrics (Sharpe, Sortino, max DD, CAGR) ---
    curve_ci = _bar_bootstrap(equity_curve, n, level, alpha, rng)

    # --- trade-level bootstrap for trade-based metrics (win rate, profit factor) ---
    trade_ci = _trade_bootstrap(trades, n, level, alpha, rng)

    return MetricsCI(
        sharpe=curve_ci.get("sharpe"),
        sortino=curve_ci.get("sortino"),
        cagr=curve_ci.get("cagr"),
        max_drawdown=curve_ci.get("max_drawdown"),
        win_rate=trade_ci.get("win_rate"),
        profit_factor=trade_ci.get("profit_factor"),
    )


def _bar_bootstrap(equity_curve, n: int, level: float, alpha: float, rng: random.Random) -> dict:
    if len(equity_curve) < 3:
        return {}

    steps_per_year = _annualisation(equity_curve)
    equities = [e for _, e in equity_curve]
    rets = [equities[i] / equities[i - 1] - 1.0 for i in range(1, len(equities)) if equities[i - 1] > 0]
    if not rets:
        return {}

    start_eq = equities[0]
    k = len(rets)

    sharpes, sortinos, max_dds, total_rets = [], [], [], []

    for _ in range(n):
        sample = [rng.choice(rets) for _ in range(k)]
        sharpes.append(_sharpe_from_rets(sample, steps_per_year))
        sortinos.append(_sortino_from_rets(sample, steps_per_year))

        # Reconstruct equity path for max DD and total return.
        eq = start_eq
        path = [eq]
        for r in sample:
            eq *= 1.0 + r
            path.append(eq)
        max_dds.append(_max_dd(path))
        total_rets.append(path[-1] / start_eq - 1.0 if start_eq > 0 else None)

    def _ci(xs: list) -> ConfidenceInterval | None:
        clean = sorted(x for x in xs if x is not None and math.isfinite(x))
        if not clean:
            return None
        lo_idx = max(0, int(alpha * len(clean)))
        hi_idx = min(len(clean) - 1, int((1.0 - alpha) * len(clean)))
        return ConfidenceInterval(lower=clean[lo_idx], upper=clean[hi_idx], level=level)

    return {
        "sharpe": _ci(sharpes),
        "sortino": _ci(sortinos),
        "max_drawdown": _ci(max_dds),
        "cagr": _ci(total_rets),   # total return as a CI proxy for CAGR (monotone transform)
    }


def _trade_bootstrap(trades, n: int, level: float, alpha: float, rng: random.Random) -> dict:
    if not trades:
        return {}

    pnls = [t.net_pnl for t in trades if t.net_pnl is not None]
    if not pnls:
        return {}

    k = len(pnls)
    win_rates, profit_factors = [], []

    for _ in range(n):
        sample = [rng.choice(pnls) for _ in range(k)]
        wins = [p for p in sample if p > 0]
        losses = [p for p in sample if p < 0]
        win_rates.append(len(wins) / k)
        gross_loss = abs(sum(losses)) if losses else 0.0
        gross_win = sum(wins) if wins else 0.0
        profit_factors.append(gross_win / gross_loss if gross_loss > 0 else math.inf)

    def _ci(xs: list) -> ConfidenceInterval | None:
        clean = sorted(x for x in xs if x is not None and math.isfinite(x))
        if not clean:
            return None
        lo_idx = max(0, int(alpha * len(clean)))
        hi_idx = min(len(clean) - 1, int((1.0 - alpha) * len(clean)))
        return ConfidenceInterval(lower=clean[lo_idx], upper=clean[hi_idx], level=level)

    return {
        "win_rate": _ci(win_rates),
        "profit_factor": _ci(profit_factors),
    }


# ── Monte Carlo trade-reorder ─────────────────────────────────────────────────

def _monte_carlo(trades, equity_curve, n: int, rng: random.Random) -> MonteCarloStats | None:
    """Permute trade-return sequence N times and report the equity distribution.

    Each trade's fractional return = net_pnl / equity_at_entry.  This is
    looked up from the equity curve (same as the batch combined_equity_curve
    approach). Permuted returns are then compounded from the starting equity.
    """
    if not equity_curve:
        return None

    start_eq = equity_curve[0][1]
    if start_eq <= 0:
        return None

    # Build (entry_time, fractional_return) for each trade.
    frac_returns: list[float] = []
    for t in trades:
        if t.entry_time is None or t.net_pnl is None:
            continue
        eq_at_entry = _equity_at(equity_curve, t.entry_time)
        if eq_at_entry > 0:
            frac_returns.append(t.net_pnl / eq_at_entry)

    if not frac_returns:
        return None

    end_equities: list[float] = []
    max_dds: list[float] = []

    for _ in range(n):
        perm = list(frac_returns)
        rng.shuffle(perm)
        eq = start_eq
        path = [eq]
        for r in perm:
            eq *= 1.0 + r
            path.append(eq)
        end_equities.append(path[-1])
        max_dds.append(_max_dd(path))

    end_equities.sort()
    max_dds_sorted = sorted(max_dds)
    m = len(end_equities)
    prob_profit = sum(1 for e in end_equities if e > start_eq) / m

    return MonteCarloStats(
        n_simulations=n,
        end_equity_p5=end_equities[max(0, int(0.05 * m))],
        end_equity_p50=end_equities[m // 2],
        end_equity_p95=end_equities[min(m - 1, int(0.95 * m))],
        max_drawdown_median=max_dds_sorted[m // 2],
        max_drawdown_worst=max_dds_sorted[0],   # most negative
        prob_profit=prob_profit,
    )


# ── Probabilistic Sharpe Ratio ────────────────────────────────────────────────

def _probabilistic_sharpe(equity_curve) -> float | None:
    """P(SR > 0) corrected for non-normality of returns (Lopez de Prado 2012).

    PSR(0) = Φ[ (ŜR × √(n−1)) / √(1 − γ₃·ŜR + (γ₄−1)/4·ŜR²) ]

    where ŜR is the per-step Sharpe (not annualised), γ₃ is skewness of
    per-bar returns, γ₄ is excess kurtosis, and n is the number of return
    observations.  A value near 1 means the Sharpe is unlikely to be a
    sampling artefact; near 0.5 means indistinguishable from noise.
    """
    if len(equity_curve) < 10:
        return None

    equities = [e for _, e in equity_curve]
    rets = [equities[i] / equities[i - 1] - 1.0 for i in range(1, len(equities)) if equities[i - 1] > 0]
    if len(rets) < 10:
        return None

    n = len(rets)
    mean_r = sum(rets) / n
    var_r = sum((r - mean_r) ** 2 for r in rets) / n
    std_r = math.sqrt(var_r)
    if std_r <= 0:
        return None

    sr_hat = mean_r / std_r   # per-step Sharpe (un-annualised)

    # Skewness (γ₃) and excess kurtosis (γ₄) of the return series.
    skew = _skewness(rets, mean_r, std_r, n)
    kurt = _excess_kurtosis(rets, mean_r, std_r, n)

    # Variance of the SR estimator (denominator under the square root).
    sr2 = sr_hat ** 2
    denom_sq = 1.0 - skew * sr_hat + (kurt / 4.0) * sr2
    if denom_sq <= 0:
        return None

    z = sr_hat * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return _norm_cdf(z)


# ── sample quality flags ──────────────────────────────────────────────────────

def _sample_quality(metrics) -> SampleQuality:
    n = metrics.num_trades
    k = metrics.num_params

    trades_per_param: float | None = None
    adequate_ratio = True
    if k > 0:
        trades_per_param = n / k
        adequate_ratio = trades_per_param >= MIN_TRADES_PER_PARAM

    min_trades_ok = n >= MIN_TRADES

    warnings: list[str] = []
    if not min_trades_ok:
        warnings.append(f"Only {n} trades — need ≥ {MIN_TRADES} for reliable statistics.")
    if not adequate_ratio:
        warnings.append(
            f"trades-to-parameters ratio is {trades_per_param:.1f} "
            f"(< {MIN_TRADES_PER_PARAM}) — high overfitting risk."
        )

    return SampleQuality(
        num_trades=n,
        num_params=k,
        trades_per_param=trades_per_param,
        min_trades_ok=min_trades_ok,
        adequate_ratio=adequate_ratio,
        warning="\n".join(warnings) if warnings else None,
    )


# ── private helpers ───────────────────────────────────────────────────────────

def _sharpe_from_rets(rets: list[float], steps_per_year: float) -> float | None:
    if not rets:
        return None
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    std = math.sqrt(var)
    if std <= 0:
        return None
    return mean / std * math.sqrt(steps_per_year)


def _sortino_from_rets(rets: list[float], steps_per_year: float) -> float | None:
    if not rets:
        return None
    mean = sum(rets) / len(rets)
    downside = [r for r in rets if r < 0]
    if not downside:
        return None
    dvar = sum(r * r for r in downside) / len(downside)
    dstd = math.sqrt(dvar)
    if dstd <= 0:
        return None
    return mean / dstd * math.sqrt(steps_per_year)


def _max_dd(equities: list[float]) -> float:
    peak = equities[0]
    dd = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            dd = min(dd, e / peak - 1.0)
    return dd


def _equity_at(curve: list[tuple], ts) -> float:
    """Equity at-or-before ts (mirrors batch._equity_at)."""
    val = curve[0][1]
    for t, e in curve:
        if t <= ts:
            val = e
        else:
            break
    return val


def _skewness(rets: list[float], mean: float, std: float, n: int) -> float:
    if std <= 0 or n < 3:
        return 0.0
    return sum((r - mean) ** 3 for r in rets) / (n * std ** 3)


def _excess_kurtosis(rets: list[float], mean: float, std: float, n: int) -> float:
    if std <= 0 or n < 4:
        return 0.0
    return sum((r - mean) ** 4 for r in rets) / (n * std ** 4) - 3.0


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf (no scipy needed)."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _norm_cdf_inv(p: float, tol: float = 1e-9) -> float:
    """Inverse normal CDF via bisection on _norm_cdf (no scipy needed)."""
    p = max(tol, min(1.0 - tol, p))
    lo, hi = -20.0, 20.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


_EULER_MASCHERONI = 0.5772156649015328


def _expected_max_sharpe(n_trials: int) -> float:
    """SR* = expected maximum Sharpe from n_trials independent trials (Bailey & López de Prado 2014)."""
    if n_trials <= 1:
        return 0.0
    gamma = _EULER_MASCHERONI
    n = float(n_trials)
    return (1 - gamma) * _norm_cdf_inv(1 - 1 / n) + gamma * _norm_cdf_inv(1 - 1 / (n * math.e))


def _deflated_sharpe(equity_curve, n_trials: int = 1) -> float | None:
    """Deflated Sharpe Ratio: P(true SR > SR*) correcting for multiple trials."""
    if len(equity_curve) < 10:
        return None

    equities = [e for _, e in equity_curve]
    rets = [equities[i] / equities[i - 1] - 1.0 for i in range(1, len(equities)) if equities[i - 1] > 0]
    if len(rets) < 10:
        return None

    n = len(rets)
    mean_r = sum(rets) / n
    var_r = sum((r - mean_r) ** 2 for r in rets) / n
    std_r = math.sqrt(var_r)
    if std_r <= 0:
        return None

    sr_hat = mean_r / std_r  # per-step Sharpe (un-annualised)

    skew = _skewness(rets, mean_r, std_r, n)
    kurt = _excess_kurtosis(rets, mean_r, std_r, n)

    sr_star = _expected_max_sharpe(n_trials)

    denom_sq = 1.0 - skew * sr_hat + (kurt / 4.0) * sr_hat ** 2
    if denom_sq <= 0:
        return None

    z = (sr_hat - sr_star) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return _norm_cdf(z)


def _min_trl(equity_curve, confidence: float = 0.95) -> int | None:
    """Minimum Track Record Length: bars needed for measured SR to be significant."""
    if len(equity_curve) < 10:
        return None

    equities = [e for _, e in equity_curve]
    rets = [equities[i] / equities[i - 1] - 1.0 for i in range(1, len(equities)) if equities[i - 1] > 0]
    if len(rets) < 10:
        return None

    n = len(rets)
    mean_r = sum(rets) / n
    var_r = sum((r - mean_r) ** 2 for r in rets) / n
    std_r = math.sqrt(var_r)
    if std_r <= 0:
        return None

    sr = mean_r / std_r  # per-step SR
    if sr == 0:
        return None

    skew = _skewness(rets, mean_r, std_r, n)
    kurt = _excess_kurtosis(rets, mean_r, std_r, n)

    z_conf = _norm_cdf_inv(confidence)
    # MinTRL = (1 + (1 - skew*SR + (kurt/4)*SR²)) * (z_conf / SR)²
    adj = 1.0 - skew * sr + (kurt / 4.0) * sr ** 2
    min_trl = (1 + adj) * (z_conf / sr) ** 2
    return math.ceil(max(1.0, min_trl))
