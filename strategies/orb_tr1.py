# Parameters: 3  (<= 3 recommended; more reduces parameter_adjusted_sharpe)
#
# Symmetric opening-range breakout, sized off the PRIOR session's true range
# (source: a LinkedIn post backtesting this on MNQ futures 2018-2026, 52% CAGR /
# 1.40 Sharpe vs. QQQ buy-and-hold's 19% CAGR / 0.84 Sharpe).
#
# TR1 = true range of the previous full session (High-Low, |High-PrevClose|,
# |Low-PrevClose| on the daily bar). Both a long-stop and a short-stop rest
# simultaneously at the session open +/- 0.25*TR1 from 09:30-13:00 ET:
#
#   Entry:  stop order at Open +/- 0.25*TR1                  (1 = long, 13:00 cutoff)
#   Stop:   0.25*TR1 from entry = 1R
#   Break-even: stop moves to entry once a completed bar closes/extends
#               +0.50*TR1 (= +2R) in the trade's favor, effective the next bar
#   Exit:   stop, break-even, or a market exit ~15:55 ET (no target, no trailing)
#   Re-entry: after a stop-out, the SAME side re-arms only once price trades
#             back through its entry level from the other side (max 3 fills/day
#             combined across both sides)
#   Sizing: risk_pct of current equity per entry, distance = entry -> stop (1R)
#
# Data-source note: the source idea trades CME MNQ futures with $0.95 round-trip
# commission + 1-tick slippage per fill. Hermes has no CME futures DataSource, so
# this runs on QQQ (the post's own benchmark, via YFinanceSource) by default --
# close in spirit, not identical. Pass use_pepperstone=True to trade the NAS100
# CFD via PepperstoneSource instead: a closer proxy for the index itself (and
# leveraged/shortable like the original futures), at the cost of needing
# CTRADER_* credentials.
#
# Cost note (Pepperstone path): index CFDs on Pepperstone carry zero commission
# -- the spread is the cost, plus overnight financing on positions held past a
# session (moot here; the strategy force-closes every day at 15:55 ET). Average
# spreads confirmed via pepperstone.com/en/markets/indices/index-fees: US500
# ~0.4 points, NAS100 ~1.0 point. CostModel.default_for()'s generic CFD spread
# (2x tick_size) is tuned for FX pairs and understates this badly, so the
# Pepperstone path below builds an explicit per-symbol CostModel instead.
#
# Margin note: "risk 1% of equity to a 0.25*TR1 stop" sizes for a leveraged
# instrument, where the margin required is a small fraction of notional. QQQ is
# unleveraged cash stock in Hermes, so the same risk-based share count is
# frequently unaffordable outright (e.g. a tight stop can size a >2x-equity
# position) -- the QQQ path runs with unconstrained=True to keep the source
# strategy's true risk-per-trade sizing intact. The NAS100 CFD path is
# leveraged (PepperstoneSource default 30x) so margin checks stay on.
#
# Data-depth note: yfinance only serves ~60 days of 5m history (hence the
# ~55-day default window there); cTrader/Pepperstone serves much deeper
# intraday history directly from the broker, so the CFD path defaults to a
# multi-year window at the same 5m granularity.

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from hermes import (
    Backtest,
    OrderType,
    Parameter,
    RiskPercent,
    Side,
    Strategy,
    Symbol,
    Timeframe,
)
from hermes.data import PepperstoneSource, YFinanceSource
from hermes.execution import (
    CostModel,
    FinancingModel,
    OrderStatus,
    PerLotCommission,
    SlippageModel,
    SpreadModel,
)

GENERATED_BY = "hermes-strategy"

D1 = Timeframe.parse("1d")

_ENTRY_START = time(9, 30)
_ENTRY_END = time(13, 0)
_EOD_TIME = time(15, 55)

_YFINANCE_LOOKBACK_DAYS = 55     # yfinance's ~60-day 5m history window
_PEPPERSTONE_LOOKBACK_DAYS = 1095  # ~3 years; confirmed available for NAS100 5m via cTrader

# Average Pepperstone spread in index points (pepperstone.com/en/markets/indices/index-fees).
# Index CFDs carry no commission there -- the spread is the entire trading cost.
_PEPPERSTONE_AVG_SPREAD_POINTS = {"US500": 0.4, "NAS100": 1.0}


def _pepperstone_cost_model(ticker: str) -> CostModel:
    return CostModel(
        commission=PerLotCommission(0.0),
        spread=SpreadModel(points=_PEPPERSTONE_AVG_SPREAD_POINTS.get(ticker, 0.4)),
        slippage=SlippageModel(ticks=1.0),
        financing=FinancingModel(annual_rate=0.005),
    )


