"""Gap Breakout on the S&P 500 universe (2017 → present), bias-free.

Uses the fja05680/sp500 historical constituent CSV so the universe at each
point in time reflects actual index membership — stocks removed or delisted
mid-period are included for the window they were tradeable.

Run:
    .venv/bin/python strategies/gap_breakout_sp500.py

The first run fetches ~8 years of daily bars for every ever-member ticker
from yfinance and caches them to .hermes_cache/.  Subsequent runs are fast.

YFinanceSource limitation: tickers that are fully delisted (not just removed
from the index) will return no data and produce zero trades for their leg —
they do not crash the run.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hermes import UniverseBacktest, Timeframe
from hermes.data import ConstituentCalendar, YFinanceSource
from strategies.gap_breakout import COST_MODEL, GapBreakout

D1 = Timeframe.parse("1d")

CALENDAR_PATH = Path(__file__).parent.parent / "sp500_history.csv"
START = datetime(2017, 1, 1, tzinfo=UTC)
END   = datetime(2024, 12, 31, tzinfo=UTC)
CASH  = 1_000_000.0


def main() -> None:
    if not CALENDAR_PATH.exists():
        print(
            f"ERROR: {CALENDAR_PATH} not found.\n"
            "Download it with:\n"
            "  curl -sL 'https://raw.githubusercontent.com/fja05680/sp500/refs/"
            "heads/master/S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv'"
            " -o sp500_history.csv"
        )
        sys.exit(1)

    print("Loading constituent calendar …")
    cal = ConstituentCalendar.from_snapshot_csv(CALENDAR_PATH)

    universe = cal.ever_member(START, END)
    print(f"Universe: {len(universe)} tickers ever in the S&P 500 during {START.year}–{END.year}")

    print("Building UniverseBacktest …")
    ub = UniverseBacktest(
        strategy_factory=GapBreakout,
        source=YFinanceSource(),
        calendar=cal,
        timeframes=[D1],
        start=START,
        end=END,
        starting_cash=CASH,
        cost_model=COST_MODEL,
    )

    print("Running … (first run fetches bars from yfinance, may take 10–20 min)")
    result = ub.run()

    pr = result.portfolio_result
    m = pr.result.metrics

    print("\n─── Portfolio metrics ────────────────────────────────────────────")
    print(f"  Universe size          : {result.universe_size} tickers")
    print(f"  Total trades           : {m.num_trades}")
    print(f"  Win rate               : {m.win_rate:.1%}")
    print(f"  Total return           : {m.total_return:.2%}")
    print(f"  Sharpe ratio           : {m.sharpe:.2f}")
    print(f"  Max drawdown           : {m.max_drawdown:.2%}")

    print("\n─── Top 20 symbols by net P&L ────────────────────────────────────")
    rows = sorted(pr.summary_rows(), key=lambda r: r["net_pnl"] or 0, reverse=True)
    print(f"  {'Symbol':<16} {'Trades':>6} {'Win %':>7} {'Net P&L':>12}")
    print("  " + "─" * 44)
    for r in rows[:20]:
        win = f"{r['win_%']:.1f}%" if r["win_%"] is not None else "  n/a"
        print(f"  {r['symbol']:<16} {r['trades']:>6} {win:>7} {r['net_pnl']:>12,.2f}")

    print("\n─── Bottom 10 symbols by net P&L ─────────────────────────────────")
    for r in rows[-10:]:
        win = f"{r['win_%']:.1f}%" if r["win_%"] is not None else "  n/a"
        print(f"  {r['symbol']:<16} {r['trades']:>6} {win:>7} {r['net_pnl']:>12,.2f}")


if __name__ == "__main__":
    main()
