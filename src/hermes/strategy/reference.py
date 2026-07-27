"""A reference instrument a Strategy *observes* but does not trade — e.g. SPY for a
market-regime filter.

Still single-instrument trading (ADR-0003): the Strategy trades its one Instrument; a
Reference is a read-only, look-ahead-safe data feed the engine steps in lockstep with
the trading clock. Declare Indicators on it so the engine sizes the Lead-in and warmup.
"""

from __future__ import annotations

from ..indicators import Indicator


class Reference:
    def __init__(self, symbol: str, source=None) -> None:
        self.symbol = symbol
        self.source = source          # optional DataSource; None -> the Backtest's source
        self._indicators: list[Indicator] = []
        self._view = None             # MultiTimeframeView, set by the engine

    def use(self, indicator: Indicator) -> Indicator:
        """Declare an Indicator on the reference (counted in Lead-in + warmup)."""
        self._indicators.append(indicator)
        return indicator

    def data(self, timeframe):
        """The reference's TimeframeSeries (closed + Forming Bar) as of now."""
        return self._view[timeframe]

    def value(self, indicator: Indicator) -> dict:
        """Compute a declared Indicator over the reference's series as of now."""
        return indicator.compute(self._view[indicator.timeframe].bars_for_compute())

    @property
    def indicators(self) -> list[Indicator]:
        return self._indicators

    @property
    def timeframes(self) -> set:
        return {ind.timeframe for ind in self._indicators}
