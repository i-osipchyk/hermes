"""EMA Crossover with deterministic S1+S2 fundamentals gate (no AI).

Entry: golden cross (fast EMA above slow EMA) that passes BOTH:
  S1 — gross/operating margin expanded ≥ min_margin_expansion_bps YoY
  S2 — revenue re-accelerating OR latest YoY growth ≥ min_revenue_growth_pct
Exit: death cross. No fundamentals check on exit.

If fundamental data is missing (enricher returns None), the trade is blocked
(conservative default — same as AI vetoing at 0.35).

Parameters: 4
"""

from hermes import (
    EMA, Backtest, EquityFraction, Parameter, Strategy, Symbol, Timeframe,
    YFinanceFundamentalsScreen,
)
from hermes.data import YFinanceSource

GENERATED_BY = "hermes-strategy"
D1 = Timeframe.parse("1D")


class EmaCrossoverFundamentals(Strategy):
    def setup(self) -> None:
        short_len = self.param(Parameter("short_ema", 8, bounds=(2, 50)))
        long_len  = self.param(Parameter("long_ema", 32, bounds=(10, 200)))
        self._min_margin_bps = self.param(
            Parameter("min_margin_expansion_bps", 100.0, bounds=(0.0, 500.0),
                      description="Minimum YoY gross/op margin expansion in basis points")
        )
        self._min_rev_growth = self.param(
            Parameter("min_revenue_growth_pct", 10.0, bounds=(0.0, 50.0),
                      description="Minimum YoY revenue growth % for S2 fast-grower condition")
        )
        self.short_ema = self.use(EMA(D1, short_len))
        self.long_ema  = self.use(EMA(D1, long_len))
        self._screen = YFinanceFundamentalsScreen()
        self._prev_short: float | None = None
        self._prev_long:  float | None = None

    def on_start(self) -> None:
        short = self.indicator_value(self.short_ema)["value"]
        long  = self.indicator_value(self.long_ema)["value"]
        if None not in (short, long):
            self._prev_short = short
            self._prev_long  = long

    def on_bar(self, bar) -> None:
        short = self.indicator_value(self.short_ema)["value"]
        long  = self.indicator_value(self.long_ema)["value"]
        prev_short, prev_long = self._prev_short, self._prev_long
        self._prev_short = short
        self._prev_long  = long

        if None in (short, long, prev_short, prev_long):
            return

        golden_cross = prev_short <= prev_long and short > long
        death_cross  = prev_short >= prev_long and short < long
        position = self.venue.position()

        if golden_cross and position.is_flat:
            ticker = self.instrument.symbol.ticker
            as_of  = bar.timestamp.date()
            result = self._screen.screen(
                ticker, as_of,
                min_margin_expansion_bps=self._min_margin_bps,
                min_revenue_growth_pct=self._min_rev_growth,
            )
            if result["s1"] or result["s2"]:
                self.buy(self.sizer or EquityFraction(0.10), tag="ema_x_fund_long")

        elif death_cross and not position.is_flat:
            for trade in self.venue.open_trades():
                self.close(trade)


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("SPY", "yfinance"))
    starting_cash = overrides.pop("starting_cash", 100_000)
    return Backtest(
        strategy=EmaCrossoverFundamentals(),
        source=YFinanceSource(),
        symbol=symbol,
        timeframes=[D1],
        starting_cash=starting_cash,
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
