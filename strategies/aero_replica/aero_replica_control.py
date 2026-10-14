# Parameters: 2  (random, seed -- control switches, not tunables)
#
# Control for aero_replica_atr (idea 87eb623e).  Same day-range break detection, same
# session, same exits (TP 0.33 x ATR(4h,20), stop 0.5 x ATR armed after 3h underwater,
# catastrophe 3.6 x ATR) -- but entry is a MARKET order at the next bar's open after a bar
# that broke the running server-day range by $0.50, so both arms enter at the same moments:
#   random=0  trade in the break direction (the hypothesis);
#   random=1  trade a seeded coin-flip side (the control).
# If random=1 matches random=0, the break direction carries no information and any profit
# is the exit geometry meeting the volatility regime.

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

from hermes import Parameter, RiskPercent

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_aero_replica", Path(__file__).with_name("aero_replica.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"

TP_ATR = 0.33


class AeroReplicaControl(_base.AeroReplica):
    def setup(self) -> None:
        super().setup()
        self._random = int(self.param(Parameter("random", 0, bounds=(0, 1),
                                                description="1 = coin-flip side (control)")))
        self._rng = random.Random(int(self.param(Parameter("seed", 1, bounds=(0, 10_000),
                                                           description="RNG seed"))))

    def on_bar(self, bar) -> None:
        srv = _base.server_time(bar.timestamp)
        new_day = srv.date() != self._day
        prev_hi, prev_lo = self._hi, self._lo
        if new_day:
            self._day, self._hi, self._lo = srv.date(), bar.high, bar.low
        else:
            self._hi, self._lo = max(self._hi, bar.high), min(self._lo, bar.low)

        atr = self.indicator_value(self.atr)["value"]
        trades = self.venue.open_trades()
        if trades:
            for trade in trades:
                self._manage(trade, bar, atr)
            return
        if atr is None or new_day:
            return
        minutes = srv.hour * 60 + srv.minute + self.base_tf.seconds // 60
        if minutes < 150 or minutes >= 23 * 60:
            return
        up = bar.high >= prev_hi + _base.BREAK_USD
        down = bar.low <= prev_lo - _base.BREAK_USD
        if up == down:                       # no break, or both sides in one bar
            return
        direction = 1 if up else -1
        if self._random:
            direction = self._rng.choice((1, -1))
        ref, tp, cat = bar.close, TP_ATR * atr, _base.CAT_ATR * atr
        if direction > 0:
            self.buy(RiskPercent(0.01), stop_loss=ref - cat, take_profit=ref + tp, tag="ctl_long")
        else:
            self.sell(RiskPercent(0.01), stop_loss=ref + cat, take_profit=ref - tp, tag="ctl_short")


def build_backtest(**overrides):
    overrides.setdefault("strategy", AeroReplicaControl())
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
