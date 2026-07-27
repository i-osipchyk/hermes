"""Mean-reversion drop strategy: enter long when a stock falls drop_pct in the
last lookback_days trading days, hold with sl_pct stop and tp_pct target, or
exit at market after max_hold_days if neither level is hit.
"""

from __future__ import annotations

GENERATED_BY = "hermes-strategy"

from hermes import (
    EMA,
    Backtest,
    Parameter,
    RiskPercent,
    Strategy,
    Symbol,
    Timeframe,
)
from hermes.data import YFinanceSource
from hermes.execution import (
    CostModel,
    FinancingModel,
    MakerTakerCommission,
    SlippageModel,
    SpreadModel,
)

D1 = Timeframe.parse("1D")


class DropRecovery(Strategy):
    def setup(self) -> None:
        self.drop_pct = self.param(Parameter(
            "drop_pct", 0.15, bounds=(0.05, 0.50),
            description="Min drop from rolling-window peak to trigger entry",
        ))
        self.lookback_days = self.param(Parameter(
            "lookback_days", 15, bounds=(5, 60),
            description="Rolling window (closed bars) to measure peak",
        ))
        self.sl_pct = self.param(Parameter(
            "sl_pct", 0.05, bounds=(0.01, 0.25),
            description="Stop loss as a fraction below entry close",
        ))
        self.tp_pct = self.param(Parameter(
            "tp_pct", 0.10, bounds=(0.02, 0.50),
            description="Take profit as a fraction above entry close",
        ))
        self.max_hold_days = self.param(Parameter(
            "max_hold_days", 20, bounds=(5, 100),
            description="Trading days before time-based exit at market",
        ))
        self.risk_pct = self.param(Parameter(
            "risk_pct", 0.01, bounds=(0.001, 0.05),
            description="Equity fraction risked per trade (drives position size)",
        ))
        self.cooldown_days = self.param(Parameter(
            "cooldown_days", 20, bounds=(0, 60),
            description="Trading days to skip re-entry after a stop-loss",
        ))
        self.spy_ema_period = self.param(Parameter(
            "spy_ema_period", 200, bounds=(0, 400),
            description="Only enter when SPY is above this daily EMA (0 = filter off)",
        ))

        # Market-regime filter: observe SPY (not traded) and require it above its EMA.
        if self.spy_ema_period > 0:
            self.market = self.use_reference("SPY")
            self.market_ema = self.market.use(EMA(D1, int(self.spy_ema_period)))
        else:
            self.market = None
            self.market_ema = None

        # Per-trade bar counter: entry_time -> bar_count when first observed open.
        self._entry_bars: dict = {}
        self._bar_count = 0
        self._cooldown_until_bar = 0

    def on_bar(self, bar) -> None:
        self._bar_count += 1
        closed = self.data(D1).closed()

        # Register newly opened trades; enforce time-based exit.
        for trade in list(self.venue.open_trades()):
            if trade.entry_time not in self._entry_bars:
                self._entry_bars[trade.entry_time] = self._bar_count
            elif self._bar_count - self._entry_bars[trade.entry_time] >= self.max_hold_days:
                self.close(trade)

        if (len(closed) < self.lookback_days
                or not self.venue.position().is_flat
                or self._bar_count < self._cooldown_until_bar
                or not self._market_ok()):
            return

        # Signal: current close is drop_pct below the rolling-window peak.
        recent_max = max(b.close for b in closed[-self.lookback_days:])
        if (recent_max - bar.close) / recent_max < self.drop_pct:
            return

        # Entry fills at next bar's open; SL/TP anchored to today's close as proxy.
        entry_ref = bar.close
        self.buy(
            RiskPercent(self.risk_pct),
            stop_loss=entry_ref * (1 - self.sl_pct),
            take_profit=entry_ref * (1 + self.tp_pct),
            tag="drop_recovery",
        )

    def _market_ok(self) -> bool:
        """True when SPY is at/above its EMA (or the filter is off)."""
        if self.market_ema is None:
            return True
        ema = self.market.value(self.market_ema)["value"]
        spy = self.market.data(D1).closed()
        if ema is None or not spy:
            return False  # SPY not warm yet — skip the trade
        return spy[-1].close >= ema

    def on_trade_closed(self, trade) -> None:
        self._entry_bars.pop(trade.entry_time, None)
        if trade.exit_reason == "stop_loss":
            # on_trade_closed fires before on_bar for the same bar, so _bar_count is
            # still the previous bar's value; +1 aligns the cooldown to the current bar.
            self._cooldown_until_bar = self._bar_count + self.cooldown_days + 1


def _stock_cost_model() -> CostModel:
    # 0.05% taker (market entries + stop/signal exits); 0% maker (TP exits are resting limits).
    # $0.01 half-spread on taker fills; 1 tick slippage on taker fills.
    return CostModel(
        commission=MakerTakerCommission(maker_rate=0.0, taker_rate=0.0005),
        spread=SpreadModel(points=0.01),
        slippage=SlippageModel(ticks=1),
        financing=FinancingModel(annual_rate=0.0),
    )


def build_backtest(**overrides) -> Backtest:
    bt = Backtest(
        strategy=DropRecovery(),
        source=YFinanceSource(),
        symbol=Symbol("AAPL", "yfinance"),
        timeframes=[D1],
        starting_cash=100_000,
        cost_model=_stock_cost_model(),
    )
    for key, value in overrides.items():
        setattr(bt, key, value)
    return bt


if __name__ == "__main__":
    print(build_backtest().run().metrics)