class OpeningRangeBreakoutTR1(Strategy):
    def setup(self) -> None:
        self._tr1_mult = self.param(
            Parameter(
                "tr1_mult", 0.25, bounds=(0.1, 0.5),
                description=(
                    "Fraction of the prior session's true range used for both the "
                    "entry offset and the stop distance (1R)"
                ),
            )
        )
        self._risk_pct = self.param(
            Parameter(
                "risk_pct", 0.01, bounds=(0.0025, 0.02),
                description="Equity fraction risked per entry (entry-to-stop distance = 1R)",
            )
        )
        self._max_entries = self.param(
            Parameter(
                "max_entries", 3, bounds=(1, 5),
                description="Max combined long+short fills per day",
            )
        )

        self._day = None
        self._session_open: float | None = None
        self._tr1: float | None = None
        self._r: float = 0.0
        self._long_level: float | None = None
        self._short_level: float | None = None
        self._entries_today = 0
        self._long_order = None
        self._short_order = None
        self._long_needs_retest = False
        self._short_needs_retest = False
        self._be_applied: set = set()

    def on_bar(self, bar) -> None:
        # ET clock time drives the schedule directly, rather than the
        # Instrument's own session bounds -- a stock session is already
        # 09:30-16:00 ET so this is a no-op there, but a CFD/futures proxy
        # trades ~24/5 with no session close, so the 09:30/13:00/15:55 ET
        # markers below are the only thing defining "the trading day" for it.
        local = self.instrument.session.to_local(bar.timestamp)
        day = local.date()
        if day != self._day:
            self._start_new_day(day)

        self._sync_fills()

        if self._is_eod(local):
            self._cancel_working()
            self.venue.force_close_all(bar.timestamp)  # ~15:55 ET market exit
            return

        t = local.time()
        if self._session_open is None and t >= _ENTRY_START:
            self._arm_levels(bar)

        if self._tr1 is None:
            return  # not enough daily history yet, or window hasn't opened today

        for trade in self.venue.open_trades():
            self._maybe_breakeven(trade, bar)

        if _ENTRY_START <= t < _ENTRY_END:
            self._maybe_arm("long", bar)
            self._maybe_arm("short", bar)
        elif t >= _ENTRY_END:
            self._cancel_working()

    def on_trade_closed(self, trade) -> None:
        if trade.exit_reason != "stop_loss":
            return
        if trade.side is Side.BUY:
            self._long_needs_retest = True
        else:
            self._short_needs_retest = True

    # --- internals ---------------------------------------------------------

    def _start_new_day(self, day) -> None:
        self._day = day
        self._entries_today = 0
        self._long_needs_retest = False
        self._short_needs_retest = False
        self._session_open = None
        self._tr1 = None
        self._be_applied.clear()
        self._cancel_working()

    def _arm_levels(self, bar) -> None:
        """Capture today's 09:30 ET open and the prior-session TR1 -- runs once,
        on the first bar at/after 09:30 ET (not on day-change), since a
        continuously-traded CFD has bars well before the entry window opens."""
        self._session_open = bar.open
        daily = self.data(D1).closed()
        if len(daily) < 2:
            return  # self._tr1 stays None for today

        prev, prev2 = daily[-1], daily[-2]
        self._tr1 = max(
            prev.high - prev.low,
            abs(prev.high - prev2.close),
            abs(prev.low - prev2.close),
        )
        self._r = self._tr1_mult * self._tr1
        self._long_level = self._session_open + self._r
        self._short_level = self._session_open - self._r

    def _is_eod(self, local) -> bool:
        bucket_end = local + timedelta(seconds=self.base_timeframe.seconds)
        return bucket_end.date() != local.date() or bucket_end.time() >= _EOD_TIME

    def _sync_fills(self) -> None:
        for attr in ("_long_order", "_short_order"):
            order = getattr(self, attr)
            if order is None:
                continue
            if order.status == OrderStatus.FILLED:
                self._entries_today += 1
                setattr(self, attr, None)
            elif order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                setattr(self, attr, None)

    def _cancel_working(self) -> None:
        for attr in ("_long_order", "_short_order"):
            order = getattr(self, attr)
            if order is not None and order.status == OrderStatus.WORKING:
                self.venue.cancel(order)
            setattr(self, attr, None)


    def _maybe_breakeven(self, trade, bar) -> None:
        if trade in self._be_applied:
            return
        # Prune closed trades so the set tracks only live ones (it is reset each
        # day anyway, but a long session should not accumulate dead references).
        self._be_applied.intersection_update(self.venue.open_trades())
        target = 2 * self._r  # +0.50*TR1 = +2R
        if trade.side is Side.BUY:
            hit = bar.high - trade.entry_price >= target
        else:
            hit = trade.entry_price - bar.low >= target
        if hit:
            self.modify(trade, stop_loss=trade.entry_price)
            self._be_applied.add(trade)

    def _maybe_arm(self, side: str, bar) -> None:
        order_attr = f"_{side}_order"
        if getattr(self, order_attr) is not None:
            return
        if self._entries_today >= int(self._max_entries):
            return

        is_long = side == "long"
        side_enum = Side.BUY if is_long else Side.SELL
        if any(t.side is side_enum for t in self.venue.open_trades()):
            return

        retest_attr = f"_{side}_needs_retest"
        level = self._long_level if is_long else self._short_level
        if getattr(self, retest_attr):
            retested = bar.low <= level if is_long else bar.high >= level
            if not retested:
                return
            setattr(self, retest_attr, False)

        # Skip when the risk budget cannot buy one native unit (a share, or one
        # volume step of a lot-sized CFD). Sizing itself is done by RiskPercent
        # below; this only avoids submitting an order that would floor to zero.
        if self._r <= 0:
            return
        risk_cash = self.venue.equity() * self._risk_pct
        per_unit_risk = self._r * self.instrument.contract_size()
        if per_unit_risk <= 0:
            return
        if self.instrument.to_native_units(risk_cash / per_unit_risk) <= 0:
            return

        stop_loss = level - self._r if is_long else level + self._r
        submit = self.buy if is_long else self.sell
        order = submit(
            RiskPercent(self._risk_pct),
            type=OrderType.STOP,
            stop=level,
            stop_loss=stop_loss,
            tag=f"orb_{side}",
        )
        setattr(self, order_attr, order)


