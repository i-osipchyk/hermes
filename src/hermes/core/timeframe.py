"""Timeframe: a bar interval, with the integer-multiple relationship that the
multi-timeframe aggregation depends on (see ADR-0002)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_PATTERN = re.compile(r"^(\d+)(m|h|d|w)$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86_400, "w": 604_800}


@total_ordering
@dataclass(frozen=True, slots=True)
class Timeframe:
    """A bar interval such as ``15m``, ``1h``, ``1d``.

    Timeframes are compared and combined by their duration in seconds. Every
    subscribed Timeframe in a Strategy must be an integer multiple of the Base
    Timeframe — :meth:`is_multiple_of` enforces that invariant upstream.
    """

    seconds: int

    @classmethod
    def parse(cls, text: str) -> Timeframe:
        m = _PATTERN.match(text.strip().lower())
        if not m:
            raise ValueError(f"Unrecognised timeframe: {text!r} (expected e.g. '15m', '1h', '1d')")
        value, unit = int(m.group(1)), m.group(2)
        return cls(value * _UNIT_SECONDS[unit])

    def is_multiple_of(self, base: Timeframe) -> bool:
        """True if this Timeframe is a whole-number multiple of ``base``."""
        return self.seconds % base.seconds == 0

    def __lt__(self, other: Timeframe) -> bool:
        return self.seconds < other.seconds

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        for unit in ("w", "d", "h", "m"):
            size = _UNIT_SECONDS[unit]
            if self.seconds % size == 0:
                return f"{self.seconds // size}{unit}"
        return f"{self.seconds}s"
