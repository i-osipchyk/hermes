#!/usr/bin/env python3
"""One-off backfill for BarCache's ".checked" coverage store.

``BarCache.missing_ranges`` was fixed in d00d420 to detect real gaps *inside*
already-cached data (e.g. a disjoint older/newer fetch leaving a silent hole).
As a side effect it also re-flags every weekend/holiday in existing daily (or
any non-continuous-trading) caches as "missing", so every run re-fetches those
same closures from the provider forever -- see ``BarCache.mark_checked``.

New fetches record their range via ``mark_checked`` automatically going
forward. This script does the same for data that's *already* cached, so
existing caches (e.g. a full SP500 universe) don't have to pay for that one
extra pass live.

Only marks gaps up to CLOSURE_CAP_SECONDS wide. Anything wider is left alone
and reported -- it might be a genuine missing chunk (the disjoint-fetch bug
the commit was fixing) rather than a calendar closure, and should still go
through a real fetch to be resolved.

Usage: python scripts/backfill_checked_ranges.py [cache_root]
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from hermes.core import Symbol, Timeframe
from hermes.data import BarCache

CLOSURE_CAP_SECONDS = 5 * 86_400  # widest plausible market closure (long holiday weekend)


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".hermes_cache/bars")
    if not root.exists():
        print(f"No cache at {root}")
        return

    cache = BarCache(root)
    marked = 0
    skipped_large: list[str] = []

    for parquet_path in sorted(root.glob("*/*.parquet")):
        source = parquet_path.parent.name
        ticker, _, tf_str = parquet_path.stem.rpartition("_")
        if not ticker:
            print(f"Skipping unrecognised filename: {parquet_path}")
            continue
        try:
            timeframe = Timeframe.parse(tf_str)
        except ValueError:
            print(f"Skipping unrecognised timeframe in: {parquet_path}")
            continue

        df = pd.read_parquet(parquet_path)
        if df.empty:
            continue
        ts = sorted(df["ts"].tolist())
        tf_seconds = timeframe.seconds
        instrument = SimpleNamespace(symbol=Symbol(ticker, source))

        cursor = ts[0]
        for t in ts[1:]:
            gap = t - cursor
            if gap > tf_seconds:
                gap_start, gap_end = (
                    datetime.fromtimestamp(cursor, tz=UTC),
                    datetime.fromtimestamp(t, tz=UTC),
                )
                if gap <= CLOSURE_CAP_SECONDS:
                    cache.mark_checked(instrument, timeframe, gap_start, gap_end)
                    marked += 1
                else:
                    skipped_large.append(f"{source}/{ticker}_{tf_str}: {gap_start} .. {gap_end}")
            cursor = max(cursor, t)

    print(f"Marked {marked} calendar-closure gap(s) as checked across {root}.")
    if skipped_large:
        print(
            f"\nLeft {len(skipped_large)} larger gap(s) alone (> {CLOSURE_CAP_SECONDS // 86_400}d) "
            "-- these will still be fetched live, since they may be real missing data:"
        )
        for line in skipped_large:
            print(f"  {line}")


if __name__ == "__main__":
    main()
