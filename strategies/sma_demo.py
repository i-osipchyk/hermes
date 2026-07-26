"""Demo strategy for the web UI — a hand-written SMA crossover with a 4h trend filter.

Follows the discovery convention (`build_backtest()` factory) so it appears in
`hermes-ui` and runs with `hermes-backtest`. No `GENERATED_BY` marker — it wasn't
AI-generated, so the UI won't auto-review it (use the button).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hermes import SMA, Backtest, RiskPercent, Strategy, Symbol, Timeframe
from hermes.data import BinanceSource

H1 = Timeframe.parse("1h")
H4 = Timeframe.parse("4h")


class SmaDemo(Strategy):
    def setup(self) -> None:
        self.fast = self.use(SMA(H1, 20))
        self.slow = self.use(SMA(H1, 50))
        self.trend = self.use(SMA(H4, 50))

    def on_bar(self, bar) -> None:
        fast = self.indicator_value(self.fast)["value"]
        slow = self.indicator_value(self.slow)["value"]
        trend = self.indicator_value(self.trend)["value"]
        if None in (fast, slow, trend):
            return
        if fast > slow and bar.close > trend and self.venue.position().is_flat:
            self.buy(RiskPercent(0.01), stop_loss=bar.close * 0.98, take_profit=bar.close * 1.04)
        elif fast < slow and not self.venue.position().is_flat:
            for trade in self.venue.open_trades():
                self.close(trade)


def build_backtest(**overrides) -> Backtest:
    end = overrides.pop("end", datetime.now(UTC))
    start = overrides.pop("start", end - timedelta(days=120))
    bt = Backtest(
        strategy=SmaDemo(),
        source=BinanceSource(),
        symbol=Symbol("BTCUSDT", "binance"),
        timeframes=[H1, H4],
        start=start,
        end=end,
        starting_cash=10_000,
    )
    for key, value in overrides.items():
        setattr(bt, key, value)
    return bt


if __name__ == "__main__":
    print(build_backtest().run().metrics)
