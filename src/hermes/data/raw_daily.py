"""Shared daily-equity history path: raw bars in the cache, adjustment at read (ADR-0016).

A source mixes this in and supplies ``_fetch_raw_daily(ticker, start, end) ->
(raw_bars, events)``. Only ``1d`` goes through here; other timeframes keep each source's
original path (weekly bars span ex-dates, so a daily event table cannot adjust them).
"""

from __future__ import annotations

from datetime import datetime

from ..core import Bar, Instrument, Timeframe
from .adjustments import (
    ADJUST_MODES,
    FETCH_PAD,
    AdjustmentEvent,
    AdjustmentStore,
    adjust_bars,
)
from .cache import BarCache, checked_through

DAILY = Timeframe.parse("1d")


class RawDailyMixin:
    name: str
    cache: BarCache
    adjust: str = "total"

    def _init_adjust(self, adjust: str) -> None:
        if adjust not in ADJUST_MODES:
            raise ValueError(f"adjust must be one of {ADJUST_MODES}, got {adjust!r}")
        self.adjust = adjust

    @property
    def _raw_cache(self) -> BarCache:
        return BarCache(self.cache.root / "_raw")

    @property
    def _adjustments(self) -> AdjustmentStore:
        return AdjustmentStore(self.cache.root / "_adjustments")

    def _fetch_raw_daily(
        self, ticker: str, start: datetime, end: datetime
    ) -> tuple[list[Bar], list[AdjustmentEvent]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _uses_raw_path(self, timeframe: Timeframe) -> bool:
        return timeframe == DAILY

    def _history_raw_daily(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        ticker = instrument.symbol.ticker
        raw = self._raw_cache
        for gap_start, gap_end in raw.missing_ranges(instrument, timeframe, start, end):
            # Pad back so an ex-date on the first requested day can see the prior close.
            bars, events = self._fetch_raw_daily(ticker, gap_start - FETCH_PAD, gap_end)
            raw.write(instrument, timeframe, bars)
            raw.mark_checked(
                instrument, timeframe, gap_start,
                checked_through(bars, timeframe, gap_start, gap_end),
            )
            self._adjustments.merge(self.name, ticker, events)
        bars = raw.read(instrument, timeframe, start, end)
        return adjust_bars(bars, self._adjustments.load(self.name, ticker), self.adjust)

    def data_epoch(self, ticker: str) -> str | None:
        """Identifies the adjusted data a run would read: the basis plus a digest of the
        ticker's event table. Part of the run key (audit F16/G6)."""
        return f"{self.adjust}:{self._adjustments.digest(self.name, ticker)}"
