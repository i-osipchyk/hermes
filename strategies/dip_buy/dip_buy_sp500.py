# Parameters: 1  (depth)
#
# S&P 500 volatile-dip limit buy (idea 6f4f38e2; user-supplied rule set, Connors/Alvarez style).
#
#   Universe: point-in-time S&P 500 (sp500_history.csv), yfinance daily bars. Fully delisted
#             names have no yfinance bars, so their legs are empty (survivor bias, optimistic).
#   Signal:   at the close, close > SMA(200), close down > 3% vs the prior close, and
#             100*ATR(5)/close > 3.
#   Entry:    a limit buy at close - depth*ATR(5), good for the next day only.
#   Exits:    take profit at the previous close + 0.5*ATR(5), re-set every close and live from
#             the day after entry; a close above the previous day's high -> exit at the next
#             open; 10 bars held -> exit at the next open.
#   Sizing:   10% of equity per position (10 slots), one capital pool.
#   Costs:    5 bp a side of notional.
#
# ATR(5) here is the simple mean of the true range (as in research/dip_buy/dip_study.py), not
# Hermes' Wilder ATR. The engine runs one leg per symbol, so the book's cross-sectional
# preference (highest ATR/close first when slots are short) is not modelled here; slots
# rarely bind (0.7 of 10 used on average, research/dip_buy/FINDINGS.md), and the event
# study in research/dip_buy/ does model it.

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT), str(_ROOT / "src")]

from hermes import (  # noqa: E402
    SMA,
    EquityFraction,
    Parameter,
    Strategy,
    Timeframe,
    UniverseBacktest,
    YFinanceSource,
)
from hermes.data import ConstituentCalendar  # noqa: E402
from hermes.execution import (  # noqa: E402
    CostModel,
    FinancingModel,
    OrderStatus,
    OrderType,
    PercentCommission,
    SlippageModel,
    SpreadModel,
)

GENERATED_BY = "hermes-strategy"

D1 = Timeframe.parse("1d")
CALENDAR = _ROOT / "sp500_history.csv"

TREND_LEN = 200
ATR_LEN = 5
DROP = -0.03
VOL_MIN = 3.0          # 100 * ATR(5) / close
TARGET_ATR = 0.5
MAX_HOLD = 10
SLOT = 0.10

COST_MODEL = CostModel(
    commission=PercentCommission(rate=0.0005),
    spread=SpreadModel(points=0.0),
    slippage=SlippageModel(ticks=0.0),
    financing=FinancingModel(annual_rate=0.0),
)


class DipBuy(Strategy):
    def setup(self) -> None:
        self.depth = self.param(Parameter(
            "depth", 0.9, bounds=(0.3, 1.5),
            description="Limit depth below the signal close, in ATR(5)",
        ))
        self.sma = self.use(SMA(D1, TREND_LEN))   # also sizes the lead-in
        self._pending = None
        self._held: dict = {}   # entry_time -> bars seen since entry

    def on_bar(self, bar) -> None:
        closed = self.data(D1).closed()
        sma = self.indicator_value(self.sma)["value"]
        if sma is None or len(closed) < ATR_LEN + 1:
            return
        assert closed[-1].timestamp == bar.timestamp
        if self._pending is not None and self._pending.status == OrderStatus.WORKING:
            self.venue.cancel(self._pending)   # a day order: unfilled -> gone
        self._pending = None

        prev = closed[-2]
        trs = [
            max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
            for p, c in zip(closed[-ATR_LEN - 1:-1], closed[-ATR_LEN:])
        ]
        atr = sum(trs) / ATR_LEN

        for trade in list(self.venue.open_trades()):
            held = self._held.get(trade.entry_time, 0)
            self._held[trade.entry_time] = held + 1
            if bar.close > prev.high or held + 1 >= MAX_HOLD + 1:
                self.close(trade)
            else:
                self.modify(trade, take_profit=bar.close + TARGET_ATR * atr)

        if not self.venue.position().is_flat:
            return
        if (bar.close > sma
                and bar.close / prev.close - 1 < DROP
                and 100 * atr / bar.close > VOL_MIN):
            self._pending = self.buy(
                EquityFraction(SLOT),
                type=OrderType.LIMIT,
                limit=bar.close - self.depth * atr,
                tag="dip",
            )

    def on_trade_closed(self, trade) -> None:
        self._held.pop(trade.entry_time, None)


def build_universe_backtest(**overrides) -> UniverseBacktest:
    kw = dict(
        strategy_factory=DipBuy,
        source=YFinanceSource(),
        calendar=ConstituentCalendar.from_snapshot_csv(CALENDAR),
        timeframes=[D1],
        start=datetime(2011, 1, 1, tzinfo=UTC),
        end=datetime(2022, 12, 31, tzinfo=UTC),
        starting_cash=1_000_000.0,
        cost_model=COST_MODEL,
    )
    kw.update(overrides)
    return UniverseBacktest(**kw)


if __name__ == "__main__":
    print(build_universe_backtest().run().portfolio_result.result.metrics)
