"""The holdout: the last slice of an instrument's history that research may not read
(ADR-0012). Carved by policy, frozen at first touch, enforced in the data layer."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core import Bar, Instrument, Symbol, Timeframe
from ..data.source import DataSource

HOLDOUT_FRACTION = 0.20
MIN_HOLDOUT = timedelta(days=183)        # ~6 months
MIN_IN_SAMPLE = timedelta(days=365)      # ~12 months; less than this and no holdout is carved

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def default_path() -> Path:
    """Read at call time, not import time (HERMES_* convention)."""
    return Path(os.environ.get("HERMES_HOLDOUTS_PATH") or "research/holdouts.json")


def _key(source: DataSource, symbol: Symbol, timeframe: Timeframe) -> str:
    return f"{source.name}/{symbol.ticker}/{timeframe}"


class HoldoutPolicy:
    def __init__(self, path: Path | str | None = None, *, now: datetime | None = None):
        self._path = Path(path) if path else None
        self._now = now

    @property
    def path(self) -> Path:
        return self._path or default_path()

    def holdout_start(
        self, source: DataSource, symbol: Symbol, timeframe: Timeframe
    ) -> datetime | None:
        key = _key(source, symbol, timeframe)
        store = self._read()
        if key in store:                              # frozen at first touch: never slides
            frozen = store[key]["holdout_start"]
            return datetime.fromisoformat(frozen) if frozen else None
        start = self._carve(source, symbol, timeframe, self._now or datetime.now(UTC))
        store[key] = {
            "holdout_start": start.isoformat() if start else None,
            "frozen_at": datetime.now(UTC).isoformat(),
            "looks": [],
        }
        self._write(store)
        return start

    def looks(self, source: DataSource, symbol: Symbol, timeframe: Timeframe) -> list[dict]:
        """Every look any idea has taken at this instrument's holdout, oldest first."""
        return list(self._read().get(_key(source, symbol, timeframe), {}).get("looks", []))

    def record_look(
        self, source: DataSource, symbol: Symbol, timeframe: Timeframe, look: dict
    ) -> None:
        store = self._read()
        store[_key(source, symbol, timeframe)]["looks"].append(look)
        self._write(store)

    def _read(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _write(self, store: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _carve(
        source: DataSource, symbol: Symbol, timeframe: Timeframe, now: datetime
    ) -> datetime | None:
        # Probe up to the start of today, never a far-future date: a caching source marks
        # the whole requested range "checked", so asking for 2200 would seal the empty
        # future and bars published later would never be fetched.
        today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        inst = source.get_instrument(symbol)
        bars = source.history(inst, timeframe, _EPOCH, today)
        if not bars:
            return None
        first, last = bars[0].timestamp, bars[-1].timestamp
        span = last - first
        held = max(span * HOLDOUT_FRACTION, MIN_HOLDOUT)
        if span - held < MIN_IN_SAMPLE:
            return None
        return (last - held).replace(hour=0, minute=0, second=0, microsecond=0)


class HoldoutViolation(Exception):
    """A research read reached into the held-out data. Loud by design: no clipping and no
    bypass flag (ADR-0012)."""


class HoldoutSource(DataSource):
    """Wraps any :class:`DataSource` for research use. A ``history`` request whose ``end``
    passes the instrument's ``holdout_start`` raises :class:`HoldoutViolation`.

    ``holdout_start`` is the first held-out instant, so a request ending exactly on it is
    allowed but never receives a bar stamped at or after it (bars are keyed by open time).
    """

    def __init__(self, inner: DataSource, policy: HoldoutPolicy | None = None):
        self._inner = inner
        self._policy = policy or HoldoutPolicy()
        self.name = inner.name

    def data_epoch(self, ticker: str) -> str | None:
        # duck-typed sources (e.g. a multi-source router) may not derive from DataSource
        epoch = getattr(self._inner, "data_epoch", None)
        return epoch(ticker) if callable(epoch) else None

    def get_instrument(self, symbol: Symbol) -> Instrument:
        return self._inner.get_instrument(symbol)

    def supported_timeframes(self) -> set[Timeframe]:
        return self._inner.supported_timeframes()

    @property
    def inner(self) -> DataSource:
        """The unguarded source. Only the holdout unlock may read through it."""
        return self._inner

    def holdout_start(self, symbol: Symbol, timeframe: Timeframe) -> datetime | None:
        return self._policy.holdout_start(self._inner, symbol, timeframe)

    def looks(self, symbol: Symbol, timeframe: Timeframe) -> list[dict]:
        return self._policy.looks(self._inner, symbol, timeframe)

    def record_look(self, symbol: Symbol, timeframe: Timeframe, look: dict) -> None:
        self._policy.record_look(self._inner, symbol, timeframe, look)

    def history(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        held = self.holdout_start(instrument.symbol, timeframe)
        if held is None:
            return self._inner.history(instrument, timeframe, start, end)
        if end > held:
            raise HoldoutViolation(
                f"{instrument.symbol.ticker} {timeframe}: requested data through {end:%Y-%m-%d} "
                f"but the holdout starts {held:%Y-%m-%d}; research may not read it (ADR-0012)"
            )
        return [b for b in self._inner.history(instrument, timeframe, start, end)
                if b.timestamp < held]


def base_timeframe(backtest) -> Timeframe:
    """The Base Timeframe a backtest will run on: its finest declared timeframe, else the
    finest an Indicator subscribes to (as ``Backtest.run`` derives it)."""
    frames = set(backtest.timeframes)
    if not frames:
        strategy = backtest.strategy
        strategy._params = dict(backtest.params)
        strategy.run_setup()
        frames = {ind.timeframe for ind in strategy.registered_indicators}
    if not frames:
        raise ValueError("No timeframes: set Backtest.timeframes or declare an Indicator.")
    return min(frames)
