"""Build a trade-level dataset for EmaCrossover across the S&P 500 universe,
joined with news headlines and the point-in-time SEC filing period at each entry.

Feeds a downstream research pass (a separate Claude session reads the CSV,
looks for news/fundamentals combinations that separate winners from losers).
Any pattern found there gets encoded as a PIT screen or an AIAdvisor prompt
and validated out-of-sample.

Two phases, run in order:

    .venv/bin/python research/build_ema_crossover_dataset.py backtest
        Runs the bias-free UniverseBacktest (fja05680/sp500 constituent
        calendar) and prints the trade count. Bars are cached under
        .hermes_cache/, so this is fast on repeat runs. Writes the raw
        trade list to research/output/ema_crossover_trades_raw.csv
        (no fundamentals/news yet) so phase 2 can resume independently.

    .venv/bin/python research/build_ema_crossover_dataset.py enrich
        Reads the raw trade list and appends news + the latest annual
        (10-K) and latest quarterly (10-Q) SEC filing periods in effect at
        each entry date — fundamentals_annual_year/_quarter and
        fundamentals_quarterly_year/_quarter, kept separate rather than
        collapsed into whichever was filed most recently overall — writing
        research/output/ema_crossover_trades.csv one row at a time. Only
        the period identifiers are stored, not the raw financials
        themselves — a separate script can look those up later by
        (symbol, year, quarter) via hermes.ai.enrichers.massive_fundamentals,
        since the per-ticker filing history is cached to
        .cache/pit/_massive_financials/ and SEC filings don't change after
        the fact. Resumable: already-written (symbol, entry_date) rows are
        skipped, and news lookups are cached to .cache/pit/ regardless, so
        an interrupted run (e.g. Polygon free-tier rate limiting) can be
        restarted without re-paying for completed work.
"""

from __future__ import annotations

import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hermes import NotionalCash, Timeframe, UniverseBacktest
from hermes.ai.enrichers import PolygonNewsEnricher, latest_period
from hermes.data import ConstituentCalendar, YFinanceSource
from strategies.ema_crossover import EmaCrossover

# Fixed notional per trade, not a fraction of the shared portfolio equity: with
# unconstrained=True and ~700 simultaneous legs sharing one Account, an
# EquityFraction sizer (the strategy's default) compounds off the pooled equity
# across all legs and blows up (see ema_crossover.py's own docstring, which
# recommends NotionalCash for exactly this case).
TRADE_NOTIONAL = 10_000.0

D1 = Timeframe.parse("1d")
ROOT = Path(__file__).parent.parent
CALENDAR_PATH = ROOT / "sp500_history.csv"
START = datetime(2017, 1, 1, tzinfo=UTC)
END = datetime.now(UTC)
CASH = 1_000_000.0

OUT_DIR = Path(__file__).parent / "output"
RAW_PATH = OUT_DIR / "ema_crossover_trades_raw.csv"
FINAL_PATH = OUT_DIR / "ema_crossover_trades.csv"

RAW_FIELDS = ["symbol", "entry_date", "entry_price", "exit_date", "exit_price", "pnl"]
FINAL_FIELDS = RAW_FIELDS + [
    "news",
    "fundamentals_annual_year", "fundamentals_annual_quarter",
    "fundamentals_quarterly_year", "fundamentals_quarterly_quarter",
]


