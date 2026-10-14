# Parameters: 0  (baseline: every constant below is fixed by the mechanism, not tuned)
#
# Sunday-open gap fill (idea 116ee823, research/ideas.json).
#
# Hypothesis: in major FX pairs the gap between Friday's close and the price once Sunday
# liquidity has returned is partly temporary and fills during the next session. The week
# opens (17:00 New York, Sunday) with few dealers quoting and Asian liquidity not yet in,
# so the first prints overshoot the weekend's news; as the book refills the overshoot
# decays. Weekend news that is real (elections, central-bank surprises) does not fill --
# expect a fat left tail.
#
# Baseline (most faithful, simplest): fade every residual gap, no size threshold, no stop.
#
#   Friday close: close of the last bar before the weekend break
#   Gap:          close of the first Sunday bar ending >= 18:00 NY  -  Friday close
#                 (entry waits out the x10 Sunday-open spread window, 17:00-18:00 NY;
#                 the gap measured is the one still open when it can be traded)
#   Entry:        market, decided on that bar's close -> fills at the next open
#   Direction:    opposite to the gap
#   Take profit:  Friday's close (the gap is filled)
#   Time exit:    Monday 16:50 NY (bar ending 16:45 -> fills 16:50 open), before the
#                 16:55-17:05 rollover spread window; never crosses 17:00, so pays no swap
#   Sizing:       notional = 1x equity (EquityFraction(1.0)); leveraged CFD, margin on
#   Costs:        Pepperstone Razor (0.2 pip + $3.50/lot/side, rollover spread schedule)

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hermes import Backtest, EquityFraction, Strategy, Symbol, Timeframe
from hermes.data import PepperstoneSource
from hermes.execution import CostModel

GENERATED_BY = "hermes-strategy"

NEW_YORK = ZoneInfo("America/New_York")
SUNDAY = 6
ENTRY_BAR_END = time(18, 0)      # NY: end of the x10 Sunday-open spread window
EXIT_BAR_END = time(16, 45)      # NY, next day: fill at 16:50, before the rollover window
WEEKEND_BREAK = timedelta(hours=36)

M5 = Timeframe.parse("5m")


class SundayGapFade(Strategy):
    entry_bar_end = ENTRY_BAR_END
    exit_bar_end = EXIT_BAR_END

    def setup(self) -> None:
        self._prev_ts: datetime | None = None
        self._prev_close: float | None = None
        self._friday_close: float | None = None
        self._exit_at: datetime | None = None

    def gap_direction(self, gap: float, bar) -> int:
        """Direction of the gap to fade (0 = skip). Override for variants."""
        return (gap > 0) - (gap < 0)

    def on_bar(self, bar) -> None:
        if self._prev_ts is not None and bar.timestamp - self._prev_ts > WEEKEND_BREAK:
            self._friday_close = self._prev_close
        self._prev_ts, self._prev_close = bar.timestamp, bar.close

        bar_end = (bar.timestamp + timedelta(seconds=bar.timeframe.seconds)).astimezone(NEW_YORK)
        open_trades = self.venue.open_trades()
        if open_trades:
            if bar_end >= self._exit_at:
                for t in open_trades:
                    self.close(t)
            return

        if self._friday_close is None or bar_end.weekday() != SUNDAY:
            return
        if bar_end.time() < self.entry_bar_end:
            return
        target, self._friday_close = self._friday_close, None   # one decision per week
        direction = self.gap_direction(bar.close - target, bar)
        if direction == 0:
            return
        self._exit_at = datetime.combine(bar_end.date() + timedelta(days=1), self.exit_bar_end, NEW_YORK)
        if direction > 0:
            self.sell(EquityFraction(1.0), take_profit=target, tag="fade_gap_up")
        else:
            self.buy(EquityFraction(1.0), take_profit=target, tag="fade_gap_down")


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("EURUSD", "pepperstone"))
    source = PepperstoneSource()
    if "cost_model" not in overrides:
        overrides["cost_model"] = CostModel.pepperstone_razor(source.get_instrument(symbol))
    return Backtest(
        strategy=overrides.pop("strategy", SundayGapFade()),
        source=source,
        symbol=symbol,
        timeframes=overrides.pop("timeframes", [M5]),
        # ~3 years of cTrader 5m history (same window as the other FX mean-reversion ideas).
        start=overrides.pop("start", datetime(2023, 10, 1, tzinfo=UTC)),
        end=overrides.pop("end", datetime(2026, 9, 30, 23, 59, tzinfo=UTC)),
        starting_cash=overrides.pop("starting_cash", 100_000),
        **overrides,
    )


if __name__ == "__main__":
    build_backtest().run()
