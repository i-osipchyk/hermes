"""EMA Close Crossover strategy with an ATR extension filter (daily, long-only).

Identical edge to ``ema_crossover.py`` — ride the trend while the short EMA stays
above the long EMA — but a golden cross is skipped when the crossover bar is
already extended far above the short EMA relative to recent volatility.

Motivation: on a plain crossover, the entry fills at the open of the bar *after*
the signal (see ``ema_crossover.py``). When the crossover bar itself is an
impulsive/climactic candle, price has often already run well past the short EMA
before the fill even happens, so the entry chases the move near a local top.
That skews the loss distribution — the trades that reverse tend to reverse hard,
since the entry had little room left before mean-reverting back toward the EMA.

Entry:  Same golden-cross detection as ``ema_crossover.py``, plus an extension
        guard: ``(close - short_ema) / ATR`` on the crossover bar must be at or
        below ``max_extension_atr``. If the crossover bar is too extended, the
        signal is skipped outright — no order is placed, no delayed/pullback
        entry is attempted. Fills at the **open of bar N+1**, same as the base
        strategy.

Exit:   Unchanged — death cross closes all open trades, fills at bar M+1 open.
        No extension guard on exits (skewing losses to the left is an entry
        problem; a death cross should always be honoured).

Sizing: Defaults to ``EquityFraction(0.95)`` when no sizer is set on the Backtest.

# Parameters: 3  (≤ 3 recommended; more reduces parameter_adjusted_sharpe)
"""

from __future__ import annotations

from hermes import (
    ATR,
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

D1 = Timeframe.parse("1d")

ATR_PERIOD = 14  # fixed, not a tunable parameter — keeps param count at 3


class EmaCrossoverAtr(Strategy):
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
        self._max_extension_atr = self.param(
            Parameter(
                "max_extension_atr", 2.0, bounds=(0.5, 6.0),
                description=(
                    "Skip a golden cross when (close - short_ema) on the crossover "
                    "bar exceeds this many ATRs — filters out entries chasing an "
                    "already-extended/impulsive move."
                ),
            )
        )
        self.short_ema = self.use(EMA(D1, short_len))
        self.long_ema = self.use(EMA(D1, long_len))
        self.atr = self.use(ATR(D1, ATR_PERIOD))
        # Previous-bar EMA values for crossover detection.
        self._prev_short: float | None = None
        self._prev_long: float | None = None

    def on_start(self) -> None:
        # Seed prev values so bar 2 of the trading window can already detect a
        # crossover. on_start fires on the SAME bar as the first on_bar, so the
        # seeded values equal that bar's own -- bar 1 can never signal a cross,
        # which is the safe behaviour (we have no confirmed prior-bar reading).
        short = self.indicator_value(self.short_ema)["value"]
        long  = self.indicator_value(self.long_ema)["value"]
        if None in (short, long):
            return
        self._prev_short = short
        self._prev_long  = long

    def on_bar(self, bar) -> None:
        short = self.indicator_value(self.short_ema)["value"]
        long = self.indicator_value(self.long_ema)["value"]
        atr = self.indicator_value(self.atr)["value"]

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
            if atr is not None and atr > 0:
                extension = (bar.close - short) / atr
                if extension > self._max_extension_atr:
                    return
            # Strategy._order already prefers a Backtest-level sizer when one is set.
            self.buy(EquityFraction(0.95), tag="ema_x_long")

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
        strategy=EmaCrossoverAtr(),
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
