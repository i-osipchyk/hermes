"""Run a single-instrument Strategy across many symbols — a universe *scan*.

Hermes strategies trade one Instrument (ADR-0003). To test a strategy on a basket
(e.g. a stock list) we run it **independently per symbol** and collect the results.
This is a scan, not a portfolio: the runs share no capital and never interact.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .engine import Backtest
from .result import BacktestResult


@dataclass(slots=True)
class BatchResult:
    results: dict[str, BacktestResult] = field(default_factory=dict)  # ticker -> result
    errors: dict[str, str] = field(default_factory=dict)             # ticker -> error text

    def summary_rows(self) -> list[dict]:
        """One readable row of headline metrics per symbol (numeric, so it sorts)."""
        rows = []
        for ticker, r in self.results.items():
            m = r.metrics
            rows.append(
                {
                    "symbol": ticker,
                    "return_%": _pct(m.total_return),
                    "cagr_%": _pct(m.cagr),
                    "sharpe": _round(m.sharpe),
                    "max_dd_%": _pct(m.max_drawdown),
                    "win_%": _pct(m.win_rate),
                    "profit_factor": _round(m.profit_factor),
                    "trades": m.num_trades,
                }
            )
        return rows

    def combined_equity_curve(self) -> list[tuple]:
        """The basket viewed as one portfolio: each symbol funded with equal capital and
        run independently, its equity summed across the union of timestamps (carried
        forward between a symbol's points, and held at its starting capital before it
        begins). Return metrics on this curve are the equal-weight portfolio's."""
        import pandas as pd

        cols = {sym: pd.Series(dict(r.equity_curve)) for sym, r in self.results.items() if r.equity_curve}
        if not cols:
            return []
        total = pd.DataFrame(cols).sort_index().ffill().bfill().sum(axis=1)
        return [(ts.to_pydatetime(), float(v)) for ts, v in total.items()]

    def combined_result(self) -> BacktestResult:
        """A single BacktestResult for the whole basket — combined equity curve + every
        symbol's trades — so it renders like any other run."""
        trades = [t for r in self.results.values() for t in r.trades]
        return BacktestResult.compute(self.combined_equity_curve(), trades)

    def aggregate(self) -> dict:
        """Universe-level summary across the per-symbol runs."""
        rets = [r.metrics.total_return for r in self.results.values() if r.metrics.total_return is not None]
        sharpes = [r.metrics.sharpe for r in self.results.values() if r.metrics.sharpe is not None]
        return {
            "symbols": len(self.results),
            "mean_return": (sum(rets) / len(rets)) if rets else None,
            "median_sharpe": _median(sharpes),
            "pct_profitable": (sum(1 for x in rets if x > 0) / len(rets)) if rets else None,
            "total_trades": sum(r.metrics.num_trades for r in self.results.values()),
            "errors": len(self.errors),
        }


def run_batch(
    tickers: list[str],
    build: Callable[[str], Backtest],
    progress: Callable[[int, int], None] | None = None,
) -> BatchResult:
    """Run one backtest per ticker via ``build(ticker) -> Backtest``. Per-symbol errors
    (bad ticker, no data) are captured rather than aborting the whole batch."""
    out = BatchResult()
    total = len(tickers)
    for i, ticker in enumerate(tickers):
        if progress:
            progress(i, total)
        try:
            out.results[ticker] = build(ticker).run()
        except Exception as exc:  # noqa: BLE001 - collect, don't abort the batch
            out.errors[ticker] = str(exc)
    if progress:
        progress(total, total)
    return out


def _pct(x) -> float | None:
    return round(x * 100, 2) if x is not None else None


def _round(x) -> float | None:
    return round(x, 2) if x is not None else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
