"""InMemorySource: a DataSource backed by pre-built bars.

For tests, offline experimentation, and feeding the engine synthetic data. Also the
simplest reference implementation of the DataSource interface.
"""

from __future__ import annotations

from datetime import datetime

from ...core import Bar, Instrument, Symbol, Timeframe
from ..source import DataSource


class InMemorySource(DataSource):
    name = "memory"

    def __init__(self, instrument: Instrument, bars_by_timeframe: dict[Timeframe, list[Bar]]):
        self._instrument = instrument
        self._bars = bars_by_timeframe

    def get_instrument(self, symbol: Symbol) -> Instrument:
        return self._instrument

    def history(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        return [b for b in self._bars.get(timeframe, []) if start <= b.timestamp <= end]

    def supported_timeframes(self) -> set[Timeframe]:
        return set(self._bars.keys())
