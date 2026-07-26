"""Backtest: the clock that drives everything (ADR-0001, -0002, -0004).

Per Base step: SimulatedVenue.on_base_bar() processes fills/SL-TP/costs for the new
bar; the MultiTimeframeView updates all Forming Bars; then ``on_bar`` is called
(suppressed during Lead-in until every declared Indicator is warm; ``on_start``
fires once at the boundary). A run is a pure function of (Strategy + Parameters,
data, config) — the contract the deferred Optimizer relies on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..ai import AIAdvisor
from ..core import Symbol, Timeframe
from ..data import DataSource, MultiTimeframeView
from ..execution import Account, CostModel, SimulatedVenue
from ..strategy import Strategy
from .result import BacktestResult

_UTC = UTC


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt.astimezone(_UTC)


@dataclass(slots=True)
class Backtest:
    strategy: Strategy
    source: DataSource
    symbol: Symbol
    timeframes: list[Timeframe]           # finest is the Base Timeframe
    start: datetime                        # Trading Window start
    end: datetime                          # Trading Window end
    starting_cash: float = 10_000.0
    cost_model: CostModel | None = None    # defaults per asset class if None
    magnifier: Timeframe | None = None     # opt-in Fill-resolution Timeframe
    advisor: AIAdvisor | None = None       # optional AI confirm/veto gate
    params: dict[str, object] = field(default_factory=dict)

    def run(self) -> BacktestResult:
        start = _as_utc(self.start)
        end = _as_utc(self.end)
        instrument = self.source.get_instrument(self.symbol)

        # --- wire the strategy & discover its declared indicators/timeframes ---
        strat = self.strategy
        strat.instrument = instrument
        strat._params = dict(self.params)  # param overrides seed before setup()
        strat.setup()

        subscribed = set(self.timeframes) | {ind.timeframe for ind in strat.registered_indicators}
        base = min(subscribed)
        higher = sorted(subscribed - {base})
        view = MultiTimeframeView(instrument, base, higher)

        account = Account(self.starting_cash)
        cost_model = self.cost_model or CostModel.default_for(instrument)
        magnifier_fn = self._make_magnifier(instrument) if self.magnifier else None
        venue = SimulatedVenue(instrument, account, cost_model, magnifier_bars=magnifier_fn)

        strat.base_timeframe = base
        strat.venue = venue
        strat.advisor = self.advisor
        strat._view = view

        # --- size the Lead-in and fetch base bars ------------------------------
        lead_start = self._lead_in_start(instrument, base, strat, start)
        bars = self.source.history(instrument, base, lead_start, end)
        bars = [b for b in bars if b.timestamp <= end]

        warm_needed: dict[Timeframe, int] = {}  # bars of each tf needed before ready
        for ind in strat.registered_indicators:
            warm_needed[ind.timeframe] = max(warm_needed.get(ind.timeframe, 0), ind.lookback)

        equity_curve: list[tuple[datetime, float]] = []
        trading = False
        for bar in bars:
            prev_closed = len(venue.closed_trades)
            venue.on_base_bar(bar)
            view.push(bar)

            if trading:
                for tr in venue.closed_trades[prev_closed:]:
                    strat.on_trade_closed(tr)

            if bar.timestamp < start:
                continue
            if not self._warm(view, strat, warm_needed):
                continue

            if not trading:
                trading = True
                strat.on_start()
            strat._current_bar = bar
            strat.on_bar(bar)
            equity_curve.append((bar.timestamp, venue.equity()))

        strat.on_stop()
        return BacktestResult.compute(equity_curve, venue.closed_trades)

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _warm(view, strat, warm_needed) -> bool:
        for tf, need in warm_needed.items():
            if len(view[tf].bars_for_compute()) < need:
                return False
        return True

    def _lead_in_start(self, instrument, base: Timeframe, strat, start: datetime) -> datetime:
        if not strat.registered_indicators:
            return start
        max_base_bars = 0
        for ind in strat.registered_indicators:
            ratio = ind.timeframe.seconds // base.seconds
            max_base_bars = max(max_base_bars, ind.lookback * ratio)
        needed = int(max_base_bars * 1.2) + 5
        session = instrument.session
        if session.is_24_7:
            lead_seconds = needed * base.seconds
        else:
            open_s = session.open_time.hour * 3600 + session.open_time.minute * 60
            close_s = session.close_time.hour * 3600 + session.close_time.minute * 60
            frac = max((close_s - open_s) / 86_400, 0.05)
            lead_seconds = needed * base.seconds / frac * (7 / 5)
        return start - timedelta(seconds=lead_seconds * 1.5)

    def _make_magnifier(self, instrument):
        """Return a callable Bar -> finer sub-bars for intrabar fill resolution.

        Best-effort: fetches the finer Fill-resolution Timeframe on demand and falls
        back to None (conservative rules) when unavailable.
        """
        finer = self.magnifier

        def _sub(bar):
            try:
                nxt = bar.timestamp + timedelta(seconds=bar.timeframe.seconds)
                return self.source.history(instrument, finer, bar.timestamp, nxt) or None
            except Exception:
                return None

        return _sub
