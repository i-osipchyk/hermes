# Parameters: 1  (metric)
#
# US equity value (+ momentum) baseline (idea 21060772; Fama-French 1992, LSV 1994).
#
#   Universe: point-in-time S&P 500 (sp500_history.csv), delisted names included; prices from
#             yfinance, and from Tiingo for the delisted names yfinance lacks (~25% of the
#             ever-members; Tiingo's free tier cannot carry all ~830).
#   Signal:   research/value/build_panel.py ranks the index on the last trading day of each
#             quarter by a valuation yield, using only SEC filings already filed that day and
#             that day's unadjusted close. `metric` picks the yield:
#               ep    = trailing-12m net income / market cap
#               bp    = latest book equity / market cap
#               combo = mean of the E/P and B/P percentile ranks (needs both)
#               ibp     = (book + capitalised R&D) / market cap        (idea f99af81f)
#               ibp_org = ibp + capitalised 30% of SG&A (organization capital)
#               mom     = 12-1 month return, dividend-adjusted              (idea f62f0c77)
#               vm_bp / vm_ibp_org = 50/50 of that value quintile and the momentum quintile
#               all   = no ranking: hold every scored stock (the equal-weight benchmark)
#   Position: long the top quintile, equal weight at entry (GROSS / basket size), entered at
#             the next open; held while it stays in the quintile, so weights drift within the
#             quarter. A stock that leaves the index is closed when its leg ends.
#   Costs:    5 bp a side of notional (commission + half-spread + impact for S&P 500 names).
#
# The rebalance calendar (quarterly), the quintile and the gross exposure are constants.

from __future__ import annotations

import csv
import json
import math
import sys
from bisect import bisect_right
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT), str(_ROOT / "src")]

from hermes import (  # noqa: E402
    EquityFraction,
    Parameter,
    Strategy,
    Symbol,
    Timeframe,
    UniverseBacktest,
    YFinanceSource,
)
from hermes.data import BarCache, ConstituentCalendar, DataSource  # noqa: E402
from hermes.execution import (  # noqa: E402
    CostModel,
    FinancingModel,
    PercentCommission,
    SlippageModel,
    SpreadModel,
)

GENERATED_BY = "hermes-strategy"

D1 = Timeframe.parse("1d")
PANEL = _ROOT / "research" / "output" / "value" / "panel.csv"
VENDORS = PANEL.with_name("vendors.json")
CALENDAR = _ROOT / "sp500_history.csv"
METRICS = ("ep", "bp", "combo", "ibp", "ibp_org", "mom", "vm_bp", "vm_ibp_org", "all")
VALUES = ("ep", "bp", "ibp", "ibp_org", "mom")
SLEEVES = {"vm_bp": ("bp", "mom"), "vm_ibp_org": ("ibp_org", "mom")}   # 50/50 combinations
QUANTILE = 0.2
GROSS = 0.95          # leave a little cash for drift
EXIT_DAYS = 4         # calendar days before an index exit to sell (leg bars stop there)
STILL_MEMBER = date(2100, 1, 1)

COST_MODEL = CostModel(
    commission=PercentCommission(rate=0.0005),
    spread=SpreadModel(points=0.0),
    slippage=SlippageModel(ticks=0.0),
    financing=FinancingModel(annual_rate=0.0),
)


@cache
def _panel() -> dict[date, list[dict]]:
    if not PANEL.exists():
        raise FileNotFoundError(f"{PANEL} missing: run research/value/build_panel.py first")
    out: dict[date, list[dict]] = {}
    with open(PANEL) as fh:
        for r in csv.DictReader(fh):
            if r["why"]:
                continue
            out.setdefault(date.fromisoformat(r["date"]), []).append({
                "ticker": r["ticker"],
                **{k: float(r[k]) if r.get(k) else None for k in VALUES},
            })
    return out


def _pct_rank(rows: list[dict], key: str) -> dict[str, float]:
    ordered = sorted(rows, key=lambda r: r[key])
    n = len(ordered)
    return {r["ticker"]: i / (n - 1) for i, r in enumerate(ordered)} if n > 1 else {}


def _top(rows: list[dict], metric: str) -> list[str]:
    """Top quintile by ``metric`` (combo: mean of the E/P and B/P percentile ranks)."""
    if metric == "combo":
        both = [r for r in rows if r["ep"] is not None and r["bp"] is not None]
        ep, bp = _pct_rank(both, "ep"), _pct_rank(both, "bp")
        scored = [(ep[r["ticker"]] + bp[r["ticker"]], r["ticker"]) for r in both]
    else:
        scored = [(r[metric], r["ticker"]) for r in rows if r[metric] is not None]
    scored.sort(reverse=True)
    return [t for _, t in scored[: math.ceil(len(scored) * QUANTILE)]]