def run_backtest() -> None:
    if not CALENDAR_PATH.exists():
        print(f"ERROR: {CALENDAR_PATH} not found.")
        sys.exit(1)

    print("Loading constituent calendar …")
    cal = ConstituentCalendar.from_snapshot_csv(CALENDAR_PATH)

    print(f"Running EmaCrossover UniverseBacktest over the S&P 500, {START.date()} – {END.date()} …")
    ub = UniverseBacktest(
        strategy_factory=EmaCrossover,
        source=YFinanceSource(),
        calendar=cal,
        timeframes=[D1],
        start=START,
        end=END,
        starting_cash=CASH,
        unconstrained=True,  # each leg trades independently; we want every crossover
                             # signal's trade, not ones blocked by shared capital.
        sizer=NotionalCash(TRADE_NOTIONAL),
    )
    result = ub.run()
    per_symbol = result.portfolio_result.per_symbol

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    with RAW_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_FIELDS)
        writer.writeheader()
        for symbol, trades in per_symbol.items():
            ticker = symbol.split(":", 1)[-1]  # per_symbol keys are "source:ticker"
            for trade in trades:
                if trade.exit_time is None:
                    continue  # still open at window end
                writer.writerow({
                    "symbol": ticker,
                    "entry_date": trade.entry_time.date().isoformat(),
                    "entry_price": trade.entry_price,
                    "exit_date": trade.exit_time.date().isoformat(),
                    "exit_price": trade.exit_price,
                    "pnl": trade.net_pnl,
                })
                n += 1

    print(f"Universe: {result.universe_size} tickers ever-member {START.date()}–{END.date()}")
    print(f"Closed trades: {n}")
    print(f"Wrote raw trade list to {RAW_PATH}")


def run_enrich(year: int | None = None) -> None:
    if not RAW_PATH.exists():
        print(f"ERROR: {RAW_PATH} not found — run the 'backtest' phase first.")
        sys.exit(1)

    with RAW_PATH.open(newline="") as f:
        raw_rows = list(csv.DictReader(f))

    if year is not None:
        raw_rows = [r for r in raw_rows if r["entry_date"].startswith(f"{year}-")]
        print(f"Filtered to entry_date year {year}: {len(raw_rows)} trades.")

    done: set[tuple[str, str]] = set()
    write_header = not FINAL_PATH.exists()
    if FINAL_PATH.exists():
        with FINAL_PATH.open(newline="") as f:
            done = {(r["symbol"], r["entry_date"]) for r in csv.DictReader(f)}

    news = PolygonNewsEnricher()

    todo = [r for r in raw_rows if (r["symbol"], r["entry_date"]) not in done]
    print(f"{len(raw_rows)} total trades, {len(done)} already enriched, {len(todo)} remaining.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with FINAL_PATH.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FINAL_FIELDS)
        if write_header:
            writer.writeheader()

        t0 = datetime.now()
        for i, row in enumerate(todo, 1):
            symbol = row["symbol"]
            entry_date = datetime.fromisoformat(row["entry_date"]).date()

            try:
                annual_year, annual_quarter = latest_period(symbol, entry_date, "annual")
            except Exception as exc:
                annual_year, annual_quarter = None, f"error: {exc}"

            try:
                quarterly_year, quarterly_quarter = latest_period(symbol, entry_date, "quarterly")
            except Exception as exc:
                quarterly_year, quarterly_quarter = None, f"error: {exc}"

            try:
                news_text = news.enrich(symbol, entry_date).replace("\n", " | ")
            except Exception as exc:
                news_text = f"[error: {exc}]"

            out = dict(row)
            out["news"] = news_text
            out["fundamentals_annual_year"] = annual_year
            out["fundamentals_annual_quarter"] = annual_quarter
            out["fundamentals_quarterly_year"] = quarterly_year
            out["fundamentals_quarterly_quarter"] = quarterly_quarter
            writer.writerow(out)
            f.flush()

            if i % 50 == 0 or i == len(todo):
                elapsed = (datetime.now() - t0).total_seconds()
                rate = i / elapsed if elapsed else 0
                eta_min = (len(todo) - i) / rate / 60 if rate else float("inf")
                print(f"  … {i}/{len(todo)} enriched (latest: {symbol} {entry_date})  "
                      f"{rate:.1f}/s  ETA {eta_min:.0f}m")

    print(f"Done. Wrote enriched dataset to {FINAL_PATH}")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "backtest"
    if phase == "backtest":
        run_backtest()
    elif phase == "enrich":
        year_arg = int(sys.argv[2]) if len(sys.argv) > 2 else None
        run_enrich(year_arg)
    else:
        print("Usage: build_ema_crossover_dataset.py [backtest|enrich [year]]")
        sys.exit(1)
