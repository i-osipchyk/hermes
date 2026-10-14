# Parameters: 2  (range_min, gap_atr)
#
# us_index_orb with the two changes the in-sample dissection pointed at
# (research/us_index_orb/dissect_baseline.py, runs 4f24b657 / 8b60557c, 2018-23):
#
#   Gap gate:    trade only when |09:30 open - previous session's cash close| >= gap_atr x ATR,
#                ATR = mean high-low of the previous 14 full cash sessions (09:30-16:00).
#                The mechanism is the open absorbing an overnight imbalance; with no gap there
#                is none to extend, and those days lost on both indices (|gap| < 0.15 ATR:
#                -0.07..-0.19 R/trade) while bigger gaps earned +0.08..+0.27 R in every bucket
#                above 0.2 on NAS100 and all but one on US500. 0.2 = middle of that plateau.
#   First break: one entry per day. The reversal entry after a stopped first break earned
#                +0.01 R (NAS100) / -0.05 R (US500): once the open imbalance has failed, the
#                second break is no longer the mechanism.
#
# Everything else as us_index_orb (range, stop at the opposite edge, flat 15:55, costs).

from __future__ import annotations

import importlib.util
from collections import deque
from pathlib import Path

from hermes import Parameter

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_us_index_orb", Path(__file__).with_name("us_index_orb.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"

ATR_DAYS = 14
FULL_CLOSE_MIN = 16 * 60
BOTH = frozenset({1, -1})


class UsIndexOrbGap(_base.UsIndexOrb):
    def setup(self) -> None:
        super().setup()
        self.gap_atr = float(self.param(Parameter(
            "gap_atr", 0.2, choices=(0.1, 0.15, 0.2, 0.25, 0.3, 0.4),
            description="Trade only if the overnight gap is at least this many ATR(14 cash "
                        "sessions)")))
        self._ranges: deque[float] = deque(maxlen=ATR_DAYS)
        self._prev_close: float | None = None
        self._s_hi = self._s_lo = self._s_close = None

    def _new_day(self, d) -> None:
        # Close out yesterday's cash session for the gap and ATR yardsticks.
        if self._s_close is not None:
            self._prev_close = self._s_close
            if self._close_min == FULL_CLOSE_MIN:
                self._ranges.append(self._s_hi - self._s_lo)
        self._s_hi = self._s_lo = self._s_close = None
        super()._new_day(d)
        self._done = set(BOTH)          # blocked until the 09:30 bar passes the gap gate

    def _sync_pending(self) -> None:
        super()._sync_pending()
        if self._done:                  # first break only
            self._done = set(BOTH)

    def on_bar(self, bar) -> None:
        ts = bar.timestamp.astimezone(_base.NY)
        minute = ts.hour * 60 + ts.minute
        if ts.date() != self._day:
            self._new_day(ts.date())
        if self._close_min is not None and _base.OPEN_MIN <= minute < self._close_min:
            if minute == _base.OPEN_MIN and self._prev_close is not None \
                    and len(self._ranges) == ATR_DAYS:
                atr = sum(self._ranges) / ATR_DAYS
                if abs(bar.open - self._prev_close) >= self.gap_atr * atr:
                    self._done = set()
            self._s_hi = bar.high if self._s_hi is None else max(self._s_hi, bar.high)
            self._s_lo = bar.low if self._s_lo is None else min(self._s_lo, bar.low)
            self._s_close = bar.close
        super().on_bar(bar)


def build_backtest(**overrides):
    overrides.setdefault("strategy", UsIndexOrbGap())
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
