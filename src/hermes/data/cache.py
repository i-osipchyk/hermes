"""Local fetch-once Parquet cache for normalized bars.

Bars are persisted per (Instrument, timeframe) so repeated backtests over the same
span read from disk and only missing date ranges hit the provider. The cache is
just files — easy to inspect, delete, or version.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core import Bar, Instrument, Timeframe

DEFAULT_CACHE_DIR = Path(".hermes_cache/bars")
_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def complete_bars(
    bars: list[Bar], timeframe: Timeframe, now: datetime | None = None
) -> list[Bar]:
    """Drop a trailing bar whose interval has not finished yet.

    Providers return the *currently forming* bar alongside closed ones. Cached as
    if it were closed, its open/high/low/close are a partial snapshot that never
    gets corrected -- every later backtest then reads a bar that never existed.
    """
    if not bars:
        return bars
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=timeframe.seconds)
    return [b for b in bars if b.timestamp <= cutoff]


def checked_through(
    bars: list[Bar], timeframe: Timeframe, gap_start: datetime, gap_end: datetime
) -> datetime:
    """How much of ``[gap_start, gap_end]`` a fetch may honestly mark as checked.

    ``mark_checked`` suppresses re-fetching forever, so marking the whole gap when
    the fetch only covered part of it burns a permanent hole in the cache. That is
    exactly what happened when a synthesised timeframe (cTrader rebuilding 4h from
    1h) discarded its still-forming trailing bucket, or when a provider withheld
    the in-progress final bar.

    An empty result legitimately means "nothing here" (a weekend, a holiday), so
    the full gap is marked in that case -- that is what ``mark_checked`` is for.
    """
    if not bars:
        return gap_end
    last_close = bars[-1].timestamp + timedelta(seconds=timeframe.seconds)
    return min(gap_end, max(gap_start, last_close))


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

    def _checked_path(self, instrument: Instrument, timeframe: Timeframe) -> Path:
        sym = instrument.symbol
        return self.root / sym.source / f"{sym.ticker}_{timeframe}.checked.json"

    def _load_checked(self, instrument: Instrument, timeframe: Timeframe) -> list[tuple[int, int]]:
        import json

        path = self._checked_path(instrument, timeframe)
        if not path.exists():
            return []
        return [(int(a), int(b)) for a, b in json.loads(path.read_text())]

    def mark_checked(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> None:
        """Record [start, end] as fetched-and-confirmed, even if it held no bars.

        A gap can be legitimately empty (a weekend, a market holiday) and will
        always come back empty from the provider. Without this, ``missing_ranges``
        -- which otherwise only knows about actual bar rows -- re-flags that same
        empty span as "missing" on every subsequent call and re-fetches it.
        """
        import json

        path = self._checked_path(instrument, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(".lock")
        with _exclusive_lock(lock_path):
            intervals = self._load_checked(instrument, timeframe)
            intervals.append((_epoch(start), _epoch(end)))
            merged = _merge_intervals(intervals)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(merged))
            tmp.rename(path)

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
            gaps = [(start, end)]
            checked = self._load_checked(instrument, timeframe)
            return _subtract_checked(gaps, checked) if checked else gaps
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
        if e - cursor > tf_seconds:
            gaps.append((datetime.fromtimestamp(cursor, tz=UTC) if cursor != s else start, end))
        checked = self._load_checked(instrument, timeframe)
        return _subtract_checked(gaps, checked) if checked else gaps

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
        path = self._path(instrument, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive lock prevents concurrent processes from interleaving their
        # read-modify-write cycles.  Atomic rename ensures readers never see a
        # partially-written file.
        lock_path = path.with_suffix(".lock")
        with _exclusive_lock(lock_path):
            existing = self._load(instrument, timeframe)
            # Concatenating an all-empty frame is deprecated in pandas and changes
            # the result dtypes; skip it when there is nothing on disk yet.
            frames = [f for f in (existing, new) if not f.empty]
            merged = (
                pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
            )
            merged = merged.drop_duplicates(subset="ts", keep="last").sort_values("ts")
            tmp = path.with_suffix(".parquet.tmp")
            merged.to_parquet(tmp, index=False)
            tmp.rename(path)


@contextlib.contextmanager
def _exclusive_lock(lock_path: Path):
    """Cross-process exclusive lock around a cache read-modify-write.

    ``fcntl`` is POSIX-only. On a platform without it the critical section still
    runs -- single-process use (the normal case) is unaffected; only concurrent
    multi-process writes lose their guard.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        fcntl = None
    with open(lock_path, "w") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield handle


def _epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for s, e in ordered[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def _subtract_checked(
    gaps: list[tuple[datetime, datetime]], checked: list[tuple[int, int]]
) -> list[tuple[datetime, datetime]]:
    """Trim already-checked (fetched-and-confirmed, possibly empty) spans out of gaps."""
    result: list[tuple[datetime, datetime]] = []
    for gap_start, gap_end in gaps:
        cursor, e = _epoch(gap_start), _epoch(gap_end)
        for cs, ce in checked:
            if ce <= cursor:
                continue
            if cs >= e:
                break
            if cs > cursor:
                result.append((
                    datetime.fromtimestamp(cursor, tz=UTC),
                    datetime.fromtimestamp(min(cs, e), tz=UTC),
                ))
            cursor = max(cursor, ce)
            if cursor >= e:
                break
        if cursor < e:
            result.append((datetime.fromtimestamp(cursor, tz=UTC), datetime.fromtimestamp(e, tz=UTC)))
    return result
