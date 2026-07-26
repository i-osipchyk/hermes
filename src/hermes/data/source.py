"""DataSource: the market-data side of the parity seam (ADR-0001).

The same interface serves historical/replay bars (backtest) and, later, live
streaming bars — a Strategy cannot tell which it is running against. Each concrete
source also resolves provider tickers into :class:`Instrument` objects and owns its
own normalization and Parquet cache.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime

from ..core import Bar, Instrument, Symbol, Timeframe


class DataSource(ABC):
    """Abstract provider adapter.

    Implementations: :class:`YFinanceSource`, :class:`BinanceSource`,
    :class:`PepperstoneSource`. All raw provider quirks (adjustments, pagination,
    tz, bid-vs-last) are resolved here so everything above sees uniform ``Bar``s.
    """

    name: str

    @abstractmethod
    def get_instrument(self, symbol: Symbol) -> Instrument:
        """Resolve a Symbol into a fully-populated Instrument (metadata, session)."""

    @abstractmethod
    def history(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        """Return closed bars in ``[start, end]`` at ``timeframe``, UTC, ascending.

        Reads from the local cache first; fetches only missing ranges from the
        provider (fetch-once, incremental — see ADR/CONTEXT). ``start`` here should
        already include the Lead-in the engine requests for indicator warmup.
        """

    def stream(self, instrument: Instrument, timeframe: Timeframe) -> Iterator[Bar]:
        """Yield live bars as they close. Out of scope for v1 (deployment)."""
        raise NotImplementedError("Live streaming is out of scope for v1")

    # Providers expose different native timeframes; the engine fetches the finest
    # it needs and aggregates upward. Implementations advertise support here.
    @abstractmethod
    def supported_timeframes(self) -> set[Timeframe]: ...
