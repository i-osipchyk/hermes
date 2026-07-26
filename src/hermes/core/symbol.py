"""Symbol: the identifier string for an Instrument. Never the object itself."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Symbol:
    """A provider-scoped identifier, e.g. ``AAPL``, ``BTCUSDT``, ``EURUSD``.

    ``source`` namespaces the ticker so the same ticker on different providers
    (or a stock vs a CFD on the same underlying) never collide.
    """

    ticker: str
    source: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.source}:{self.ticker}"
