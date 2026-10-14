# Parameters: 1  (range_min)
#
# US index cash-open opening range breakout, baseline (idea 08e23fa6; Crabel 1990).
#
#   Range:   high/low of the first range_min minutes after the 09:30 New York cash open
#            (NYSE sessions only, from exchange_calendars XNYS; holidays are skipped because
#            the CFD trades but the cash open -- the mechanism -- does not happen).
#   Entry:   buy stop one tick above the range high / sell stop one tick below the low, live
#            from the close of the last range bar until 15:00 NY; each side at most once a day;
#            one position at a time (a stopped long can still become a short the same day).
#   Exit:    stop at the opposite edge of the range; no target; flatten order issued on the
#            15:50 bar (fills 15:55), 10 minutes before an early close on half days.
#   Size:    1% risk per trade.
#
# Costs: measured cTrader bid/ask spread at 09:25-10:30 NY, quarterly samples 2018-2026
# (research/us_index_orb/measure_open_spread.py): US500 0.4 pt, NAS100 1.0 pt throughout, no
# widening at the open except NAS100 2025-26 (1.3-1.4 pt for the first 5 min). Commission 0
# (ProtoOASymbol). Slippage on stop fills in the opening rush is not modelled -- the 2x-cost
# test is where it is judged.

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import exchange_calendars as xc

from hermes import Backtest, Parameter, RiskPercent, Strategy, Symbol, Timeframe
from hermes.data import PepperstoneSource
from hermes.execution import (
    CostModel,
    FinancingModel,
    OrderStatus,
    OrderType,
    PerLotCommission,
    SlippageModel,
    SpreadModel,
    SpreadSchedule,
)

GENERATED_BY = "hermes-strategy"

NY = ZoneInfo("America/New_York")
OPEN_MIN = 9 * 60 + 30
LAST_ENTRY_MIN = 15 * 60
TICK = 0.1
SPREAD = {"US500": 0.4, "NAS100": 1.0}

_XNYS = xc.get_calendar("XNYS")


def _session_close_min(d: date) -> int | None:
    """Minute-of-day (NY) the cash session closes on ``d``, or None if NYSE is shut."""
    key = d.isoformat()
    if not _XNYS.is_session(key):
        return None
    close = _XNYS.session_close(key).tz_convert(NY)
    return close.hour * 60 + close.minute


class UsIndexOrb(Strategy):
    base_tf = Timeframe.parse("5m")

    def setup(self) -> None:
        self.range_min = int(self.param(Parameter(
            "range_min", 15, choices=(5, 10, 15, 20, 30, 45, 60),
            description="Opening range length in minutes after the 09:30 NY cash open")))
        self._day: date | None = None
        self._close_min: int | None = None
        self._hi = self._lo = None
        self._done: set[int] = set()
        self._pending: list = []
        self._armed = False

    def _new_day(self, d: date) -> None:
        self._day = d
        self._close_min = _session_close_min(d)
        self._hi = self._lo = None
        self._done = set()
        self._armed = False

    def _sync_pending(self) -> None:
        for o in self._pending:
            if o.status is OrderStatus.FILLED:
                self._done.add(1 if o.side.name == "BUY" else -1)
            else:
                self.venue.cancel(o)
        self._pending = []

    def on_bar(self, bar) -> None:
        ts = bar.timestamp.astimezone(NY)          # bar open time; called once the bar closed
        minute = ts.hour * 60 + ts.minute
        if ts.date() != self._day:
            self._new_day(ts.date())
        self._sync_pending()

        trades = self.venue.open_trades()
        if self._close_min is None:
            return
        flat_min = self._close_min - 10              # issue on this bar -> fills at close - 5
        if trades:
            if minute >= flat_min:
                for t in trades:
                    self.close(t)
            return

        range_end = OPEN_MIN + self.range_min
        if OPEN_MIN <= minute < range_end:
            self._hi = bar.high if self._hi is None else max(self._hi, bar.high)
            self._lo = bar.low if self._lo is None else min(self._lo, bar.low)
            if minute + 5 == range_end:
                self._armed = True                   # range complete at this bar's close
            else:
                return
        if not self._armed or minute + 5 > min(LAST_ENTRY_MIN, flat_min):
            return
        if 1 not in self._done:
            entry = self._hi + TICK
            self._pending.append(self.buy(RiskPercent(0.01), type=OrderType.STOP, stop=entry,
                                          stop_loss=self._lo, tag="orb_long"))
        if -1 not in self._done:
            entry = self._lo - TICK
            self._pending.append(self.sell(RiskPercent(0.01), type=OrderType.STOP, stop=entry,
                                           stop_loss=self._hi, tag="orb_short"))


def cost_model(ticker: str) -> CostModel:
    return CostModel(
        commission=PerLotCommission(per_lot=0.0),
        spread=SpreadModel(points=SPREAD.get(ticker, SPREAD["US500"]),
                           schedule=SpreadSchedule.cfd_default()),
        slippage=SlippageModel(ticks=0.0),
        financing=FinancingModel(annual_rate=0.005),
    )


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("NAS100", "pepperstone"))
    overrides.setdefault("cost_model", cost_model(symbol.ticker))
    strategy = overrides.pop("strategy", None) or UsIndexOrb()
    return Backtest(
        strategy=strategy,
        source=PepperstoneSource(),
        symbol=symbol,
        timeframes=[strategy.base_tf],
        # In-sample window. 2024-01-01 onward is the holdout (idea 08e23fa6): do not run there
        # until a candidate is frozen.
        start=overrides.pop("start", datetime(2018, 1, 1, tzinfo=UTC)),
        end=overrides.pop("end", datetime(2024, 1, 1, tzinfo=UTC)),
        starting_cash=overrides.pop("starting_cash", 100_000),
        **overrides,
    )


if __name__ == "__main__":
    build_backtest().run()
