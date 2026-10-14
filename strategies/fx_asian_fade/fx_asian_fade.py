# Parameters: 0  (baseline: every constant below is fixed by the mechanism, not tuned)
#
# Asian-session overshoot fade (idea e411c67d, research/ideas.json).
#
# Hypothesis: in EUR and GBP pairs, the move made during the Asian session is partly
# temporary and reverses once London opens. EUR/GBP home-market dealers are absent in
# Asia, so liquidity is thin and a given order moves price further than it would in
# London hours; when London liquidity arrives, dealers absorb the inventory and the
# temporary component decays. USDJPY (an Asian-home currency) is the natural control.
#
# Baseline (most faithful, simplest): fade every Asian-session move, no size threshold.
#
#   Asian move:   close of the 06:55 London bar  -  open of the 00:00 London bar
#   Entry:        market, decided on the 06:55 bar's close -> fills at the 07:00 open
#   Direction:    opposite to the Asian move
#   Exit:         market at 12:00 London (bar ending 12:00 -> next open), before the
#                 New York open; never crosses the 17:00 NY rollover, so pays no swap
#   Sizing:       notional = 1x equity (EquityFraction(1.0)); leveraged CFD, margin on
#   Costs:        Pepperstone Razor (0.2 pip + $3.50/lot/side, rollover spread schedule)
#
# Symbol override note: --symbol keeps the cost model built for EURUSD, priced in
# 0.00001 ticks -- correct for GBPUSD/EURGBP; JPY pairs need a wrapper whose default
# symbol is the JPY pair (fx_asian_fade_jpy.py).

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hermes import Backtest, EquityFraction, Strategy, Symbol, Timeframe
from hermes.data import PepperstoneSource
from hermes.execution import CostModel

GENERATED_BY = "hermes-strategy"

LONDON = ZoneInfo("Europe/London")
ASIA_OPEN_BAR = time(0, 0)
ASIA_LAST_BAR = time(6, 55)
EXIT_LONDON = time(12, 0)

M5 = Timeframe.parse("5m")


class AsianFade(Strategy):
    def setup(self) -> None:
        self._day: date | None = None
        self._asia_open: float | None = None

    def asian_direction(self, move: float) -> int:
        """Direction of the Asian move to fade (0 = skip). Override for variants."""
        return (move > 0) - (move < 0)

    def on_bar(self, bar) -> None:
        ldn = bar.timestamp.astimezone(LONDON)
        if ldn.date() != self._day:
            self._day = ldn.date()
            self._asia_open = None

        open_trades = self.venue.open_trades()
        if open_trades:
            bar_end = (bar.timestamp + timedelta(seconds=bar.timeframe.seconds)).astimezone(LONDON)
            if bar_end.time() >= EXIT_LONDON or bar_end.date() != self._day:
                for t in open_trades:
                    self.close(t)
            return

        if ldn.weekday() >= 5:
            return
        t = ldn.time()
        if t == ASIA_OPEN_BAR:
            self._asia_open = bar.open
        elif t == ASIA_LAST_BAR and self._asia_open is not None:
            direction = self.asian_direction(bar.close - self._asia_open)
            if direction > 0:
                self.sell(EquityFraction(1.0), tag="fade_up")
            elif direction < 0:
                self.buy(EquityFraction(1.0), tag="fade_down")


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("EURUSD", "pepperstone"))
    source = PepperstoneSource()
    if "cost_model" not in overrides:
        overrides["cost_model"] = CostModel.pepperstone_razor(source.get_instrument(symbol))
    return Backtest(
        strategy=overrides.pop("strategy", AsianFade()),
        source=source,
        symbol=symbol,
        timeframes=[M5],
        # ~3 years of cTrader 5m history (same window as the month-end fix research).
        start=overrides.pop("start", datetime(2023, 10, 1, tzinfo=UTC)),
        end=overrides.pop("end", datetime(2026, 9, 30, 23, 59, tzinfo=UTC)),
        starting_cash=overrides.pop("starting_cash", 100_000),
        **overrides,
    )


if __name__ == "__main__":
    build_backtest().run()
