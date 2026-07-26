"""Reporting: built-in plots and an optional quantstats tear-sheet.

Kept separate from :class:`BacktestResult` so the core stays viz-independent and
these heavier deps (matplotlib/quantstats) are only imported when used.
"""

from __future__ import annotations

from .result import BacktestResult


def plot_equity(result: BacktestResult, ax=None):
    """Equity curve with drawdown shading."""
    import matplotlib.pyplot as plt

    if not result.equity_curve:
        raise ValueError("No equity curve to plot")
    times, eq = zip(*result.equity_curve)
    if ax is None:
        _, ax = plt.subplots(figsize=(11, 4))
    ax.plot(times, eq, label="Equity")
    peak, dd = eq[0], []
    for e in eq:
        peak = max(peak, e)
        dd.append(e - peak)
    ax2 = ax.twinx()
    ax2.fill_between(times, dd, 0, color="red", alpha=0.15, label="Drawdown")
    ax.set_title("Equity curve")
    ax.legend(loc="upper left")
    return ax


def plot_trades(result: BacktestResult, bars=None, ax=None):
    """Price with entries/exits marked — the primary debugging view. Pass the base
    ``bars`` (list[Bar]) to draw price; otherwise only trade markers are shown."""
    import matplotlib.pyplot as plt

    if ax is None:
        _, ax = plt.subplots(figsize=(11, 5))
    if bars:
        ax.plot([b.timestamp for b in bars], [b.close for b in bars], color="black", lw=0.8)
    for t in result.trades:
        ax.scatter(t.entry_time, t.entry_price, marker="^", color="green", zorder=3)
        if t.exit_time is not None:
            color = "red" if (t.net_pnl or 0) < 0 else "blue"
            ax.scatter(t.exit_time, t.exit_price, marker="v", color=color, zorder=3)
    ax.set_title("Trades")
    return ax


def tearsheet(result: BacktestResult, output: str | None = None):
    """Hand the equity returns to quantstats for a full HTML/inline report."""
    import quantstats as qs

    returns = result.to_frame().pct_change().dropna()
    if output:
        qs.reports.html(returns, output=output)
        return output
    return qs.reports.metrics(returns, display=False)
