"""ConstituentCalendar: point-in-time index membership for bias-free universe backtests.

Survivorship bias arises when a backtest's universe is fixed to *today's* index
members.  Stocks that were delisted, went bankrupt, or were removed look
artificially good in hindsight because they are absent from the test.

``ConstituentCalendar`` solves this by tracking which symbols were in an index at
each historical snapshot — so a backtest in 2018 only sees the stocks that were
actually tradeable in 2018.

Data formats
------------
**Snapshot CSV** (recommended): one row per rebalancing event, columns::

    date,tickers
    2020-01-01,"AAPL,MSFT,AMZN,..."
    2020-04-01,"AAPL,MSFT,GOOGL,..."   # AMZN removed, GOOGL added

The calendar interpolates: any date between two rows uses the earlier row's
membership list.

**Changes CSV**: one row per addition/removal, columns::

    date,ticker,action
    2020-01-15,GOOGL,add
    2020-03-01,AMZN,remove

Free S&P 500 constituent history
---------------------------------
A community-maintained snapshot dataset is available at:
    https://github.com/fja05680/sp500
    (file: ``S&P 500 Historical Components & Changes.csv``)

The Tiingo ``/tickers`` API endpoint also exposes constituent data for
supported indices (requires a Tiingo API key).
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path


class ConstituentCalendar:
    """Point-in-time index membership calendar.

    Build from a snapshot CSV or a changes CSV, then query which symbols
    were in the index on any historical date.

    Usage::

        cal = ConstituentCalendar.from_snapshot_csv("sp500_history.csv")

        # Who was in the index on a specific date?
        tickers = cal.symbols_on(datetime(2018, 6, 1))

        # What is the full set ever present during a period (for UniverseBacktest)?
        universe = cal.ever_member(start, end)

        # When was AAPL actively in the index during the period?
        first, last = cal.membership_window("AAPL", start, end)
    """

    def __init__(self, snapshots: list[tuple[date, list[str]]]) -> None:
        """
        Args:
            snapshots: List of (date, [tickers]) pairs.  Need not be sorted.
                       Each entry is a full membership snapshot at a rebalancing date.
        """
        if not snapshots:
            raise ValueError("ConstituentCalendar requires at least one snapshot.")
        self._snapshots: list[tuple[date, list[str]]] = sorted(
            snapshots, key=lambda x: x[0]
        )

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_snapshot_csv(cls, path: str | Path) -> ConstituentCalendar:
        """Load from a snapshot CSV with columns ``date`` and ``tickers``.

        The ``tickers`` column is a comma-separated list of ticker symbols,
        optionally quoted.  Each row represents a full membership snapshot at
        a rebalancing date.

        Compatible with the fja05680/sp500 dataset format::

            date,tickers
            2010-01-04,"A,AA,AAPL,..."
            2010-04-30,"A,AAPL,AMZN,..."
        """
        path = Path(path)
        snapshots: list[tuple[date, list[str]]] = []
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                d = date.fromisoformat(row["date"].strip())
                raw = row["tickers"].strip().strip('"')
                tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
                if tickers:
                    snapshots.append((d, tickers))
        if not snapshots:
            raise ValueError(f"No data found in {path}")
        return cls(snapshots)

    @classmethod
    def from_changes_csv(cls, path: str | Path) -> ConstituentCalendar:
        """Load from a changes CSV with columns ``date``, ``ticker``, ``action``.

        ``action`` must be ``add`` or ``remove``.  Builds internal snapshots by
        replaying the change stream from the first event forward::

            date,ticker,action
            2010-01-15,GOOGL,add
            2010-03-01,ENRN,remove
        """
        path = Path(path)
        events: list[tuple[date, str, str]] = []
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                d = date.fromisoformat(row["date"].strip())
                ticker = row["ticker"].strip().upper()
                action = row["action"].strip().lower()
                if action not in ("add", "remove"):
                    raise ValueError(
                        f"Unknown action {action!r} in {path} at {d}; "
                        "expected 'add' or 'remove'."
                    )
                events.append((d, ticker, action))
        events.sort(key=lambda x: x[0])

        current: set[str] = set()
        snapshots: list[tuple[date, list[str]]] = []
        prev_date: date | None = None
        for d, ticker, action in events:
            if prev_date is not None and d != prev_date:
                snapshots.append((prev_date, sorted(current)))
            if action == "add":
                current.add(ticker)
            else:
                current.discard(ticker)
            prev_date = d
        if current and prev_date is not None:
            snapshots.append((prev_date, sorted(current)))
        if not snapshots:
            raise ValueError(f"No data found in {path}")
        return cls(snapshots)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def symbols_on(self, when: datetime | date) -> list[str]:
        """Return ticker symbols in the index on ``when``.

        Uses the most-recent snapshot at or before ``when``.  Returns an empty
        list when ``when`` precedes all snapshots (no information available for
        that date).
        """
        d: date = when.date() if isinstance(when, datetime) else when
        result: list[str] = []
        for snap_date, tickers in self._snapshots:
            if snap_date <= d:
                result = tickers
            else:
                break
        return list(result)

    def ever_member(self, start: datetime | date, end: datetime | date) -> list[str]:
        """Return the union of all tickers present in the index during ``[start, end]``.

        This is the full universe a bias-free backtest must cover — it includes
        stocks that joined mid-period (no look-ahead) and stocks that were removed
        or delisted (no survivorship bias).
        """
        s: date = start.date() if isinstance(start, datetime) else start
        e: date = end.date() if isinstance(end, datetime) else end

        seen: set[str] = set()
        # Members active at the start of the window.
        seen.update(self.symbols_on(s))
        # Members added in subsequent snapshots within (s, e].
        for snap_date, tickers in self._snapshots:
            if snap_date <= s:
                continue
            if snap_date > e:
                break
            seen.update(tickers)
        return sorted(seen)

    def membership_window(
        self,
        ticker: str,
        start: datetime | date,
        end: datetime | date,
    ) -> tuple[date, date] | None:
        """Return the ``(first_seen, last_seen)`` dates for ``ticker`` in ``[start, end]``.

        ``first_seen`` is the earliest date within the window where the calendar
        confirms the ticker was a constituent.  ``last_seen`` is the latest such date —
        estimated conservatively as one day before the next rebalancing snapshot
        after the ticker's last known appearance (or ``end`` if still a member).

        Returns ``None`` if the ticker never appears in the window.

        Use these dates to set the ``start``/``end`` of a ``Backtest`` leg so the
        strategy only trades the stock during its confirmed membership period.
        """
        s: date = start.date() if isinstance(start, datetime) else start
        e: date = end.date() if isinstance(end, datetime) else end
        t = ticker.upper()

        # Collect all snapshot dates where ticker appears.
        all_appearances: list[date] = [
            snap_date for snap_date, tickers in self._snapshots if t in tickers
        ]
        if not all_appearances:
            return None

        # --- first_seen ---------------------------------------------------
        # If already a member at s (snapshot at/before s has the ticker), use s.
        if t in self.symbols_on(s):
            first_seen: date = s
        else:
            # First snapshot after s that contains the ticker.
            later = [d for d in all_appearances if d > s]
            if not later or later[0] > e:
                return None
            first_seen = later[0]

        # --- last_seen ----------------------------------------------------
        if t in self.symbols_on(e):
            # Still a member at the end of the window.
            last_seen: date = e
        else:
            # Last snapshot within [s, e] that contains the ticker.
            past = [d for d in all_appearances if d <= e]
            if not past:
                return None
            last_known = past[-1]
            if last_known < first_seen:
                return None

            # Estimate the removal date as one day before the next snapshot.
            all_snap_dates = [d for d, _ in self._snapshots]
            next_snaps = [d for d in all_snap_dates if d > last_known]
            if next_snaps:
                last_seen = min(next_snaps[0] - timedelta(days=1), e)
            else:
                last_seen = min(last_known, e)
            last_seen = max(last_seen, first_seen)

        return (first_seen, last_seen)
