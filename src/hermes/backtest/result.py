"""BacktestResult: the viz-independent output of a run (CONTEXT.md).

Holds the equity curve, the Trade blotter (every closed Trade with entry/exit/
P&L/costs and any Advisor Decision), and computed metrics. Plotting and third-party
tear-sheets (quantstats) consume it; the object itself imports no plotting lib.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from ..execution import Trade
from ..execution.order import Order

if TYPE_CHECKING:
    from .validation import StatValidation


@dataclass(slots=True)
class Metrics:
    total_return: float | None = None
    cagr: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    pnl_ratio: float | None = None
    num_trades: int = 0
    num_params: int = 0
    parameter_adjusted_sharpe: float | None = None

    # Ratios
    calmar: float | None = None
    omega_ratio: float | None = None

    # Drawdown detail
    avg_drawdown: float | None = None
    avg_drawdown_duration: float | None = None
    avg_recovery_bars: float | None = None

    # Distributional
    var_95: float | None = None
    cvar_95: float | None = None

    # Exposure / turnover
    exposure_pct: float | None = None
    turnover: float | None = None


@dataclass(slots=True)
class BenchmarkStats:
    """Buy-and-hold baseline with regression statistics."""
    total_return: float
    cagr: float | None
    description: str
    # Regression stats (None when < 10 aligned bars)
    alpha: float | None = None
    beta: float | None = None
    information_ratio: float | None = None
    tracking_error: float | None = None


# Backward-compatibility alias
BenchmarkComparison = BenchmarkStats


@dataclass(slots=True)
class BacktestResult:
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    vetoed_signals: list[Order] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    benchmark: BenchmarkStats | None = None
    stat_validation: StatValidation | None = None
    benchmark_equity: list[tuple[datetime, float]] | None = None

    @classmethod
    def compute(
        cls,
        equity_curve,
        trades,
        num_params: int = 0,
        vetoed_signals: list | None = None,
    ) -> BacktestResult:
        return cls(
            equity_curve=list(equity_curve),
            trades=list(trades),
            vetoed_signals=list(vetoed_signals or []),
            metrics=_metrics(equity_curve, trades, num_params),
        )

    def to_frame(self):
        """Equity curve as a pandas Series indexed by timestamp (for quantstats)."""
        import pandas as pd

        if not self.equity_curve:
            return pd.Series(dtype=float)
        idx, vals = zip(*self.equity_curve)
        return pd.Series(vals, index=pd.DatetimeIndex(idx), name="equity")

    def to_dict(self) -> dict:
        """JSON-serialisable view of the result (equity curve, trades, metrics).

        Reused by the web UI to hand a run to a Claude Code review and to render
        without touching live objects.
        """
        d: dict = {
            "metrics": asdict(self.metrics),
            "equity_curve": [(ts.isoformat(), eq) for ts, eq in self.equity_curve],
            "trades": [_trade_dict(t) for t in self.trades],
            "vetoed_signals": [_vetoed_dict(o) for o in self.vetoed_signals],
        }
        if self.benchmark is not None:
            d["benchmark"] = asdict(self.benchmark)
        if self.stat_validation is not None:
            d["stat_validation"] = self.stat_validation.to_dict()
        return d


def _trade_dict(t: Trade) -> dict:
    d = {
        "symbol": str(t.instrument.symbol),
        "side": t.side.value,
        "size": t.size,
        "entry_price": t.entry_price,
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "exit_price": t.exit_price,
        "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        "exit_reason": t.exit_reason,
        "stop_loss": t.stop_loss,
        "take_profit": t.take_profit,
        "gross_pnl": t.gross_pnl,
        "costs": t.costs,
        "net_pnl": t.net_pnl,
    }
    if t.ai_decision is not None:
        ai = t.ai_decision
        d["ai_decision"] = {
            "approved": ai.approved,
            "confidence": ai.confidence,
            "reason": ai.reason,
            "model_id": ai.model_id,
            "prompt": ai.prompt,
            "from_cache": ai.from_cache,
        }
    return d


def _vetoed_dict(o: Order) -> dict:
    ai = o.ai_decision
    return {
        "symbol": str(o.instrument.symbol),
        "side": o.side.value,
        "size": o.size,
        "entry_ref": o.fill_price or o.limit_price or o.stop_price,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "ai_decision": {
            "approved": ai.approved,
            "confidence": ai.confidence,
            "reason": ai.reason,
            "model_id": ai.model_id,
            "prompt": ai.prompt,
            "from_cache": ai.from_cache,
        },
    }


def _metrics(equity_curve, trades, num_params: int = 0) -> Metrics:
    m = Metrics(num_trades=len(trades), num_params=num_params)
    if len(equity_curve) >= 2:
        equities = [e for _, e in equity_curve]
        start_eq, end_eq = equities[0], equities[-1]
        if start_eq > 0:
            m.total_return = end_eq / start_eq - 1.0

        # Per-step simple returns.
        rets = [
            (equities[i] / equities[i - 1] - 1.0)
            for i in range(1, len(equities))
            if equities[i - 1] > 0
        ]
        if rets:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / len(rets)
            std = math.sqrt(var)
            steps_per_year = _annualisation(equity_curve)
            if std > 0:
                m.sharpe = mean / std * math.sqrt(steps_per_year)
                # Penalise for free parameters: Sharpe × √((n−k)/n).
                n = len(trades)
                if n > 0 and num_params > 0:
                    penalty = max(0.0, (n - num_params) / n)
                    m.parameter_adjusted_sharpe = m.sharpe * math.sqrt(penalty)
                else:
                    m.parameter_adjusted_sharpe = m.sharpe
            downside = [r for r in rets if r < 0]
            if downside:
                dvar = sum(r * r for r in downside) / len(downside)
                dstd = math.sqrt(dvar)
                if dstd > 0:
                    m.sortino = mean / dstd * math.sqrt(steps_per_year)

            # Omega ratio: sum(positive returns) / sum(|negative returns|)
            pos_sum = sum(r for r in rets if r > 0)
            neg_sum = abs(sum(r for r in rets if r < 0))
            if neg_sum > 0:
                m.omega_ratio = pos_sum / neg_sum

            # VaR and CVaR at 95%
            sorted_rets = sorted(rets)
            var_idx = max(0, int(0.05 * len(sorted_rets)) - 1)
            m.var_95 = sorted_rets[var_idx]
            tail = sorted_rets[:var_idx + 1]
            if tail:
                m.cvar_95 = sum(tail) / len(tail)

        # Time-based CAGR.
        span_days = (equity_curve[-1][0] - equity_curve[0][0]).total_seconds() / 86_400
        if span_days >= 1 and start_eq > 0 and end_eq > 0:
            years = span_days / 365.25
            try:
                m.cagr = (end_eq / start_eq) ** (1 / years) - 1.0
            except OverflowError:
                m.cagr = None

        m.max_drawdown = _max_drawdown(equities)

        # Calmar: CAGR / |max_drawdown|
        if m.cagr is not None and m.max_drawdown is not None and m.max_drawdown != 0:
            m.calmar = m.cagr / abs(m.max_drawdown)

        # Drawdown periods analysis
        periods = _drawdown_periods(equities)
        if periods:
            m.avg_drawdown = sum(p[0] for p in periods) / len(periods)
            m.avg_drawdown_duration = sum(p[1] for p in periods) / len(periods)
            recoveries = [p[2] for p in periods if p[2] is not None]
            if recoveries:
                m.avg_recovery_bars = sum(recoveries) / len(recoveries)

    if trades:
        wins = [t for t in trades if (t.net_pnl or 0) > 0]
        losses = [t for t in trades if (t.net_pnl or 0) < 0]
        m.win_rate = len(wins) / len(trades)
        gross_win = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        m.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else math.inf

        # PnL ratio: avg winning trade / avg losing trade (payoff ratio)
        if wins and losses:
            avg_win = gross_win / len(wins)
            avg_loss = gross_loss / len(losses)
            if avg_loss > 0:
                m.pnl_ratio = avg_win / avg_loss

        # Exposure: fraction of equity curve bars with an open position
        if equity_curve:
            m.exposure_pct = _compute_exposure(equity_curve, trades)

        # Turnover: total traded notional / avg equity, annualized
        if equity_curve:
            equities = [e for _, e in equity_curve]
            avg_equity = sum(equities) / len(equities)
            if avg_equity > 0:
                total_notional = sum(
                    abs(t.size * t.entry_price)
                    for t in trades
                    if t.size is not None and t.entry_price is not None
                )
                steps_per_year = _annualisation(equity_curve)
                n_bars = len(equity_curve)
                m.turnover = (total_notional / avg_equity) * (steps_per_year / n_bars)

    return m


def _max_drawdown(equities: list[float]) -> float:
    peak = equities[0]
    max_dd = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            max_dd = min(max_dd, e / peak - 1.0)
    return max_dd


def _drawdown_periods(equities: list[float]) -> list[tuple[float, int, int | None]]:
    """Return list of (depth, duration_bars, recovery_bars | None) for each drawdown episode."""
    if not equities:
        return []

    periods = []
    peak = equities[0]
    peak_idx = 0
    in_drawdown = False
    trough_depth = 0.0
    trough_idx = 0

    for i, e in enumerate(equities):
        if e > peak:
            if in_drawdown:
                # recovered
                recovery_bars = i - trough_idx
                periods.append((trough_depth, trough_idx - peak_idx, recovery_bars))
                in_drawdown = False
            peak = e
            peak_idx = i
        elif peak > 0:
            dd = e / peak - 1.0
            if dd < 0:
                if not in_drawdown:
                    in_drawdown = True
                    trough_depth = dd
                    trough_idx = i
                elif dd < trough_depth:
                    trough_depth = dd
                    trough_idx = i

    # Still in drawdown at end
    if in_drawdown:
        periods.append((trough_depth, trough_idx - peak_idx, None))

    return periods


def _compute_exposure(equity_curve, trades) -> float:
    """Fraction of equity curve bars with an open position."""
    if not equity_curve or not trades:
        return 0.0

    curve_times = [t for t, _ in equity_curve]
    open_bars = 0

    for bar_time in curve_times:
        for trade in trades:
            entry = trade.entry_time
            exit_t = trade.exit_time
            if entry is None:
                continue
            if entry <= bar_time and (exit_t is None or exit_t >= bar_time):
                open_bars += 1
                break

    return open_bars / len(curve_times)


def _compute_benchmark_stats(
    equity_curve: list[tuple[datetime, float]],
    benchmark_equity: list[tuple[datetime, float]],
    description: str,
) -> BenchmarkStats:
    """Compute benchmark comparison with regression statistics."""
    if not benchmark_equity:
        return BenchmarkStats(total_return=0.0, cagr=None, description=description)

    bh_equities = [e for _, e in benchmark_equity]
    bh_start, bh_end = bh_equities[0], bh_equities[-1]
    bh_return = bh_end / bh_start - 1.0 if bh_start > 0 else 0.0

    span_days = (benchmark_equity[-1][0] - benchmark_equity[0][0]).total_seconds() / 86_400
    bh_cagr: float | None = None
    if span_days >= 1 and bh_start > 0 and bh_end > 0:
        years = span_days / 365.25
        try:
            bh_cagr = (bh_end / bh_start) ** (1 / years) - 1.0
        except OverflowError:
            pass

    stats = BenchmarkStats(total_return=bh_return, cagr=bh_cagr, description=description)

    # Align returns — both curves must have same timestamps
    # Build dicts for lookup
    bh_dict = {t: e for t, e in benchmark_equity}
    strat_dict = {t: e for t, e in equity_curve}

    common_times = sorted(set(bh_dict) & set(strat_dict))
    if len(common_times) < 11:  # need at least 10 return pairs
        return stats

    # Compute aligned returns
    strat_rets = []
    bh_rets = []
    prev_strat = None
    prev_bh = None
    for t in common_times:
        se = strat_dict[t]
        be = bh_dict[t]
        if prev_strat is not None and prev_strat > 0 and prev_bh > 0:
            strat_rets.append(se / prev_strat - 1.0)
            bh_rets.append(be / prev_bh - 1.0)
        prev_strat = se
        prev_bh = be

    if len(strat_rets) < 10:
        return stats

    steps_per_year = _annualisation(equity_curve)
    n = len(strat_rets)

    # OLS: beta = Σ(x_i * y_i) / Σ(x_i²) where x = bh_rets, y = strat_rets
    mean_bh = sum(bh_rets) / n
    mean_strat = sum(strat_rets) / n
    xx = sum((x - mean_bh) ** 2 for x in bh_rets)
    xy = sum((bh_rets[i] - mean_bh) * (strat_rets[i] - mean_strat) for i in range(n))

    if xx > 0:
        beta = xy / xx
        # Alpha: intercept annualized = (mean_strat - beta * mean_bh) * steps_per_year
        alpha = (mean_strat - beta * mean_bh) * steps_per_year
        stats.beta = beta
        stats.alpha = alpha

    # Tracking error and information ratio
    excess = [strat_rets[i] - bh_rets[i] for i in range(n)]
    mean_excess = sum(excess) / n
    var_excess = sum((e - mean_excess) ** 2 for e in excess) / n
    std_excess = math.sqrt(var_excess)

    if std_excess > 0:
        stats.tracking_error = std_excess * math.sqrt(steps_per_year)
        stats.information_ratio = mean_excess / std_excess * math.sqrt(steps_per_year)

    return stats


def _annualisation(equity_curve) -> float:
    """Estimate steps-per-year from the median spacing of the equity curve."""
    if len(equity_curve) < 3:
        return 252.0
    deltas = [
        (equity_curve[i][0] - equity_curve[i - 1][0]).total_seconds()
        for i in range(1, len(equity_curve))
    ]
    deltas = sorted(d for d in deltas if d > 0)
    if not deltas:
        return 252.0
    median = deltas[len(deltas) // 2]
    return 365.25 * 86_400 / median
