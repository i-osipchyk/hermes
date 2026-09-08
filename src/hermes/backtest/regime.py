"""Regime analysis: classify trades by market regime and compute per-regime metrics."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .validation import _sharpe_from_rets, _sortino_from_rets


@dataclass(slots=True)
class RegimeStats:
    label: str
    n_trades: int
    sharpe: float | None
    win_rate: float | None
    profit_factor: float | None
    total_pnl: float


@dataclass(slots=True)
class RegimeAnalysis:
    regimes: list[RegimeStats]


def regime_analysis(
    result,
    trend_lookback: int = 60,
    vol_lookback: int = 20,
    external_benchmark: list[tuple] | None = None,
) -> RegimeAnalysis:
    """Classify trades by market regime and compute per-regime metrics.

    Parameters
    ----------
    result:             A BacktestResult with benchmark_equity populated.
    trend_lookback:     Bars for rolling MA trend detection.
    vol_lookback:       Bars for rolling std volatility detection.
    external_benchmark: Optional external price series [(datetime, price), ...].
                        Falls back to result.benchmark_equity if None.
    """
    benchmark = external_benchmark or result.benchmark_equity
    if not benchmark:
        return RegimeAnalysis(regimes=[])

    trades = result.trades
    if not trades:
        return RegimeAnalysis(regimes=[])

    # Build benchmark price series as a sorted list
    bench_sorted = sorted(benchmark, key=lambda x: x[0])
    bench_times = [t for t, _ in bench_sorted]
    bench_prices = [p for _, p in bench_sorted]

    # Compute rolling mean and rolling std for each bar
    trend_ma = _rolling_mean(bench_prices, trend_lookback)
    vol_std = _rolling_std(bench_prices, vol_lookback)

    # Compute median vol for high/low split
    valid_vols = [v for v in vol_std if v is not None]
    if not valid_vols:
        return RegimeAnalysis(regimes=[])
    median_vol = sorted(valid_vols)[len(valid_vols) // 2]

    # Build lookup: timestamp -> (trend_label, vol_label)
    regime_map = {}
    for i, t in enumerate(bench_times):
        ma = trend_ma[i]
        vol = vol_std[i]
        if ma is None or vol is None:
            continue
        trend_label = "Bull" if bench_prices[i] > ma else "Bear"
        vol_label = "High Vol" if vol > median_vol else "Low Vol"
        regime_map[t] = f"{trend_label}/{vol_label}"

    # For each trade, find closest benchmark time at entry
    bucket_trades: dict[str, list] = {
        "Bull/High Vol": [],
        "Bull/Low Vol": [],
        "Bear/High Vol": [],
        "Bear/Low Vol": [],
    }

    for trade in trades:
        if trade.entry_time is None:
            continue
        label = _closest_regime(regime_map, bench_times, trade.entry_time)
        if label and label in bucket_trades:
            bucket_trades[label].append(trade)

    regimes = []
    for label in ["Bull/High Vol", "Bull/Low Vol", "Bear/High Vol", "Bear/Low Vol"]:
        bucket = bucket_trades[label]
        stats = _regime_stats(label, bucket)
        regimes.append(stats)

    return RegimeAnalysis(regimes=regimes)


def _regime_stats(label: str, trades: list) -> RegimeStats:
    if not trades:
        return RegimeStats(
            label=label, n_trades=0, sharpe=None,
            win_rate=None, profit_factor=None, total_pnl=0.0,
        )

    pnls = [t.net_pnl for t in trades if t.net_pnl is not None]
    total_pnl = sum(pnls)

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls) if pnls else None
    gross_loss = abs(sum(losses)) if losses else 0.0
    gross_win = sum(wins) if wins else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else None)

    # Sharpe from fractional PnL returns (approximate — use pnl / |avg pnl| as proxy)
    if pnls:
        sharpe = _sharpe_from_rets(pnls, 252.0)
    else:
        sharpe = None

    return RegimeStats(
        label=label,
        n_trades=len(trades),
        sharpe=sharpe,
        win_rate=win_rate,
        profit_factor=profit_factor,
        total_pnl=total_pnl,
    )


def _rolling_mean(values: list[float], window: int) -> list[float | None]:
    result = []
    for i in range(len(values)):
        if i < window - 1:
            result.append(None)
        else:
            window_vals = values[i - window + 1:i + 1]
            result.append(sum(window_vals) / window)
    return result


def _rolling_std(values: list[float], window: int) -> list[float | None]:
    result = []
    for i in range(len(values)):
        if i < window - 1:
            result.append(None)
        else:
            window_vals = values[i - window + 1:i + 1]
            mean = sum(window_vals) / window
            var = sum((v - mean) ** 2 for v in window_vals) / window
            result.append(math.sqrt(var))
    return result


def _closest_regime(regime_map: dict, bench_times: list, target_time) -> str | None:
    """Find the regime label for the benchmark time closest to (and not after) target_time."""
    best = None
    for t in bench_times:
        if t <= target_time:
            best = t
        else:
            break
    return regime_map.get(best) if best is not None else None
