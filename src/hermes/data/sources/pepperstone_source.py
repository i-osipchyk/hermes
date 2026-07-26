"""Pepperstone-backed DataSource for CFDs (via MetaTrader5).

Normalization responsibilities:
  * MT5 bars are BID — record price_basis=BID so the Cost Model applies spread;
  * near-24/5 Session Calendar; sized in lots; leverage/margin metadata;
  * share CFDs receive dividend adjustments (modelled as cash, like stocks).

Note: the MetaTrader5 package is Windows-only; this source is import-guarded.
"""

from __future__ import annotations

from datetime import datetime

from ...core import Bar, Cfd, Instrument, Symbol, Timeframe
from ..source import DataSource


class PepperstoneSource(DataSource):
    name = "pepperstone"

    def get_instrument(self, symbol: Symbol) -> Cfd:
        raise NotImplementedError  # TODO: MT5 symbol_info -> Cfd (lot/tick/leverage/session)

    def history(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        raise NotImplementedError  # TODO: MT5 copy_rates -> UTC bid Bars, cache-first

    def supported_timeframes(self) -> set[Timeframe]:
        return {
            Timeframe.parse(t)
            for t in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
        }
