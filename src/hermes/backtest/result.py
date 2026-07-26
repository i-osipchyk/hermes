"""BacktestResult: the viz-independent output of a run (CONTEXT.md).

Holds the equity curve, the Trade blotter (every closed Trade with entry/exit/
P&L/costs and any Advisor Decision), and computed metrics. Plotting and third-party
tear-sheets (quantstats) consume it; the object itself imports no plotting lib.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime

from ..execution import Trade


@dataclass(slots=True)
class Metrics:
    total_return: float | None = None
    cagr: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    num_trades: int = 0


@dataclass(slots=True)
class BacktestResult:
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)

    @classmethod
    def compute(cls, equity_curve, trades) -> BacktestResult:
        return cls(
            equity_curve=list(equity_curve),
            trades=list(trades),
            metrics=_metrics(equity_curve, trades),
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
        return {
            "metrics": asdict(self.metrics),
            "equity_curve": [(ts.isoformat(), eq) for ts, eq in self.equity_curve],
            "trades": [_trade_dict(t) for t in self.trades],
        }


def _trade_dict(t: Trade) -> dict:
    return {
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


def _metrics(equity_curve, trades) -> Metrics:
    m = Metrics(num_trades=len(trades))
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
            downside = [r for r in rets if r < 0]
            if downside:
                dvar = sum(r * r for r in downside) / len(downside)
                dstd = math.sqrt(dvar)
                if dstd > 0:
                    m.sortino = mean / dstd * math.sqrt(steps_per_year)

        # Time-based CAGR.
        span_days = (equity_curve[-1][0] - equity_curve[0][0]).total_seconds() / 86_400
        if span_days > 0 and start_eq > 0 and end_eq > 0:
            years = span_days / 365.25
            if years > 0:
                m.cagr = (end_eq / start_eq) ** (1 / years) - 1.0

        m.max_drawdown = _max_drawdown(equities)

    if trades:
        wins = [t for t in trades if (t.net_pnl or 0) > 0]
        losses = [t for t in trades if (t.net_pnl or 0) < 0]
        m.win_rate = len(wins) / len(trades)
        gross_win = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        m.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else math.inf
    return m


def _max_drawdown(equities: list[float]) -> float:
    peak = equities[0]
    max_dd = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            max_dd = min(max_dd, e / peak - 1.0)
    return max_dd


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