def _default_window(use_pepperstone: bool, base_tf: Timeframe) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    if use_pepperstone:
        days = _PEPPERSTONE_LOOKBACK_DAYS
    else:
        # yfinance depth: ~60 days for sub-hourly bars, ~2 years for 1h bars.
        days = 720 if base_tf.seconds >= 3600 else _YFINANCE_LOOKBACK_DAYS
    return end - timedelta(days=days), end


def build_backtest(**overrides) -> Backtest:
    """Factory used by hermes-backtest and the web UI.

    Defaults to QQQ via yfinance (no credentials needed, ~55-day 5m window).
    Pass ``use_pepperstone=True`` to trade the NAS100 CFD via cTrader instead
    (needs CTRADER_* credentials; multi-year 5m window, real leverage/margin).
    Pass ``base_timeframe="1h"`` on the yfinance path for a longer but coarser
    (~2y) run instead of the default 5m/~55-day window.
    """
    use_pepperstone = overrides.pop("use_pepperstone", False)
    default_symbol = Symbol("NAS100", "pepperstone") if use_pepperstone else Symbol("QQQ", "yfinance")
    symbol = overrides.pop("symbol", default_symbol)
    starting_cash = overrides.pop("starting_cash", 100_000)
    default_base_tf = "5m"
    base_tf = Timeframe.parse(overrides.pop("base_timeframe", default_base_tf))
    default_start, default_end = _default_window(use_pepperstone, base_tf)
    start = overrides.pop("start", default_start)
    end = overrides.pop("end", default_end)
    if use_pepperstone:
        source = PepperstoneSource(leverage=overrides.pop("leverage", 30.0))
        unconstrained = False  # the CFD is leveraged, so real margin checks apply
        overrides.setdefault("cost_model", _pepperstone_cost_model(symbol.ticker))
    else:
        source = YFinanceSource(shortable=True)
        unconstrained = True  # see the margin note above -- QQQ cash shares can't
    return Backtest(
        strategy=OpeningRangeBreakoutTR1(),
        source=source,
        symbol=symbol,
        timeframes=[base_tf, D1],
        start=start,
        end=end,
        starting_cash=starting_cash,
        unconstrained=unconstrained,
        **overrides,
    )


if __name__ == "__main__":
    result = build_backtest().run()
    m = result.metrics
    print(
        f"trades={m.num_trades}  return={m.total_return:.2%}  "
        f"sharpe={m.sharpe:.2f}  adj_sharpe={m.parameter_adjusted_sharpe:.2f}  "
        f"max_dd={m.max_drawdown:.2%}  win_rate={m.win_rate:.2%}"
    )
