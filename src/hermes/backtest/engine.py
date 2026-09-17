"""Backtest: the clock that drives everything (ADR-0001, -0002, -0004).

Per Base step:
1. SimulatedVenue.on_base_bar() processes fills/SL-TP/costs for the new bar.
2. MultiTimeframeView.push() updates all Forming Bars / seals higher-TF bars.
3. Indicator updates — before on_bar fires so indicator_value() is always O(1):
   - On the first trading bar: precompute() seeds every indicator from lead-in history.
   - On each subsequent bar: on_bar_closed() when the indicator's TF bar sealed;
     on_forming_bar() for ``latest``-mode indicators when the forming bar changed.
4. on_bar() fires (suppressed during Lead-in; on_start() fires once at the boundary).

A run is a pure function of (Strategy + Parameters, data, config) — the contract
the deferred Optimizer relies on.

Indicator warmup gate:
- ``latest_confirmed`` indicators: require len(closed_bars) >= lookback (only sealed data).
- ``latest`` indicators: require len(bars_for_compute) >= lookback (forming bar counts).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..ai import AIAdvisor
from ..core import Symbol, Timeframe
from ..data import DataSource, MultiTimeframeView
from ..execution import Account, CostModel, SimulatedVenue
from ..indicators import Indicator
from ..strategy import Sizer, Strategy
from .result import BacktestResult, BenchmarkStats, _compute_benchmark_stats
from .validation import validate as _validate

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
    validate: bool = False                 # run StatValidation after the backtest
    unconstrained: bool = False            # skip capital check — orders never rejected for insufficient funds
    sizer: Sizer | None = None             # backtest-level sizer; overrides the strategy's own sizing when set

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
        venue = SimulatedVenue(instrument, account, cost_model,
                               magnifier_bars=magnifier_fn, unconstrained=self.unconstrained)

        strat.base_timeframe = base
        strat.venue = venue
        strat.advisor = self.advisor
        strat.sizer = self.sizer
        strat._view = view

        # --- build timeframe → indicator index for efficient per-bar updates ---
        tf_to_indicators: dict[Timeframe, list[Indicator]] = defaultdict(list)
        for ind in strat.registered_indicators:
            tf_to_indicators[ind.timeframe].append(ind)

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

        ref_tf_indicators: list[dict[Timeframe, list[Indicator]]] = []
        for ref, *_ in refs:
            rtf: dict[Timeframe, list[Indicator]] = defaultdict(list)
            for ind in ref.indicators:
                rtf[ind.timeframe].append(ind)
            ref_tf_indicators.append(rtf)

        # --- size the Lead-in over main + reference indicators, then fetch -----
        groups = [(strat.registered_indicators, base, instrument.session)]
        groups += [(r.indicators, rb, ri.session) for r, _v, ri, _s, rb in refs]
        lead_start = self._lead_in_start(start, groups)

        bars = [b for b in self.source.history(instrument, base, lead_start, end) if b.timestamp <= end]
        ref_feeds = []  # [ref_view, sorted_ref_bars, pointer]
        for _ref, ref_view, ref_inst, ref_source, ref_base in refs:
            rb = [b for b in ref_source.history(ref_inst, ref_base, lead_start, end) if b.timestamp <= end]
            ref_feeds.append([ref_view, rb, 0])

        equity_curve: list[tuple[datetime, float]] = []
        benchmark_equity_curve: list[tuple[datetime, float]] = []
        trading = False
        first_close: float | None = None   # for buy-and-hold benchmark
        last_close: float | None = None

        # Closed-bar counts per TF — tracked to detect new closures each step.
        closed_counts: dict[Timeframe, int] = {tf: 0 for tf in tf_to_indicators}
        ref_closed_counts: list[dict[Timeframe, int]] = [
            {tf: 0 for tf in rtf} for rtf in ref_tf_indicators
        ]

        for bar in bars:
            t = bar.timestamp

            # Advance reference feeds up to now — no look-ahead.
            for i, feed in enumerate(ref_feeds):
                rv, rb, ptr = feed
                rtf = ref_tf_indicators[i]
                rc = ref_closed_counts[i]
                while ptr < len(rb) and rb[ptr].timestamp <= t:
                    rv.push(rb[ptr])
                    ptr += 1
                feed[2] = ptr
                # Update ref indicator closed counts (batch: may have advanced many bars).
                if trading:
                    for tf, inds in rtf.items():
                        new_count = len(rv[tf].closed())
                        if new_count > rc.get(tf, 0):
                            closed = rv[tf].closed()
                            for ind in inds:
                                ind.on_bar_closed(closed)
                            rc[tf] = new_count
                        elif rv[tf].forming is not None:
                            bfc = rv[tf].bars_for_compute()
                            for ind in inds:
                                if ind.mode == "latest":
                                    ind.on_forming_bar(bfc)

            prev_closed = len(venue.closed_trades)
            venue.on_base_bar(bar)
            view.push(bar)

            if trading:
                for tr in venue.closed_trades[prev_closed:]:
                    strat.on_trade_closed(tr)

            if t < start:
                continue
            if not self._warm(view, tf_to_indicators, refs, ref_tf_indicators,
                               [rv for rv, _, _ in ref_feeds]):
                continue

            if not trading:
                trading = True
                # Precompute all strategy indicators from lead-in history.
                for tf, inds in tf_to_indicators.items():
                    closed = view[tf].closed()
                    for ind in inds:
                        ind.precompute(closed)
                    closed_counts[tf] = len(closed)
                # Precompute all reference indicators.
                for i, (ref, ref_view, *_) in enumerate(refs):
                    for tf, inds in ref_tf_indicators[i].items():
                        closed = ref_view[tf].closed()
                        for ind in inds:
                            ind.precompute(closed)
                        ref_closed_counts[i][tf] = len(closed)
                strat._current_bar = bar
                strat.on_start()
                first_close = bar.close
            else:
                # Incremental updates for strategy indicators.
                for tf, inds in tf_to_indicators.items():
                    series = view[tf]
                    new_count = len(series.closed())
                    if new_count > closed_counts[tf]:
                        closed = series.closed()
                        for ind in inds:
                            ind.on_bar_closed(closed)
                        closed_counts[tf] = new_count
                    elif series.forming is not None:
                        bfc = series.bars_for_compute()
                        for ind in inds:
                            if ind.mode == "latest":
                                ind.on_forming_bar(bfc)

            last_close = bar.close
            strat._current_bar = bar
            strat.on_bar(bar)
            equity_curve.append((t, venue.equity()))
            if first_close and first_close > 0:
                bh_eq = self.starting_cash * (bar.close / first_close)
                benchmark_equity_curve.append((t, bh_eq))

        strat.on_stop()
        num_params = len(strat.declared_parameters())
        result = BacktestResult.compute(equity_curve, venue.closed_trades, num_params=num_params)

        # --- buy-and-hold benchmark ------------------------------------------
        if first_close and last_close and first_close > 0 and equity_curve:
            description = f"Buy-and-hold {instrument.symbol.ticker}"
            result.benchmark_equity = benchmark_equity_curve
            result.benchmark = _compute_benchmark_stats(
                equity_curve, benchmark_equity_curve, description
            )

        # --- optional statistical validation ---------------------------------
        if self.validate:
            result.stat_validation = _validate(result)

        return result

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _warm(
        view,
        tf_to_indicators: dict,
        refs,
        ref_tf_indicators: list[dict],
        ref_views: list,
    ) -> bool:
        """Return True once every indicator has enough history to produce a value.

        ``latest_confirmed`` indicators require len(closed_bars) >= lookback;
        ``latest`` indicators accept len(bars_for_compute) >= lookback.
        """
        for tf, inds in tf_to_indicators.items():
            series = view[tf]
            for ind in inds:
                need = ind.lookback
                check = series.closed() if ind.mode == "latest_confirmed" else series.bars_for_compute()
                if len(check) < need:
                    return False
        for (ref, ref_view, *_), rtf in zip(refs, ref_tf_indicators):
            for tf, inds in rtf.items():
                series = ref_view[tf]
                for ind in inds:
                    need = ind.lookback
                    check = series.closed() if ind.mode == "latest_confirmed" else series.bars_for_compute()
                    if len(check) < need:
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
