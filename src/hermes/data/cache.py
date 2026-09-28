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

    def _nodata_path(self, instrument: Instrument, timeframe: Timeframe) -> Path:
        sym = instrument.symbol
        return self.root / sym.source / f"{sym.ticker}_{timeframe}.nodata"

    def mark_nodata(self, instrument: Instrument, timeframe: Timeframe) -> None:
        path = self._nodata_path(instrument, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def is_nodata(self, instrument: Instrument, timeframe: Timeframe) -> bool:
        return self._nodata_path(instrument, timeframe).exists()

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
        """Gaps in [start, end] not covered by cached bars.

        Scans the cached timestamps that actually fall inside [start, end], not
        just the cache's global min/max. Two disjoint fetches (e.g. a recent
        window fetched first, an older window fetched later) otherwise leave a
        hole in the middle that looked "covered" purely because *some* row
        existed before it and *some* row existed after it -- ``read()`` then
        silently returned a sparse slice instead of the full requested range,
        with no error or warning.
        """
        df = self._load(instrument, timeframe)
        tf_seconds = timeframe.seconds if hasattr(timeframe, "seconds") else 86_400
        if df.empty:
            return [(start, end)]
        s, e = _epoch(start), _epoch(end)
        in_range = df.loc[(df["ts"] >= s) & (df["ts"] <= e), "ts"]
        ts_in_range = sorted(in_range.tolist())

        gaps: list[tuple[datetime, datetime]] = []
        cursor = s
        for t in ts_in_range:
            # Skip gaps narrower than the timeframe's bar width: no new bar can
            # fit inside them, so fetching would always return empty.
            if t - cursor > tf_seconds:
                gaps.append((datetime.fromtimestamp(cursor, tz=UTC), datetime.fromtimestamp(t, tz=UTC)))
            cursor = max(cursor, t)
        if e - cursor >= tf_seconds:
            gaps.append((datetime.fromtimestamp(cursor, tz=UTC) if cursor != s else start, end))
        return gaps

    def write(self, instrument: Instrument, timeframe: Timeframe, bars: list[Bar]) -> None:
        if not bars:
            return
        import fcntl
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
        path = self._path(instrument, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive lock prevents concurrent processes from interleaving their
        # read-modify-write cycles.  Atomic rename ensures readers never see a
        # partially-written file.
        lock_path = path.with_suffix(".lock")
        with open(lock_path, "w") as _lf:
            fcntl.flock(_lf, fcntl.LOCK_EX)
            merged = (
                pd.concat([self._load(instrument, timeframe), new], ignore_index=True)
                .drop_duplicates(subset="ts", keep="last")
                .sort_values("ts")
            )
            tmp = path.with_suffix(".parquet.tmp")
            merged.to_parquet(tmp, index=False)
            tmp.rename(path)


def _epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())
