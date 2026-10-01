"""Quote-currency -> account-currency conversion (look-ahead-safe, ADR-0002).

A venue books money in the account currency, but an Instrument's P&L arrives in its
quote currency. :class:`ClosedBarRate` answers "how many account-currency units is one
quote-currency unit worth?" using only bars that have CLOSED strictly before ``ts``.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime

from ..core import Bar


class ClosedBarRate:
    """Rate from a conversion pair's bars: the close of the last bar before ``ts``.

    ``inverse=True`` when the pair quotes the account currency against the quote
    currency (USDJPY: 150 JPY per USD), so one JPY is worth ``1 / close`` USD.
    """

    def __init__(self, bars: list[Bar], *, inverse: bool) -> None:
        self._bars = sorted(bars, key=lambda b: b.timestamp)
        self._times = [b.timestamp for b in self._bars]
        self._inverse = inverse

    def __call__(self, ts: datetime) -> float:
        i = bisect_left(self._times, ts)  # bars strictly before ts are closed
        # Before any closed bar exists, the first bar's open is the only known price.
        price = self._bars[i - 1].close if i > 0 else self._bars[0].open
        return 1.0 / price if self._inverse else price
