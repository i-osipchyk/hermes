"""Local fetch-once Parquet cache for normalized bars.

Bars are persisted per (Instrument, timeframe) so repeated backtests over the same
span read from disk and only missing date ranges hit the provider. The cache is
just files — easy to inspect, delete, or version.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ..core import Bar, Instrument, Timeframe

DEFAULT_CACHE_DIR = Path(".hermes_cache/bars")
_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


class BarCache:
    def __init__(self, root: Path = DEFAULT_CACHE_DIR) -> None:
        self.root = Path(root)

    def _path(self, instrument: Instrument, timeframe: Timeframe) -> Path:
        sym = instrument.symbol
        return self.root / sym.source / f"{sym.ticker}_{timeframe}.parquet"

    def _load(self, instrument: Instrument, timeframe: Timeframe):
        import pandas as pd

        path = self._path(instrument, timeframe)
        if not path.exists():
            return pd.DataFrame(columns=_COLUMNS)
        return pd.read_parquet(path)

    def read(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        df = self._load(instrument, timeframe)
        if df.empty:
            return []
        s, e = _epoch(start), _epoch(end)
        rows = df[(df["ts"] >= s) & (df["ts"] <= e)].sort_values("ts")
        return [
            Bar(
                datetime.fromtimestamp(r.ts, tz=UTC),
                timeframe,
                r.open,
                r.high,
                r.low,
                r.close,
                r.volume,
            )
            for r in rows.itertuples()
        ]

    def missing_ranges(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        df = self._load(instrument, timeframe)
        if df.empty:
            return [(start, end)]
        cached_min = datetime.fromtimestamp(df["ts"].min(), tz=UTC)
        cached_max = datetime.fromtimestamp(df["ts"].max(), tz=UTC)
        gaps: list[tuple[datetime, datetime]] = []
        if start < cached_min:
            gaps.append((start, min(cached_min, end)))
        if end > cached_max:
            gaps.append((max(cached_max, start), end))
        return gaps

    def write(self, instrument: Instrument, timeframe: Timeframe, bars: list[Bar]) -> None:
        if not bars:
            return
        import pandas as pd

        new = pd.DataFrame(
            {
                "ts": [_epoch(b.timestamp) for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            }
        )
        merged = (
            pd.concat([self._load(instrument, timeframe), new], ignore_index=True)
            .drop_duplicates(subset="ts", keep="last")
            .sort_values("ts")
        )
        path = self._path(instrument, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(path, index=False)


def _epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())
