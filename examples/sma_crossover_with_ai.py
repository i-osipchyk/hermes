"""Example: an SMA-crossover swing strategy on one Instrument, with a higher-
timeframe trend filter, risk-based sizing, attached Stop Loss / Take Profit, and an
optional AI confirmation gate.

Runs out of the box against Binance (public API, no credentials). Enable the AI gate
by setting ANTHROPIC_API_KEY and passing the advisor (see bottom).

    python examples/sma_crossover_with_ai.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hermes import (
    SMA,
    Backtest,
    Parameter,
    RiskPercent,
    Strategy,
    Symbol,
    Timeframe,
)
from hermes.data import BinanceSource

H1 = Timeframe.parse("1h")
H4 = Timeframe.parse("4h")


class SmaCrossover(Strategy):
    def setup(self) -> None:
        fast_len = self.param(Parameter("fast_len", 20, bounds=(5, 50)))
        slow_len = self.param(Parameter("slow_len", 50, bounds=(20, 200)))
        self.fast = self.use(SMA(H1, fast_len))
        self.slow = self.use(SMA(H1, slow_len))
        self.trend = self.use(SMA(H4, 50))  # higher-TF trend filter (Forming-Bar aware)

    def on_bar(self, bar) -> None:
        fast = self.indicator_value(self.fast)["value"]
        slow = self.indicator_value(self.slow)["value"]
        trend = self.indicator_value(self.trend)["value"]
        if None in (fast, slow, trend):
            return

        long_setup = fast > slow and bar.close > trend
        if long_setup and self.venue.position().is_flat:
            entry = bar.close
            order = self.buy(
                RiskPercent(0.01),               # risk 1% of equity to the stop
                stop_loss=entry * 0.98,
                take_profit=entry * 1.04,
                tag="sma_x_long",
            )
            # Optional AI gate — no-op unless an advisor is configured on the Backtest.
            if not self.confirm_with_ai(order, "Confirm this long swing setup."):
                self.venue.cancel(order)

        elif not self.venue.position().is_flat and fast < slow:
            for trade in self.venue.open_trades():
                self.close(trade)


if __name__ == "__main__":
    end = datetime.now(UTC)
    start = end - timedelta(days=120)
    bt = Backtest(
        strategy=SmaCrossover(),
        source=BinanceSource(),
        symbol=Symbol("BTCUSDT", "binance"),
        timeframes=[H1, H4],
        start=start,
        end=end,
        starting_cash=10_000,
        # advisor=AIAdvisor(ClaudeProvider()),  # uncomment + set ANTHROPIC_API_KEY
    )
    result = bt.run()
    m = result.metrics
    print(f"trades={m.num_trades} return={m.total_return} sharpe={m.sharpe} "
          f"max_dd={m.max_drawdown} win_rate={m.win_rate}")
