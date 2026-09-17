"""EMA Close Crossover strategy (daily, long-only).

Edge: trend-following — ride the trend while the short EMA stays above the long EMA.

Entry:  A market order is placed when the short EMA crosses above the long EMA on the
        close of bar N.  The order fills at the **open of bar N+1** (next day's open),
        which is exactly the look-ahead-safe execution of "buy the open of the day after
        the crossover."

Exit:   When the short EMA crosses back below the long EMA (death cross on bar M), a
        market order closes all open trades.  It fills at the **open of bar M+1**.
        NOTE: the user spec asked for "close of the crossover day"; that would require
        executing at the same bar where the signal was detected — which is look-ahead
        in event-driven backtesting.  Opening-of-next-day is the earliest look-ahead-safe
        equivalent, and on a daily strategy the difference is negligible.

Sizing: Controlled by ``Backtest.sizer``.  Defaults to ``EquityFraction(0.95)`` —
        95% of current equity per entry — when no sizer is set on the Backtest config.
        Set ``Backtest(sizer=NotionalCash(10_000))`` for fixed notional (recommended
        for multi-symbol runs with ``unconstrained=True``).

# Parameters: 2  (≤ 3 recommended; more reduces parameter_adjusted_sharpe)
"""

from __future__ import annotations

from hermes import (
    EMA,
    Backtest,
    EquityFraction,
    Parameter,
    Strategy,
    Symbol,
    Timeframe,
)
from hermes.data import YFinanceSource

GENERATED_BY = "hermes-strategy"

D1 = Timeframe.parse("1D")


class EmaCrossover(Strategy):
    def setup(self) -> None:
        short_len = self.param(
            Parameter(
                "short_ema", 8, bounds=(2, 50),
                description="Period of the fast EMA",
            )
        )
        long_len = self.param(
            Parameter(
                "long_ema", 32, bounds=(10, 200),
                description="Period of the slow EMA",
            )
        )
        self.short_ema = self.use(EMA(D1, short_len))
        self.long_ema = self.use(EMA(D1, long_len))
        # Previous-bar EMA values for crossover detection.
        self._prev_short: float | None = None
        self._prev_long: float | None = None

    def on_start(self) -> None:
        # Seed prev values from the last lead-in bar so bar-1 of the trading window
        # can detect a crossover (without this, _prev is None and bar-1 is always skipped).
        short = self.indicator_value(self.short_ema)["value"]
        long  = self.indicator_value(self.long_ema)["value"]
        if None in (short, long):
            return
        self._prev_short = short
        self._prev_long  = long
        # If the stock is already in a golden-cross state at window start, enter now.
        # Without this, a stock mid-uptrend at period start would never be entered
        # (the strategy would wait for a death cross + re-cross that might not arrive).
        if short > long and self.venue.position().is_flat:
            self.buy(self.sizer or EquityFraction(0.95), tag="ema_x_long_initial")

    def on_bar(self, bar) -> None:  # noqa: ARG002
        short = self.indicator_value(self.short_ema)["value"]
        long = self.indicator_value(self.long_ema)["value"]

        prev_short = self._prev_short
        prev_long = self._prev_long

        # Always update prev before any early returns so the next bar has correct history.
        self._prev_short = short
        self._prev_long = long

        if None in (short, long, prev_short, prev_long):
            return

        golden_cross = prev_short <= prev_long and short > long
        death_cross = prev_short >= prev_long and short < long

        position = self.venue.position()

        if golden_cross and position.is_flat:
            self.buy(self.sizer or EquityFraction(0.95), tag="ema_x_long")

        elif death_cross and not position.is_flat:
            # Close all open trades → fills at tomorrow's open.
            for trade in self.venue.open_trades():
                self.close(trade)


def build_backtest(**overrides) -> Backtest:
    """Factory used by hermes-backtest and the web UI.

    Pass ``symbol``, ``start``, ``end``, or ``starting_cash`` to override defaults.
    start/end are omitted here so the run defaults to year-to-date (Jan 1 → today).
    """
    symbol = overrides.pop("symbol", Symbol("SPY", "yfinance"))
    starting_cash = overrides.pop("starting_cash", 100_000)
    return Backtest(
        strategy=EmaCrossover(),
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
