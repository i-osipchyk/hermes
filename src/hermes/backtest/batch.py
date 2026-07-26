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
        """The basket as one **shared, compounding** portfolio.

        One capital pool. Each closed trade's return relative to the equity it was
        risked against (``net_pnl / equity_at_entry`` — for risk-based sizing this is
        R-multiple × risk%, invariant to how much capital the sleeve had) is applied to
        the running pool in **exit-time order**. So a win on one symbol grows the capital
        available to the next trade on any symbol: 100 → risk 1%, +2R → 102 → risk 1.02,
        +2R → 104.04.
        """
        events: list[tuple] = []
        start_ts = None
        total_start = 0.0
        for r in self.results.values():
            if not r.equity_curve:
                continue
            total_start += r.equity_curve[0][1]
            first_ts = r.equity_curve[0][0]
            start_ts = first_ts if start_ts is None else min(start_ts, first_ts)
            for t in r.trades:
                if t.exit_time is None or t.net_pnl is None:
                    continue
                eq = _equity_at(r.equity_curve, t.entry_time)
                if eq > 0:
                    events.append((t.exit_time, t.net_pnl / eq))
        if start_ts is None:
            return []
        events.sort(key=lambda e: e[0])
        equity = total_start
        out = [(start_ts, equity)]
        for ts, frac in events:
            equity *= 1 + frac
            out.append((ts, equity))
        return out

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


def _equity_at(curve: list[tuple], ts) -> float:
    """Equity in ``curve`` (sorted by time) at-or-before ``ts`` — the equity a trade
    entered at that time was sized against. Falls back to the first value."""
    val = curve[0][1]
    for t, e in curve:
        if t <= ts:
            val = e
        else:
            break
    return val


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
