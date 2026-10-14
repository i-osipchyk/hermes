# Parameters: 1  (k)
#
# Sunday-open gap fill, large gaps only (idea 116ee823, iteration 3).
#
# Same entry (20:00 NY, after the wide Sunday spread -- see fx_sunday_gap_tokyo.py),
# take profit and time exit. The idea's own condition: only gaps larger than k x the
# daily ATR are an overshoot; small gaps are noise around Friday's close. k = 0.25 is
# set a priori (about the top quarter of weekend gaps), not fitted.
#
# Daily ATR: simple 14-session mean of true range over FX sessions that roll at 17:00 New
# York, built from closed 5m bars as they arrive -- the current (Sunday) session is never
# part of it, so the threshold uses only data from before the weekend.

from __future__ import annotations

import importlib.util
from collections import deque
from datetime import timedelta
from pathlib import Path

from hermes import Backtest, Parameter, Symbol

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_fx_sunday_gap_tokyo", Path(__file__).with_name("fx_sunday_gap_tokyo.py")
)
_tokyo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tokyo)
_base = _tokyo._base

GENERATED_BY = "hermes-strategy"

ATR_SESSIONS = 14
SESSION_ROLL = timedelta(hours=7)    # NY 17:00 + 7h = midnight: the FX trading day


class SundayGapFadeLarge(_tokyo.SundayGapFadeTokyo):
    def setup(self) -> None:
        super().setup()
        self._k = self.param(Parameter(
            "k", 0.25, bounds=(0.1, 0.5),
            description="Fade only weekend gaps larger than k x the 14-session daily ATR",
        ))
        self._session = None
        self._hi = self._lo = self._close = None
        self._prev_session_close: float | None = None
        self._tr: deque[float] = deque(maxlen=ATR_SESSIONS)

    def _roll_session(self, bar) -> None:
        session = (bar.timestamp.astimezone(_base.NEW_YORK) + SESSION_ROLL).date()
        if session != self._session:
            if self._session is not None:
                prev = self._prev_session_close
                tr = self._hi - self._lo if prev is None else max(self._hi, prev) - min(self._lo, prev)
                self._tr.append(tr)
                self._prev_session_close = self._close
            self._session, self._hi, self._lo = session, bar.high, bar.low
        self._hi, self._lo, self._close = max(self._hi, bar.high), min(self._lo, bar.low), bar.close

    def on_bar(self, bar) -> None:
        self._roll_session(bar)
        super().on_bar(bar)

    def gap_direction(self, gap: float, bar) -> int:
        if len(self._tr) < ATR_SESSIONS:
            return 0
        if abs(gap) <= self._k * sum(self._tr) / ATR_SESSIONS:
            return 0
        return (gap > 0) - (gap < 0)


def build_backtest(**overrides) -> Backtest:
    overrides.setdefault("strategy", SundayGapFadeLarge())
    overrides.setdefault("symbol", Symbol("EURUSD", "pepperstone"))
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
