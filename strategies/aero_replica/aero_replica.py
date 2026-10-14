# Parameters: 0  (every constant was read off the Aero MT5 tester log, not fitted)
#
# Reverse-engineered "Aero MT5" (MQL5 Market EA, product 176860) from its MT5 tester log
# 20261009.log (XAUUSD H1, 2026-01-01..2026-08-06, idea 87eb623e).  What the log shows:
#
#   Level:   the running high / low of the broker day (server day = 17:00 New York; the
#            server is UTC+2/+3 on EU DST).  ~2/3 of the 103 limit prices sit within
#            $0.30 of it; most of the rest are recent 1h fractals (not modelled).
#   Trigger: price trades $0.50 through the level.  The EA then rests a limit AT the level
#            for ~1.5 min (fills in ~0.6 min, 61% fill rate).  15m bars cannot resolve
#            that, so this replica enters with a stop at level +/- $0.50 -- $0.50 worse
#            than the EA on every fill, but it takes the 39% the EA's limit missed.
#   Session: no new entries 00:00-02:30 or 23:00-24:00 server time; one position at a time.
#   Exit:    TP $12/oz from the level (the EA's trail locks +$10 at ~+$12 and realised a
#            mean of $11.98/oz on 65 winners; a hard TP is the bar-level equivalent).
#            "Recovery grid": once the trade is >= 3h old and underwater, its legs sit at
#            entry -/+ 0.5/1.0/1.5 x ATR(4h,20) and the first leg filling flattens the
#            basket -- i.e. a stop at entry -/+ 0.5 x ATR(4h,20) armed after 3h.
#            Catastrophe stop 3.6 x ATR(4h,20) (never hit in the log).
#   Not reproducible: the vendor's kNN filter that picks which breaks to take.
#
# Hermes ATR is Wilder's; the logged ATR matched a 20-period 4h ATR to within ~7%.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from hermes import ATR, Backtest, RiskPercent, Strategy, Symbol, Timeframe
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
SERVER_SHIFT = timedelta(hours=7)          # server midnight = 17:00 New York
BREAK_USD = 0.50                           # how far through the level the EA triggers
TP_USD = 12.0                              # realised win per oz in the log
GRID_ATR = 0.5                             # first recovery leg = effective stop
CAT_ATR = 3.6                              # catastrophe stop
GRID_AFTER = timedelta(hours=3)
ATR_TF = Timeframe.parse("4h")
ATR_PERIOD = 20
SPREAD_USD = 0.12


def server_time(ts: datetime) -> datetime:
    return ts.astimezone(NY).replace(tzinfo=None) + SERVER_SHIFT


class AeroReplica(Strategy):
    base_tf = Timeframe.parse("15m")

    def setup(self) -> None:
        self.atr = self.use(ATR(ATR_TF, ATR_PERIOD))
        self._day = None
        self._hi = None
        self._lo = None
        self._pending: list = []
        self._armed: set = set()               # ids of trades whose recovery stop is set

    def _tp(self, atr: float) -> float:
        return TP_USD

    def on_bar(self, bar) -> None:
        srv = server_time(bar.timestamp)
        for o in self._pending:
            if o.status is not OrderStatus.FILLED:
                self.venue.cancel(o)
        self._pending = []

        # running range of the server day, including this bar (it has closed)
        if srv.date() != self._day:
            self._day, self._hi, self._lo = srv.date(), bar.high, bar.low
        else:
            self._hi, self._lo = max(self._hi, bar.high), min(self._lo, bar.low)

        atr = self.indicator_value(self.atr)["value"]
        trades = self.venue.open_trades()
        if trades:
            for trade in trades:
                self._manage(trade, bar, atr)
            return
        if atr is None:
            return
        minutes = srv.hour * 60 + srv.minute + self.base_tf.seconds // 60  # next bar's start
        if minutes < 150 or minutes >= 23 * 60:
            return
        hi, lo, tp = self._hi, self._lo, self._tp(atr)
        self._pending = [
            self.buy(RiskPercent(0.01), type=OrderType.STOP, stop=hi + BREAK_USD,
                     stop_loss=hi - CAT_ATR * atr, take_profit=hi + tp, tag="aero_long"),
            self.sell(RiskPercent(0.01), type=OrderType.STOP, stop=lo - BREAK_USD,
                      stop_loss=lo + CAT_ATR * atr, take_profit=lo - tp, tag="aero_short"),
        ]

    def _manage(self, trade, bar, atr) -> None:
        if id(trade) in self._armed or atr is None:
            return
        if bar.timestamp + timedelta(seconds=self.base_tf.seconds) - trade.entry_time < GRID_AFTER:
            return
        long = trade.side.name == "BUY"
        entry = trade.entry_price
        underwater = bar.close < entry if long else bar.close > entry
        if not underwater:
            return
        stop = entry - GRID_ATR * atr if long else entry + GRID_ATR * atr
        self._armed.add(id(trade))
        if (bar.close <= stop) if long else (bar.close >= stop):
            self.close(trade)
        else:
            self.modify(trade, stop_loss=stop)


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("XAUUSD", "pepperstone"))
    if "cost_model" not in overrides:
        overrides["cost_model"] = CostModel(
            commission=PerLotCommission(per_lot=3.5),
            spread=SpreadModel(points=SPREAD_USD, schedule=SpreadSchedule.cfd_default()),
            slippage=SlippageModel(ticks=0.0),
            financing=FinancingModel(annual_rate=0.005),
        )
    base = overrides.pop("base", None)          # "5m" for finer fills/exits; default 15m
    strategy = overrides.pop("strategy", None) or AeroReplica()
    if base:
        strategy.base_tf = Timeframe.parse(base)
    return Backtest(
        strategy=strategy,
        source=PepperstoneSource(),
        symbol=symbol,
        timeframes=[strategy.base_tf],
        start=overrides.pop("start", datetime(2018, 1, 1, tzinfo=UTC)),
        end=overrides.pop("end", datetime(2026, 9, 30, 23, 59, tzinfo=UTC)),
        starting_cash=overrides.pop("starting_cash", 100_000),
        **overrides,
    )


if __name__ == "__main__":
    build_backtest().run()
