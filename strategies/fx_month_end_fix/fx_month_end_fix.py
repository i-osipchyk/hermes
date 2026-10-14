# Parameters: 0  (baseline: every constant below is fixed by the mechanism, not tuned)
#
# Month-end London-fix reversal (idea 5cf533fe, research/ideas.json).
#
# Hypothesis: on the last trading day of the month, a USD pair trends into the 16:00
# London WM/R fix and partially reverses afterwards. Foreign holders of US equities
# rebalance their FX hedges at the month-end fix (Melvin & Prins 2015); that flow is
# forced, price-insensitive and concentrated in the fix window, so it pushes price away
# from fair value and the push decays once the flow is gone.
#
# Baseline (most faithful, simplest): fade the pre-fix move, no equity signal, no filter.
#
#   Pre-fix move:  close of the 15:55 London bar  -  open of the 15:00 London bar
#   Entry:         market, decided on the 16:00 London bar's close -> fills at 16:05,
#                  i.e. after the 15:57:30-16:02:30 fix window has finished
#   Direction:     opposite to the pre-fix move
#   Exit:          market at 16:30 New York, before the 16:55-17:05 rollover spread
#                  window and the 17:00 financing rollover (the trade never pays swap)
#   Sizing:        notional = 1x equity (EquityFraction(1.0)); leveraged CFD, margin on
#   Costs:         Pepperstone Razor (0.2 pip + $3.50/lot/side, rollover spread schedule)
#
# "Last trading day" = the last Monday-Friday of the calendar month; the calendar is
# known in advance, so this uses no future data. FX fixes run on most bank holidays.
#
# Symbol override note: the CLI's --symbol keeps the cost model built here, which is
# priced in ticks of the default symbol. Pairs sharing EURUSD's 0.00001 tick (GBPUSD,
# AUDUSD, ...) are priced correctly; JPY pairs need a wrapper whose default symbol is
# the JPY pair (see fx_month_end_fix_1h_jpy.py).

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hermes import Backtest, EquityFraction, Strategy, Symbol, Timeframe
from hermes.data import PepperstoneSource
from hermes.execution import CostModel

GENERATED_BY = "hermes-strategy"

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")

PRE_FIX_START = time(15, 0)   # London: start of the pre-fix window
LAST_PRE_FIX_BAR = time(15, 55)
DECISION_BAR = time(16, 0)    # London: its close (16:05) is after the fix window
EXIT_BAR_END = time(16, 30)   # New York: flat before the rollover

M5 = Timeframe.parse("5m")

# Pepperstone's FX feed stops early on Dec 24, Dec 31 (15:00 NY) and Good Friday (16:00 NY),
# before EXIT_BAR_END, so the 16:30 exit never fired and the trade was held to the next open.
# Measured from the cached EURUSD 5m bars. The exit fills at the NEXT open, so the decision
# bar must end one 5m bar before the close. The holiday calendar is public, not future price.
EARLY_CLOSE_NY = {"christmas_eve": time(15, 0), "new_year_eve": time(15, 0), "good_friday": time(16, 0)}
_FILL_LEAD = timedelta(minutes=10)


def _easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def exit_bar_end_ny(d: date) -> time:
    """Earliest bar end (New York) that triggers the exit on day ``d``."""
    close = None
    if (d.month, d.day) == (12, 24):
        close = EARLY_CLOSE_NY["christmas_eve"]
    elif (d.month, d.day) == (12, 31):
        close = EARLY_CLOSE_NY["new_year_eve"]
    elif d == _easter(d.year) - timedelta(days=2):
        close = EARLY_CLOSE_NY["good_friday"]
    if close is None:
        return EXIT_BAR_END
    early = (datetime.combine(d, close) - _FILL_LEAD).time()
    return min(EXIT_BAR_END, early)


def is_last_weekday_of_month(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.month != d.month


class MonthEndFixFade(Strategy):
    def setup(self) -> None:
        self._day: date | None = None
        self._pre_open: float | None = None
        self._pre_close: float | None = None
        self._entered = False

    def pre_fix_direction(self, bar) -> int:
        """+1 if the pre-fix move was up, -1 if down, 0 if flat. Override for variants."""
        move = self._pre_close - self._pre_open
        return (move > 0) - (move < 0)

    def exit_due(self, bar) -> bool:
        """True on the bar whose close is the exit time; the fill is the next open."""
        bar_end = bar.timestamp + timedelta(seconds=bar.timeframe.seconds)
        bar_end_ny = bar_end.astimezone(NEW_YORK)
        return bar_end_ny.time() >= exit_bar_end_ny(bar_end_ny.date()) or bar_end_ny.date() != self._day

    def on_bar(self, bar) -> None:
        ldn = bar.timestamp.astimezone(LONDON)
        if ldn.date() != self._day:
            self._day = ldn.date()
            self._pre_open = self._pre_close = None
            self._entered = False

        open_trades = self.venue.open_trades()
        if open_trades:
            if self.exit_due(bar):
                for t in open_trades:
                    self.close(t)
            return

        if not is_last_weekday_of_month(ldn.date()) or self._entered:
            return

        t = ldn.time()
        if t == PRE_FIX_START:
            self._pre_open = bar.open
        elif t == LAST_PRE_FIX_BAR:
            self._pre_close = bar.close
        elif t == DECISION_BAR and self._pre_open is not None and self._pre_close is not None:
            self._entered = True
            direction = self.pre_fix_direction(bar)
            if direction > 0:
                self.sell(EquityFraction(1.0), tag="fade_up")
            elif direction < 0:
                self.buy(EquityFraction(1.0), tag="fade_down")


def _window() -> tuple[datetime, datetime]:
    # ~3 years of cTrader 5m history; whole months so every month-end is included.
    return datetime(2023, 10, 1, tzinfo=UTC), datetime(2026, 9, 30, 23, 59, tzinfo=UTC)


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("EURUSD", "pepperstone"))
    source = PepperstoneSource()
    start, end = _window()
    if "cost_model" not in overrides:
        overrides["cost_model"] = CostModel.pepperstone_razor(source.get_instrument(symbol))
    return Backtest(
        strategy=overrides.pop("strategy", MonthEndFixFade()),
        source=source,
        symbol=symbol,
        timeframes=[M5],
        start=overrides.pop("start", start),
        end=overrides.pop("end", end),
        starting_cash=overrides.pop("starting_cash", 100_000),
        **overrides,
    )


if __name__ == "__main__":
    build_backtest().run()