@cache
def selections(metric: str) -> tuple[tuple[date, ...], dict[date, dict[str, float]]]:
    """Rebalance date -> {ticker: weight}, weights summing to 1, from the point-in-time panel.
    A 50/50 metric ("vm_bp" = value bp + momentum) gives each sleeve half, equal weight
    within it; a stock in both sleeves gets both halves."""
    picks: dict[date, dict[str, float]] = {}
    for d, rows in _panel().items():
        if metric == "all":
            sleeves = [[r["ticker"] for r in rows]]
        else:
            sleeves = [_top(rows, m) for m in SLEEVES.get(metric, (metric,))]
        w: dict[str, float] = {}
        for sleeve in sleeves:
            for t in sleeve:
                w[t] = w.get(t, 0.0) + 1 / (len(sleeves) * len(sleeve))
        picks[d] = w
    return tuple(sorted(picks)), picks


class Value(Strategy):
    base_tf = D1

    def setup(self) -> None:
        self.metric = self.param(Parameter(
            "metric", "ep", choices=METRICS,
            description="Valuation yield ranked each quarter (all = equal-weight benchmark)"))
        self.dates, self.picks = selections(self.metric)

    def on_bar(self, bar) -> None:
        # Evaluated every bar, not once per quarter: the venue checks cash when an order is
        # *submitted*, so a buy placed alongside other legs' same-day sells can be rejected;
        # re-asking the next day fills it once those sells have released the cash.
        d = bar.timestamp.date()
        i = bisect_right(self.dates, d) - 1
        if i < 0:
            return
        ticker = self.instrument.symbol.ticker
        weight = self.picks[self.dates[i]].get(ticker)
        leaving = _index_exit(ticker)
        # A leg's bars stop when the stock leaves the index, but the engine only closes its
        # position at the end of the whole run -- so sell a few days before the exit.
        want = weight is not None and not (leaving and 0 <= (leaving - d).days <= EXIT_DAYS)
        trades = self.venue.open_trades()
        if trades and not want:
            for t in trades:
                self.close(t)
        elif want and not trades:
            self.buy(EquityFraction(GROSS * weight), tag=self.metric)


@cache
def _calendar() -> ConstituentCalendar:
    return ConstituentCalendar.from_snapshot_csv(CALENDAR)


@cache
def _index_exit(ticker: str) -> date | None:
    """Last day of ``ticker``'s membership window, or None while it is still a member."""
    w = _calendar().membership_window(ticker, date(1990, 1, 1), STILL_MEMBER)
    return None if w is None or w[1] >= STILL_MEMBER else w[1]


class PanelPrices(DataSource):
    """Adjusted daily bars from whichever vendor build_panel.py used for each ticker:
    yfinance where it has the stock, Tiingo (read from the bar cache only, never fetched
    here) for the delisted names yfinance lacks. vendors.json records the split."""

    name = "value-panel"

    def __init__(self) -> None:
        self.yf = YFinanceSource()
        self.cache = BarCache()
        self.vendors = json.loads(VENDORS.read_text())

    def get_instrument(self, symbol: Symbol):
        return self.yf.get_instrument(symbol)

    def history(self, instrument, timeframe, start, end):
        t = instrument.symbol.ticker
        vendor = self.vendors.get(t)
        if vendor == "yfinance":
            inst = self.yf.get_instrument(Symbol(t.replace(".", "-"), "yfinance"))
            return self.yf.history(inst, timeframe, start, end)
        if vendor == "tiingo":
            inst = self.yf.get_instrument(Symbol(t, "tiingo"))
            return self.cache.read(inst, timeframe, start, end)
        return []

    def supported_timeframes(self) -> set[Timeframe]:
        return {D1}


def build_universe_backtest(**overrides) -> UniverseBacktest:
    kw: dict = dict(
        strategy_factory=Value,
        source=PanelPrices(),
        calendar=_calendar(),
        timeframes=[D1],
        # In-sample window. 2022-01-01..2026-09-30 is the holdout (idea 21060772): do not
        # run there until a candidate is frozen.
        start=datetime(2011, 1, 1, tzinfo=UTC),
        end=datetime(2021, 12, 31, tzinfo=UTC),
        starting_cash=1_000_000.0,
        cost_model=COST_MODEL,
    )
    kw.update(overrides)
    return UniverseBacktest(**kw)


if __name__ == "__main__":
    r = build_universe_backtest().run()
    print(r.portfolio_result.result.metrics)
