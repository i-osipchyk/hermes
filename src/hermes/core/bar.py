"""Bar: the one uniform OHLCV row every DataSource produces (see ADR-0001).

Its shape never changes across sources. A higher-timeframe Forming Bar is just a
Bar with ``is_closed=False`` whose fields are aggregated from base bars and mutate
each base step until its Timeframe boundary is crossed (see ADR-0002)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .timeframe import Timeframe


@dataclass(frozen=True, slots=True)
class Bar:
    """One candlestick.

    Attributes
    ----------
    timestamp:
        Bar OPEN time, always timezone-aware UTC. (Bucketing boundaries are
        computed in the Instrument's exchange-local tz, but bars are stored in
        UTC — see the "Bar bucketing rule" in CONTEXT.md.)
    timeframe:
        The interval this bar represents.
    is_closed:
        ``False`` while this is a still-forming higher-timeframe bar. Closed base
        bars and finalised higher-timeframe bars are ``True``.
    """

    timestamp: datetime
    timeframe: Timeframe
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = True
