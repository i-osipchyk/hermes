"""UniverseBacktest: bias-free portfolio backtest over a historical index universe.

Survivorship bias inflates backtest returns when the test universe is fixed to
*today's* index members — stocks that were delisted or removed are simply
absent, so only winners are visible.

:class:`UniverseBacktest` eliminates this bias by:

1. Consulting a :class:`~hermes.data.ConstituentCalendar` to discover every
   ticker that was *ever* a member of the index during ``[start, end]``.
2. Clipping each leg's trading window to the symbol's confirmed membership
   period (``membership_window``), so the strategy never trades a stock before
   it joined the index or after it was removed.
3. Delegating to :class:`~hermes.backtest.PortfolioBacktest` with a shared
   capital pool for the full, time-correct universe.

Usage::

    from hermes import TiingoSource, Timeframe
    from hermes.backtest import UniverseBacktest
    from hermes.data import ConstituentCalendar
    from strategies.gap_breakout import GapBreakout
    from datetime import datetime, UTC

    source = TiingoSource()   # reads TIINGO_API_KEY from env
    cal = ConstituentCalendar.from_snapshot_csv("sp500_history.csv")

    ub = UniverseBacktest(
        strategy_factory=GapBreakout,   # called once per symbol
        source=source,
        calendar=cal,
        timeframes=[Timeframe.parse("1d")],
        start=datetime(2015, 1, 1, tzinfo=UTC),
        end=datetime(2023, 12, 31, tzinfo=UTC),
        starting_cash=1_000_000,
    )
    result = ub.run()
    print(result.portfolio_result.result.metrics)
    print(result.summary_rows())
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from ..core import Symbol, Timeframe
from ..data.constituent_calendar import ConstituentCalendar
from ..data.source import DataSource
from ..execution import CostModel
from ..strategy import Strategy
from .engine import Backtest
from .portfolio import PortfolioBacktest, PortfolioResult


@dataclass(slots=True)
class UniverseResult:
    """Result of a :class:`UniverseBacktest` run.

    Attributes:
        portfolio_result:   The combined :class:`PortfolioResult` with shared
                            equity curve and per-symbol trade breakdown.
        membership_windows: Mapping of ``ticker -> (first_seen, last_seen)``
                            dates.  Reflects the actual constituent windows
                            that each leg's start/end was clipped to.
        universe_size:      Total number of distinct tickers in the universe
                            (active *and* delisted).
    """

    portfolio_result: PortfolioResult
    membership_windows: dict[str, tuple[date, date]] = field(default_factory=dict)
    universe_size: int = 0

    def summary_rows(self) -> list[dict]:
        """Delegate to the underlying portfolio result summary."""
        return self.portfolio_result.summary_rows()


@dataclass
class UniverseBacktest:
    """Run a strategy against a historical index universe, bias-free.

    Each symbol that was ever a constituent during ``[start, end]`` gets its
    own :class:`Backtest` leg.  The leg's time window is clipped to the
    symbol's confirmed membership period so there is no look-ahead (a stock
    not yet in the index is not traded) and no survivorship bias (delisted
    stocks are included for the period they were tradeable).

    Args:
        strategy_factory:  Zero-argument callable that returns a fresh
                           :class:`~hermes.strategy.Strategy` instance.
                           Called once per symbol so each leg has an
                           independent state.
        source:            :class:`~hermes.data.DataSource` used to fetch
                           OHLC bars.  Use :class:`~hermes.data.TiingoSource`
                           for delisted-ticker coverage.
        calendar:          :class:`~hermes.data.ConstituentCalendar` that
                           maps dates to index membership lists.
        timeframes:        Timeframes to subscribe each leg to (e.g.
                           ``[Timeframe.parse("1d")]``).
        start:             Overall backtest start date (UTC).
        end:               Overall backtest end date (UTC).
        starting_cash:     Total portfolio capital shared across all legs.
        cost_model:        Optional cost model; defaults per asset class.
        params:            Parameter overrides applied to every strategy
                           instance (e.g. ``{"atr_sl_mult": 2.0}``).
    """

    strategy_factory: Callable[[], Strategy]
    source: DataSource
    calendar: ConstituentCalendar
    timeframes: list[Timeframe]
    start: datetime
    end: datetime
    starting_cash: float = 100_000.0
    cost_model: CostModel | None = None
    params: dict[str, object] = field(default_factory=dict)
    unconstrained: bool = False  # skip capital check — orders never rejected for insufficient funds
    sizer: object | None = None  # backtest-level sizer applied to every leg
    advisor: object | None = None  # optional AIAdvisor wired to every leg
    progress_callback: object | None = None  # Callable[[int, int], None] | None — forwarded to PortfolioBacktest

    def run(self) -> UniverseResult:
        """Build and run the bias-free portfolio backtest.

        Returns:
            :class:`UniverseResult` with the combined equity curve, per-symbol
            trade breakdown, and the membership windows used for each leg.
        """
        tickers = self.calendar.ever_member(self.start, self.end)
        legs: list[Backtest] = []
        windows: dict[str, tuple[date, date]] = {}

        for ticker in tickers:
            window = self.calendar.membership_window(ticker, self.start, self.end)
            if window is None:
                continue

            first_seen, last_seen = window
            leg_start = _to_utc(first_seen)
            leg_end = _to_utc_end_of_day(last_seen)

            symbol = Symbol(ticker, self.source.name)
            legs.append(
                Backtest(
                    strategy=self.strategy_factory(),
                    source=self.source,
                    symbol=symbol,
                    timeframes=list(self.timeframes),
                    start=leg_start,
                    end=leg_end,
                    cost_model=self.cost_model,
                    params=dict(self.params),
                    sizer=self.sizer,
                    advisor=self.advisor,
                )
            )
            windows[ticker] = window

        if not legs:
            raise ValueError(
                "UniverseBacktest produced no legs.  Check that the calendar "
                "covers the requested [start, end] range and contains at least "
                "one ticker."
            )

        portfolio = PortfolioBacktest(legs=legs, starting_cash=self.starting_cash,
                                      unconstrained=self.unconstrained,
                                      progress_callback=self.progress_callback)
        portfolio_result = portfolio.run()

        return UniverseResult(
            portfolio_result=portfolio_result,
            membership_windows=windows,
            universe_size=len(tickers),
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _to_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _to_utc_end_of_day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)
