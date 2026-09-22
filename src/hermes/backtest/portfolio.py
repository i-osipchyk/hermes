"""PortfolioBacktest: one shared capital pool across multiple instruments.

Each *leg* is a pre-configured :class:`Backtest` (one Strategy, one instrument).
All legs share a single :class:`~hermes.execution.Account`; the simulation clock is
synchronised so bar-by-bar margin is competed for in real time — a win on one symbol
grows the capital available to the next trade on any other.

Each leg's :class:`~hermes.execution.SimulatedVenue` is wired with a cross-leg margin
callback so the ``_can_afford`` gate accounts for all other legs' open notional.
The shared ``account.cash`` is always correct (every fill credits/debits it).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from ..core import Symbol, Timeframe
from ..data import MultiTimeframeView
from ..execution import Account, CostModel, SimulatedVenue
from ..indicators import Indicator
from .engine import Backtest, _as_utc
from .result import BacktestResult


@dataclass
class _LegState:
    """Wired, ready-to-clock state for one portfolio leg."""

    strategy: object          # Strategy instance
    venue: SimulatedVenue
    view: MultiTimeframeView
    warm_needed: dict[Timeframe, int]
    tf_to_indicators: dict[Timeframe, list[Indicator]]
    closed_counts: dict[Timeframe, int]
    bars: list                # pre-fetched base bars (lead-in + trading window)
    start: datetime           # trading-window start (bars before this are Lead-in)
    ref_feeds: list           # [[ref_view, bars, pointer], ...]
    ref_tf_indicators: list   # [dict[Timeframe, list[Indicator]], ...] — one per reference
    ref_closed_counts: list   # [dict[Timeframe, int], ...] — one per reference
    trading: bool = False


@dataclass(slots=True)
class PortfolioResult:
    """Result of a :class:`PortfolioBacktest` run.

    ``result`` holds the true shared-equity curve (recomputed from the one Account) and
    every trade across all legs.  ``per_symbol`` breaks trades out by ticker so you can
    see each instrument's individual contribution.
    """

    result: BacktestResult
    per_symbol: dict[str, list] = field(default_factory=dict)  # ticker str -> list[Trade]

    def summary_rows(self) -> list[dict]:
        """One headline row per symbol: trade count, win-rate, net P&L."""
        rows = []
        for sym, trades in self.per_symbol.items():
            if not trades:
                rows.append({"symbol": sym, "trades": 0, "win_%": None, "net_pnl": 0.0})
                continue
            wins = [t for t in trades if (t.net_pnl or 0) > 0]
            net = sum(t.net_pnl or 0 for t in trades)
            rows.append(
                {
                    "symbol": sym,
                    "trades": len(trades),
                    "win_%": round(len(wins) / len(trades) * 100, 1),
                    "net_pnl": round(net, 2),
                }
            )
        return rows


@dataclass(slots=True)
class PortfolioBacktest:
    """Run multiple :class:`Backtest` legs against a single shared capital pool.

    ``starting_cash`` is the total portfolio cash; ``legs[i].starting_cash`` is ignored.
    Every leg must share compatible timeframes (the same base or an integer-multiple
    hierarchy) and the same trading window (``start``/``end``).  Mixed timeframes across
    legs are supported — each leg advances on its own bar clock.

    Usage::

        bt_aapl = Backtest(strategy=MyStrat(), source=yf, symbol=Symbol("AAPL","yfinance"), ...)
        bt_msft = Backtest(strategy=MyStrat(), source=yf, symbol=Symbol("MSFT","yfinance"), ...)
        port = PortfolioBacktest(legs=[bt_aapl, bt_msft], starting_cash=20_000)
        pr = port.run()
        print(pr.result.metrics)
        print(pr.summary_rows())
    """

    legs: list[Backtest]
    starting_cash: float = 10_000.0
    unconstrained: bool = False  # skip capital check — orders never rejected for insufficient funds
    progress_callback: object | None = None  # Callable[[int, int], None] | None — called as (done, total)

    def run(self) -> PortfolioResult:
        if not self.legs:
            raise ValueError("PortfolioBacktest requires at least one leg.")

        account = Account(self.starting_cash)
        all_states = [self._wire(leg, account, self.unconstrained) for leg in self.legs]
        leg_states: list[_LegState] = []
        for leg, ls in zip(self.legs, all_states):
            if not ls.bars:
                import warnings
                warnings.warn(f"Skipping {leg.symbol}: no data found in cache.", stacklevel=2)
            else:
                leg_states.append(ls)

        # Give each venue a cross-leg margin view so _can_afford rejects orders that
        # would exceed the shared capital pool.
        for i, ls in enumerate(leg_states):
            others = [lx.venue for j, lx in enumerate(leg_states) if j != i]
            ls.venue._external_margin = lambda vs=others: sum(v.used_margin() + v.pending_margin() for v in vs)

        # Unified sorted event stream: (timestamp, leg_index, bar).
        # Ties broken by leg order so deterministic.
        events: list[tuple[datetime, int, object]] = []
        for i, ls in enumerate(leg_states):
            for bar in ls.bars:
                events.append((bar.timestamp, i, bar))
        events.sort(key=lambda e: (e[0], e[1]))

        equity_curve: list[tuple[datetime, float]] = []
        prev_closed_counts = [0] * len(leg_states)

        _window_start = min(_as_utc(leg.start) for leg in self.legs)
        _total_events = sum(1 for ts, _i, _b in events if ts >= _window_start)
        _cb = self.progress_callback
        _cb_done = 0
        for (ts, idx, bar) in events:
            if ts >= _window_start:
                if _cb and _cb_done % 1000 == 0:
                    _cb(_cb_done, _total_events, ts)
                _cb_done += 1
            ls = leg_states[idx]

            # Advance reference feeds for this leg up to now (no look-ahead).
            # Also update reference indicators for any newly closed bars.
            for i, feed in enumerate(ls.ref_feeds):
                rv, rb, ptr = feed
                while ptr < len(rb) and rb[ptr].timestamp <= ts:
                    rv.push(rb[ptr])
                    ptr += 1
                feed[2] = ptr
                if ls.trading:
                    rtf = ls.ref_tf_indicators[i]
                    rc = ls.ref_closed_counts[i]
                    for tf, inds in rtf.items():
                        new_count = len(rv[tf].closed())
                        if new_count > rc.get(tf, 0):
                            closed = rv[tf].closed()
                            for ind in inds:
                                ind.on_bar_closed(closed)
                            rc[tf] = new_count

            prev_closed = prev_closed_counts[idx]
            ls.venue.on_base_bar(bar)
            ls.view.push(bar)

            if ls.trading:
                for tr in ls.venue.closed_trades[prev_closed:]:
                    ls.strategy.on_trade_closed(tr)
            prev_closed_counts[idx] = len(ls.venue.closed_trades)

            if ts < ls.start:
                continue
            if not _warm(ls.view, ls.warm_needed):
                continue

            if not ls.trading:
                ls.trading = True
                # Precompute all indicators from lead-in history, mirroring engine.py.
                for tf, inds in ls.tf_to_indicators.items():
                    closed = ls.view[tf].closed()
                    for ind in inds:
                        ind.precompute(closed)
                    ls.closed_counts[tf] = len(closed)
                # Precompute reference indicators, mirroring engine.py.
                for i, feed in enumerate(ls.ref_feeds):
                    rv, rb, ptr = feed
                    rtf = ls.ref_tf_indicators[i]
                    rc = ls.ref_closed_counts[i]
                    for tf, inds in rtf.items():
                        closed = rv[tf].closed()
                        for ind in inds:
                            ind.precompute(closed)
                        rc[tf] = len(closed)
                ls.strategy._current_bar = bar
                ls.strategy.on_start()
            else:
                # Incremental indicator updates, mirroring engine.py.
                for tf, inds in ls.tf_to_indicators.items():
                    series = ls.view[tf]
                    new_count = len(series.closed())
                    if new_count > ls.closed_counts[tf]:
                        closed = series.closed()
                        for ind in inds:
                            ind.on_bar_closed(closed)
                        ls.closed_counts[tf] = new_count
                    elif series.forming is not None:
                        bfc = series.bars_for_compute()
                        for ind in inds:
                            if ind.mode == "latest":
                                ind.on_forming_bar(bfc)

            ls.strategy._current_bar = bar
            ls.strategy.on_bar(bar)

            # Portfolio equity = shared cash + unrealised PnL across ALL venues.
            total_unrealised = sum(lx.venue.unrealised_pnl() for lx in leg_states)
            equity_curve.append((ts, account.cash + total_unrealised))

        if _cb:
            _cb(_total_events, _total_events)

        last_ts = events[-1][0] if events else _window_start
        for ls in leg_states:
            ls.strategy.on_stop()
            ls.venue.force_close_all(last_ts)
        # Append a final equity point after all force-closes so the curve ends
        # at realized-only P&L (unrealised is now 0).
        total_unrealised = sum(lx.venue.unrealised_pnl() for lx in leg_states)
        equity_curve.append((last_ts, account.cash + total_unrealised))

        # Gather trades per symbol and build the combined BacktestResult.
        per_symbol: dict[str, list] = {}
        for leg, ls in zip(self.legs, leg_states):
            per_symbol[str(leg.symbol)] = ls.venue.closed_trades

        all_trades = [t for trades in per_symbol.values() for t in trades]
        all_vetoed = [o for ls in leg_states for o in ls.venue.vetoed_orders]
        num_params = max(
            (len(ls.strategy.declared_parameters()) for ls in leg_states),
            default=0,
        )
        # Resample to one point per calendar day: multiple legs share the same
        # date, so the raw curve has N-stocks entries per day. Keeping only the
        # last value per day restores correct per-period return spacing for
        # Sharpe, VaR, drawdown, and all other time-series metrics.
        daily_curve = _last_per_day(equity_curve)
        result = BacktestResult.compute(
            daily_curve, all_trades, num_params=num_params,
            vetoed_signals=all_vetoed,
        )
        return PortfolioResult(result=result, per_symbol=per_symbol)

    # --- internal wiring -------------------------------------------------------

    def _wire(self, bt: Backtest, account: Account, unconstrained: bool = False) -> _LegState:
        start = _as_utc(bt.start)
        end = _as_utc(bt.end)
        instrument = bt.source.get_instrument(bt.symbol)

        strat = bt.strategy
        strat.instrument = instrument
        strat._params = dict(bt.params)
        strat.setup()

        subscribed = set(bt.timeframes) | {ind.timeframe for ind in strat.registered_indicators}
        if not subscribed:
            raise ValueError(
                f"Leg {bt.symbol}: no timeframes — set Backtest.timeframes or declare an Indicator."
            )
        base = min(subscribed)
        higher = sorted(subscribed - {base})
        view = MultiTimeframeView(instrument, base, higher)

        cost_model = bt.cost_model or CostModel.default_for(instrument)
        venue = SimulatedVenue(instrument, account, cost_model, unconstrained=unconstrained)

        strat.base_timeframe = base
        strat.venue = venue
        strat.advisor = bt.advisor
        strat.sizer = bt.sizer
        strat._view = view

        # Reference feeds (observe-only, not traded).
        # Resolve references first so their indicators contribute to the lead-in.
        ref_resolved = []  # (ref, ref_view, ref_inst, ref_source, ref_base)
        for ref in strat.references:
            ref_source = ref.source or bt.source
            ref_inst = ref_source.get_instrument(Symbol(ref.symbol, ref_source.name))
            ref_tfs = ref.timeframes or subscribed
            ref_base = min(ref_tfs)
            ref_view = MultiTimeframeView(ref_inst, ref_base, sorted(set(ref_tfs) - {ref_base}))
            ref._view = ref_view
            ref_resolved.append((ref, ref_view, ref_inst, ref_source, ref_base))

        ref_tf_indicators: list[dict] = []
        for ref, *_ in ref_resolved:
            rtf: dict = defaultdict(list)
            for ind in ref.indicators:
                rtf[ind.timeframe].append(ind)
            ref_tf_indicators.append(dict(rtf))

        warm_needed: dict[Timeframe, int] = {}
        for ind in strat.registered_indicators:
            warm_needed[ind.timeframe] = max(warm_needed.get(ind.timeframe, 0), ind.lookback)

        tf_to_indicators: dict[Timeframe, list[Indicator]] = defaultdict(list)
        for ind in strat.registered_indicators:
            tf_to_indicators[ind.timeframe].append(ind)

        # Include reference indicator groups so their lookback extends the lead-in.
        groups = [(strat.registered_indicators, base, instrument.session)]
        groups += [(r.indicators, rb, ri.session) for r, _v, ri, _s, rb in ref_resolved]
        lead_start = bt._lead_in_start(start, groups)
        bars = [b for b in bt.source.history(instrument, base, lead_start, end) if b.timestamp <= end]

        # Fetch reference bars from lead_start so indicators can warm up over the lead-in.
        ref_feeds = []
        for _ref, ref_view, ref_inst, ref_source, ref_base in ref_resolved:
            rb = [b for b in ref_source.history(ref_inst, ref_base, lead_start, end) if b.timestamp <= end]
            ref_feeds.append([ref_view, rb, 0])

        return _LegState(
            strategy=strat, venue=venue, view=view,
            warm_needed=warm_needed,
            tf_to_indicators=dict(tf_to_indicators),
            closed_counts={tf: 0 for tf in tf_to_indicators},
            bars=bars, start=start,
            ref_feeds=ref_feeds,
            ref_tf_indicators=ref_tf_indicators,
            ref_closed_counts=[{} for _ in ref_resolved],
        )


def _last_per_day(
    curve: list[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """Resample an equity curve to one point per calendar day (last value wins).

    PortfolioBacktest appends one equity entry per leg bar, so N stocks on the
    same date produce N same-timestamp entries.  Keeping only the last value per
    day restores correct per-period return spacing for Sharpe, VaR, drawdown,
    and every other time-series metric.
    """
    by_day: dict = {}
    for ts, eq in curve:
        by_day[ts.date()] = (ts, eq)
    return list(by_day.values())


def _warm(view: MultiTimeframeView, warm_needed: dict) -> bool:
    for tf, need in warm_needed.items():
        if len(view[tf].bars_for_compute()) < need:
            return False
    return True
