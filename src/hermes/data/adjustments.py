"""Raw bars + corporate-action events, adjusted at read time (ADR-0016, audit F16).

Providers hand back bars already adjusted to *today's* basis and silently restate the
whole history whenever a new split or dividend happens. Caching those and fetching only
missing ranges stitches two bases together and fabricates a gap at the seam. Instead the
cache holds what was actually printed (raw bars never change) and a small table of dated
events; adjustment is applied when bars are read, so a new dividend adds one row and
rewrites nothing.

Conventions (match Yahoo / Tiingo):
  * ``split``  — ratio ``new_shares / old_shares`` (2.0 for a 2-for-1). Prices before the
    ex-date are divided by it, volume multiplied.
  * ``dividend`` — cash per share, in the basis printed on the ex-date; ``prev_close`` —
    the raw close of the last bar before it. Total-return adjustment multiplies earlier
    prices by ``1 - dividend / prev_close``. The ratio is basis-invariant, so a later split
    does not change it.
  * a bar is "before" an event when its (UTC) date is earlier than the ex-date.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core import Bar, Timeframe

ADJUST_MODES = ("total", "split", "none")


@dataclass(frozen=True)
class AdjustmentEvent:
    ex_date: date
    split: float = 1.0
    dividend: float = 0.0
    prev_close: float | None = None

    @property
    def dividend_factor(self) -> float:
        """Multiplier for earlier prices; 1.0 when it cannot be computed honestly."""
        if self.dividend > 0 and self.prev_close and self.prev_close > self.dividend:
            return 1.0 - self.dividend / self.prev_close
        return 1.0

    def to_json(self) -> dict[str, Any]:
        return {"ex_date": self.ex_date.isoformat(), "split": self.split,
                "dividend": self.dividend, "prev_close": self.prev_close}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> AdjustmentEvent:
        return cls(date.fromisoformat(d["ex_date"]), float(d.get("split", 1.0)),
                   float(d.get("dividend", 0.0)), d.get("prev_close"))


def adjust_bars(bars: list[Bar], events: list[AdjustmentEvent], mode: str) -> list[Bar]:
    """Apply ``events`` to raw ``bars``. ``mode``: ``"total"`` (splits + dividends),
    ``"split"`` (splits only — keeps real ex-dividend gaps) or ``"none"``."""
    if mode not in ADJUST_MODES:
        raise ValueError(f"adjust must be one of {ADJUST_MODES}, got {mode!r}")
    if mode == "none" or not events or not bars:
        return bars
    evs = sorted(events, key=lambda e: e.ex_date)
    dates = [e.ex_date for e in evs]
    n = len(evs)
    # suffix[i] = product over events i..n-1
    split_sfx = [1.0] * (n + 1)
    div_sfx = [1.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        split_sfx[i] = split_sfx[i + 1] * (evs[i].split or 1.0)
        div_sfx[i] = div_sfx[i + 1] * evs[i].dividend_factor
    out: list[Bar] = []
    for b in bars:
        i = bisect_right(dates, b.timestamp.date())   # events with ex_date > bar date
        s = split_sfx[i]
        m = div_sfx[i] if mode == "total" else 1.0
        if s == 1.0 and m == 1.0:
            out.append(b)
            continue
        k = m / s
        out.append(Bar(b.timestamp, b.timeframe, b.open * k, b.high * k, b.low * k,
                       b.close * k, b.volume * s))
    return out


def yfinance_rows_to_raw(
    rows: list[dict[str, Any]], timeframe: Timeframe, splits: list[tuple[date, float]],
) -> tuple[list[Bar], list[AdjustmentEvent]]:
    """Turn yfinance rows (``auto_adjust=False``) into truly raw bars + events.

    Yahoo's unadjusted prices, volume and dividends are already restated for splits as of
    *now*, so they move whenever a new split happens. Multiplying prices (dividing volume)
    by the product of every split with a later ex-date undoes that and gives what was
    printed, which never changes. ``rows`` are dicts with ``ts open high low close volume
    dividend``; ``splits`` is the ticker's full split history.
    """
    rows = sorted(rows, key=lambda r: r["ts"])
    split_dates = sorted(splits)

    def future_splits(d: date) -> float:
        k = 1.0
        for ex, ratio in split_dates:
            if ex > d:
                k *= ratio
        return k

    bars: list[Bar] = []
    events: dict[date, AdjustmentEvent] = {ex: AdjustmentEvent(ex, split=r)
                                           for ex, r in split_dates}
    prev_close: float | None = None
    for r in rows:
        d = r["ts"].date()
        k = future_splits(d)
        close = float(r["close"]) * k
        bars.append(Bar(r["ts"], timeframe, float(r["open"]) * k, float(r["high"]) * k,
                        float(r["low"]) * k, close, float(r["volume"]) / k if k else 0.0))
        div = float(r.get("dividend") or 0.0) * k
        if div > 0:
            old = events.get(d)
            events[d] = AdjustmentEvent(d, split=old.split if old else 1.0, dividend=div,
                                        prev_close=prev_close)
        prev_close = close
    return bars, sorted(events.values(), key=lambda e: e.ex_date)


def tiingo_rows_to_raw(
    rows: list[dict[str, Any]], timeframe: Timeframe,
) -> tuple[list[Bar], list[AdjustmentEvent]]:
    """Raw bars + events from Tiingo EOD rows. Uses ``open/high/low/close/volume`` (what
    printed) and ``divCash`` / ``splitFactor`` (the events), never the ``adj*`` fields."""
    from datetime import UTC

    bars: list[Bar] = []
    events: list[AdjustmentEvent] = []
    prev_close: float | None = None
    for row in sorted(rows, key=lambda r: r.get("date", "")):
        ts = datetime.fromisoformat(row.get("date", ""))
        ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
        o, h, lo, c = (row.get(k) for k in ("open", "high", "low", "close"))
        if None in (o, h, lo, c):
            continue
        split = float(row.get("splitFactor") or 1.0)
        div = float(row.get("divCash") or 0.0)
        if split != 1.0 or div > 0:
            events.append(AdjustmentEvent(ts.date(), split=split, dividend=div,
                                          prev_close=prev_close))
        bars.append(Bar(ts, timeframe, float(o), float(h), float(lo), float(c),
                        float(row.get("volume") or 0.0)))
        prev_close = float(c)
    return bars, events


class AdjustmentStore:
    """Per-(source, ticker) event tables as small JSON files under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, source: str, ticker: str) -> Path:
        return self.root / source / f"{ticker}.json"

    def load(self, source: str, ticker: str) -> list[AdjustmentEvent]:
        p = self._path(source, ticker)
        if not p.exists():
            return []
        evs = [AdjustmentEvent.from_json(d) for d in json.loads(p.read_text())]
        return sorted(evs, key=lambda e: e.ex_date)

    def merge(self, source: str, ticker: str, new: list[AdjustmentEvent]) -> None:
        """Add/replace events by ex-date. A known ``prev_close`` is never lost to a fetch
        that started on the ex-date and could not see the prior bar."""
        if not new:
            return
        merged = {e.ex_date: e for e in self.load(source, ticker)}
        for e in new:
            old = merged.get(e.ex_date)
            if old is not None and e.prev_close is None and old.prev_close is not None:
                e = AdjustmentEvent(e.ex_date, e.split, e.dividend, old.prev_close)
            merged[e.ex_date] = e
        p = self._path(source, ticker)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps([e.to_json() for e in
                                   sorted(merged.values(), key=lambda e: e.ex_date)]))
        tmp.rename(p)

    def digest(self, source: str, ticker: str) -> str:
        """Short hash of the event table: it changes exactly when a run's adjusted data
        would (a new split/dividend, or a prior close that became known)."""
        blob = json.dumps([e.to_json() for e in self.load(source, ticker)], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


# Daily history is padded back this far on a fetch so a dividend on the first requested
# day can still see the bar before it.
FETCH_PAD = timedelta(days=10)
