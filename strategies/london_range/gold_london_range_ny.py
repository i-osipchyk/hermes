# Parameters: 0
#
# Gold London range -> NY open, literal form (idea c706aa04; research/london_range/).
#
#   Range:   high/low of 5m bars from 08:00 Europe/London to 08:30 America/New_York, weekdays.
#   Entry:   at the 08:30 ET bar open, if inside the range, go toward the FARTHER edge.
#   Exit:    target = that edge; stop = the same distance on the other side (1:1);
#            flat at the 16:45 ET bar. One trade a day, 1% risk.
#            Siblings set `toward`/`stop_at` (gold_london_bracket_far / _near).
#
# The event study found the London edges are touched after the NY open no more often than
# random levels at the same distance, so this is the baseline of record, not a candidate.

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from hermes import Backtest, RiskPercent, Strategy, Symbol, Timeframe
from hermes.data import PepperstoneSource
from hermes.execution import (
    CostModel,
    FinancingModel,
    OrderType,
    PerLotCommission,
    SlippageModel,
    SpreadModel,
    SpreadSchedule,
)

GENERATED_BY = "hermes-strategy"

LDN, NY = ZoneInfo("Europe/London"), ZoneInfo("America/New_York")
RANGE_START_LDN = 8 * 60
NY_OPEN = 8 * 60 + 30
FLAT_NY = 16 * 60 + 45
MIN_RANGE_BARS = 40


class GoldLondonRangeNY(Strategy):
    base_tf = Timeframe.parse("5m")
    toward = "far"          # which edge is the target: "far" or "near"
    stop_at = "mirror"      # "mirror": same distance as the target (1:1); "edge": the other edge

    def setup(self) -> None:
        self._day = None
        self._hi = self._lo = None
        self._n = 0
        self._traded = False

    def on_bar(self, bar) -> None:
        ny = bar.timestamp.astimezone(NY)
        ldn = bar.timestamp.astimezone(LDN)
        nym, ldnm = ny.hour * 60 + ny.minute, ldn.hour * 60 + ldn.minute
        if ny.date() != self._day:
            self._day, self._hi, self._lo, self._n, self._traded = ny.date(), None, None, 0, False
        trades = self.venue.open_trades()
        if trades and nym >= FLAT_NY:
            for t in trades:
                self.close(t)
            return
        if ny.weekday() >= 5:
            return
        # The bar that closes at 08:30 ET is the last range bar; the order fills at 08:30's open.
        end_nym = nym + 5
        if ldnm >= RANGE_START_LDN and ldn.date() == ny.date() and end_nym <= NY_OPEN:
            self._hi = bar.high if self._hi is None else max(self._hi, bar.high)
            self._lo = bar.low if self._lo is None else min(self._lo, bar.low)
            self._n += 1
        if end_nym != NY_OPEN or self._traded or trades or self._n < MIN_RANGE_BARS:
            return
        self._traded = True
        p = bar.close
        if not (self._lo < p < self._hi):
            return
        up_is_far = self._hi - p > p - self._lo
        if up_is_far == (self.toward == "far"):
            sl = p - (self._hi - p) if self.stop_at == "mirror" else self._lo
            self.buy(RiskPercent(0.01), type=OrderType.MARKET, stop_loss=sl,
                     take_profit=self._hi, tag="ldn_long")
        else:
            sl = p + (p - self._lo) if self.stop_at == "mirror" else self._hi
            self.sell(RiskPercent(0.01), type=OrderType.MARKET, stop_loss=sl,
                      take_profit=self._lo, tag="ldn_short")


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("XAUUSD", "pepperstone"))
    overrides.setdefault("cost_model", CostModel(
        commission=PerLotCommission(per_lot=3.5),
        spread=SpreadModel(points=0.12, schedule=SpreadSchedule.cfd_default()),
        slippage=SlippageModel(ticks=0.0),
        financing=FinancingModel(annual_rate=0.005),
    ))
    strategy = overrides.pop("strategy", None) or GoldLondonRangeNY()
    return Backtest(
        strategy=strategy,
        source=PepperstoneSource(),
        symbol=symbol,
        timeframes=[strategy.base_tf],
        start=overrides.pop("start", datetime(2018, 1, 1, tzinfo=UTC)),
        end=overrides.pop("end", datetime(2023, 1, 1, tzinfo=UTC)),
        starting_cash=overrides.pop("starting_cash", 100_000),
        **overrides,
    )


if __name__ == "__main__":
    build_backtest().run()
