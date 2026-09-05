"""PortfolioBacktest: one shared capital pool across multiple instruments.

Each *leg* is a pre-configured :class:`Backtest` (one Strategy, one instrument).
All legs share a single :class:`~hermes.execution.Account`; the simulation clock is
synchronised so bar-by-bar margin is competed for in real time — a win on one symbol
grows the capital available to the next trade on any other.

**Limitation:** :class:`~hermes.execution.SimulatedVenue`'s margin check uses only its
own leg's ``used_margin()``. The shared ``account.cash`` is always correct (every fill
credits/debits it), but cross-leg reserved margin is not summed during the
``_can_afford`` gate. For unleveraged or lightly-leveraged strategies this is immaterial;
for highly-leveraged CFD books use with care.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..core import Symbol, Timeframe
from ..data import MultiTimeframeView
from ..execution import Account, CostModel, SimulatedVenue
from .engine import Backtest, _as_utc
from .result import BacktestResult


@dataclass
class _LegState:
    """Wired, ready-to-clock state for one portfolio leg."""

    strategy: object          # Strategy instance
    venue: SimulatedVenue
    view: MultiTimeframeView
    warm_needed: dict[Timeframe, int]
    bars: list                # pre-fetched base bars (lead-in + trading window)
    start: datetime           # trading-window start (bars before this are Lead-in)
    ref_feeds: list           # [[ref_view, bars, pointer], ...]
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

    def run(self) -> PortfolioResult:
        if not self.legs:
            raise ValueError("PortfolioBacktest requires at least one leg.")

        account = Account(self.starting_cash)
        leg_states: list[_LegState] = [self._wire(leg, account) for leg in self.legs]

        # Unified sorted event stream: (timestamp, leg_index, bar).
        # Ties broken by leg order so deterministic.
        events: list[tuple[datetime, int, object]] = []
        for i, ls in enumerate(leg_states):
            for bar in ls.bars:
                events.append((bar.timestamp, i, bar))
        events.sort(key=lambda e: (e[0], e[1]))

        equity_curve: list[tuple[datetime, float]] = []
        prev_closed_counts = [0] * len(leg_states)

        for ts, idx, bar in events:
            ls = leg_states[idx]

            # Advance reference feeds for this leg up to now (no look-ahead).
            for feed in ls.ref_feeds:
                rv, rb, ptr = feed
                while ptr < len(rb) and rb[ptr].timestamp <= ts:
                    rv.push(rb[ptr])
                    ptr += 1
                feed[2] = ptr

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
                ls.strategy.on_start()

            ls.strategy._current_bar = bar
            ls.strategy.on_bar(bar)

            # Portfolio equity = shared cash + unrealised PnL across ALL venues.
            total_unrealised = sum(lx.venue.unrealised_pnl() for lx in leg_states)
            equity_curve.append((ts, account.cash + total_unrealised))

        for ls in leg_states:
            ls.strategy.on_stop()

        # Gather trades per symbol and build the combined BacktestResult.
        per_symbol: dict[str, list] = {}
        for leg, ls in zip(self.legs, leg_states):
            per_symbol[str(leg.symbol)] = ls.venue.closed_trades

        all_trades = [t for trades in per_symbol.values() for t in trades]
        num_params = max(
            (len(ls.strategy.declared_parameters()) for ls in leg_states),
            default=0,
        )
        result = BacktestResult.compute(equity_curve, all_trades, num_params=num_params)
        return PortfolioResult(result=result, per_symbol=per_symbol)

    # --- internal wiring -------------------------------------------------------

    def _wire(self, bt: Backtest, account: Account) -> _LegState:
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
        venue = SimulatedVenue(instrument, account, cost_model)

        strat.base_timeframe = base
        strat.venue = venue
        strat.advisor = bt.advisor
        strat._view = view

        # Reference feeds (observe-only, not traded).
        ref_feeds = []
        for ref in strat.references:
            ref_source = ref.source or bt.source
            ref_inst = ref_source.get_instrument(Symbol(ref.symbol, ref_source.name))
            ref_tfs = ref.timeframes or subscribed
            ref_base = min(ref_tfs)
            ref_view = MultiTimeframeView(ref_inst, ref_base, sorted(set(ref_tfs) - {ref_base}))
            ref._view = ref_view
            rb = [b for b in ref_source.history(ref_inst, ref_base, start, end) if b.timestamp <= end]
            ref_feeds.append([ref_view, rb, 0])

        warm_needed: dict[Timeframe, int] = {}
        for ind in strat.registered_indicators:
            warm_needed[ind.timeframe] = max(warm_needed.get(ind.timeframe, 0), ind.lookback)

        groups = [(strat.registered_indicators, base, instrument.session)]
        lead_start = bt._lead_in_start(start, groups)
        bars = [b for b in bt.source.history(instrument, base, lead_start, end) if b.timestamp <= end]

        return _LegState(
            strategy=strat, venue=venue, view=view,
            warm_needed=warm_needed, bars=bars, start=start,
            ref_feeds=ref_feeds,
        )


def _warm(view: MultiTimeframeView, warm_needed: dict) -> bool:
    for tf, need in warm_needed.items():
        if len(view[tf].bars_for_compute()) < need:
            return False
    return True
