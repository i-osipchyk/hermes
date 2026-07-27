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


def default_start() -> datetime:
    """Default Trading Window start: the beginning of the current year (UTC)."""
    return datetime(datetime.now(_UTC).year, 1, 1, tzinfo=_UTC)


def default_end() -> datetime:
    """Default Trading Window end: now (today, UTC)."""
    return datetime.now(_UTC)


@dataclass(slots=True)
class Backtest:
    strategy: Strategy
    source: DataSource
    symbol: Symbol
    # Finest is the Base Timeframe. Empty -> derived from the strategy's declared
    # Indicator timeframes (so a strategy can choose its timeframes via Parameters).
    timeframes: list[Timeframe] = field(default_factory=list)
    # Default Trading Window: year-to-date (Jan 1 this year → today).
    start: datetime = field(default_factory=default_start)
    end: datetime = field(default_factory=default_end)
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
        if not subscribed:
            raise ValueError(
                "No timeframes: set Backtest.timeframes or declare at least one Indicator."
            )
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

        # --- resolve reference feeds (observed, not traded) --------------------
        refs = []  # (reference, ref_view, ref_instrument, ref_source, ref_base)
        for ref in strat.references:
            ref_source = ref.source or self.source
            ref_inst = ref_source.get_instrument(Symbol(ref.symbol, ref_source.name))
            ref_tfs = ref.timeframes or subscribed
            ref_base = min(ref_tfs)
            ref_view = MultiTimeframeView(ref_inst, ref_base, sorted(set(ref_tfs) - {ref_base}))
            ref._view = ref_view
            refs.append((ref, ref_view, ref_inst, ref_source, ref_base))

        # --- size the Lead-in over main + reference indicators, then fetch -----
        groups = [(strat.registered_indicators, base, instrument.session)]
        groups += [(r.indicators, rb, ri.session) for r, _v, ri, _s, rb in refs]
        lead_start = self._lead_in_start(start, groups)

        bars = [b for b in self.source.history(instrument, base, lead_start, end) if b.timestamp <= end]
        ref_feeds = []  # [ref_view, sorted_ref_bars, pointer]
        for _ref, ref_view, ref_inst, ref_source, ref_base in refs:
            rb = [b for b in ref_source.history(ref_inst, ref_base, lead_start, end) if b.timestamp <= end]
            ref_feeds.append([ref_view, rb, 0])

        warm_needed: dict[Timeframe, int] = {}  # bars of each tf needed before ready
        for ind in strat.registered_indicators:
            warm_needed[ind.timeframe] = max(warm_needed.get(ind.timeframe, 0), ind.lookback)

        equity_curve: list[tuple[datetime, float]] = []
        trading = False
        for bar in bars:
            t = bar.timestamp
            # advance reference feeds up to now — no look-ahead
            for feed in ref_feeds:
                rv, rb, ptr = feed
                while ptr < len(rb) and rb[ptr].timestamp <= t:
                    rv.push(rb[ptr])
                    ptr += 1
                feed[2] = ptr

            prev_closed = len(venue.closed_trades)
            venue.on_base_bar(bar)
            view.push(bar)

            if trading:
                for tr in venue.closed_trades[prev_closed:]:
                    strat.on_trade_closed(tr)

            if t < start:
                continue
            if not self._warm(view, warm_needed, refs):
                continue

            if not trading:
                trading = True
                strat.on_start()
            strat._current_bar = bar
            strat.on_bar(bar)
            equity_curve.append((t, venue.equity()))

        strat.on_stop()
        return BacktestResult.compute(equity_curve, venue.closed_trades)

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _warm(view, warm_needed, refs) -> bool:
        for tf, need in warm_needed.items():
            if len(view[tf].bars_for_compute()) < need:
                return False
        for ref, ref_view, *_ in refs:
            for ind in ref.indicators:
                if len(ref_view[ind.timeframe].bars_for_compute()) < ind.lookback:
                    return False
        return True

    def _lead_in_start(self, start: datetime, groups) -> datetime:
        """Earliest Lead-in start across all indicator groups (main + references).

        ``groups`` is an iterable of ``(indicators, base_timeframe, session)``; the
        group needing the most history wins."""
        earliest = start
        for indicators, base, session in groups:
            if not indicators:
                continue
            max_base_bars = max(
                ind.lookback * (ind.timeframe.seconds // base.seconds) for ind in indicators
            )
            needed = int(max_base_bars * 1.2) + 5
            if session.is_24_7:
                lead_seconds = needed * base.seconds
            else:
                open_s = session.open_time.hour * 3600 + session.open_time.minute * 60
                close_s = session.close_time.hour * 3600 + session.close_time.minute * 60
                frac = max((close_s - open_s) / 86_400, 0.05)
                lead_seconds = needed * base.seconds / frac * (7 / 5)
            earliest = min(earliest, start - timedelta(seconds=lead_seconds * 1.5))
        return earliest

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
