"""Forming-bar aggregation — the heart of the multi-timeframe engine (ADR-0002).

The engine steps one **Base Timeframe** bar at a time. On each base step, every
higher subscribed Timeframe is updated: its current bar is a **Forming Bar**
(``is_closed=False``) built from the base bars seen so far —

    open   = first sub-bar's open
    high   = running max of sub-bar highs
    low    = running min of sub-bar lows
    close  = most recent completed sub-bar's close
    volume = running sum
    is_closed = False

When the Base clock crosses the higher Timeframe's boundary the bar is frozen
(``is_closed=True``) and appended, and a fresh Forming Bar takes the current slot.

Bucketing rule: higher-TF boundaries anchor to **wall-clock/calendar boundaries in
the Instrument's exchange-local timezone**, but are **session-bounded** for
intraday/daily Timeframes — a bar never spans an overnight/weekend gap, so the
first bar of a session is partial and a bar force-closes at session end.

Weekly Timeframes span multiple sessions and therefore roll on the week boundary
(their ``is_closed`` flips at the first base bar of the next week rather than
exactly at the last session close — a documented, minor limitation).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from ..core import Bar, Instrument, SessionCalendar, Timeframe

_UTC = UTC
_DAY = 86_400
_WEEK = 604_800


def _local_midnight(local_dt: datetime) -> datetime:
    return local_dt.replace(hour=0, minute=0, second=0, microsecond=0)


def bucket_bounds(
    ts_utc: datetime, timeframe: Timeframe, session: SessionCalendar
) -> tuple[datetime, datetime]:
    """Return ``(open_utc, close_utc)`` of the higher-TF bucket ``ts_utc`` falls in.

    * intraday (< 1 day): wall-clock-anchored within the local day, close clamped to
      the session close (so the last bar of a session may be short and the first is
      partial);
    * daily (== 1 day): one bucket per trading day, close = session close;
    * weekly (multiples of 1 week): anchored to the local Monday, no session clamp.
    """
    local = session.to_local(ts_utc)

    if timeframe.seconds % _WEEK == 0:
        monday = _local_midnight(local) - timedelta(days=local.weekday())
        open_local = monday
        close_local = open_local + timedelta(seconds=timeframe.seconds)
        return open_local.astimezone(_UTC), close_local.astimezone(_UTC)

    midnight = _local_midnight(local)
    idx = int((local - midnight).total_seconds() // timeframe.seconds)
    open_local = midnight + timedelta(seconds=idx * timeframe.seconds)
    close_local = open_local + timedelta(seconds=timeframe.seconds)
    open_utc = open_local.astimezone(_UTC)
    close_utc = close_local.astimezone(_UTC)

    if timeframe.seconds <= _DAY:
        session_close = session.session_close_utc(ts_utc)
        if session_close is not None and session_close < close_utc:
            close_utc = session_close
    return open_utc, close_utc


class TimeframeSeries:
    """The closed-bar history plus the current Forming Bar for one Timeframe.

    The Forming Bar occupies the current slot and mutates on each base step;
    indicators read :meth:`bars_for_compute` and include the Forming Bar as their
    latest data point (parity-safe "repaint").
    """

    def __init__(self, instrument: Instrument, timeframe: Timeframe) -> None:
        self.instrument = instrument
        self.timeframe = timeframe
        self._closed: list[Bar] = []
        self._forming: Bar | None = None

    @property
    def forming(self) -> Bar | None:
        return self._forming

    def closed(self) -> list[Bar]:
        return self._closed

    def current(self) -> Bar | None:
        """The bar a strategy 'sees now' — the Forming Bar if any, else last closed."""
        return self._forming or (self._closed[-1] if self._closed else None)

    def bars_for_compute(self) -> list[Bar]:
        """Closed history plus the Forming Bar — what an Indicator computes over."""
        if self._forming is None:
            return self._closed
        return [*self._closed, self._forming]

    def __len__(self) -> int:
        return len(self._closed) + (1 if self._forming else 0)


class MultiTimeframeView:
    """Aggregates a single stream of Base bars into all subscribed Timeframes.

    Feed base bars in order via :meth:`push`; read per-timeframe series back. This
    is the object a Strategy queries (directly or via helpers) inside ``on_bar``.
    """

    def __init__(
        self, instrument: Instrument, base: Timeframe, timeframes: list[Timeframe]
    ) -> None:
        for tf in timeframes:
            if not tf.is_multiple_of(base):
                raise ValueError(f"{tf} is not an integer multiple of base {base}")
        self.instrument = instrument
        self.base = base
        self.series: dict[Timeframe, TimeframeSeries] = {
            tf: TimeframeSeries(instrument, tf) for tf in sorted({base, *timeframes})
        }

    def push(self, base_bar: Bar) -> None:
        """Advance one base step: append the base bar, then update/roll every higher
        Timeframe's Forming Bar, force-closing at bucket/session boundaries."""
        self.series[self.base]._closed.append(base_bar)

        base_end = base_bar.timestamp + timedelta(seconds=self.base.seconds)
        for tf, series in self.series.items():
            if tf == self.base:
                continue
            open_utc, close_utc = bucket_bounds(base_bar.timestamp, tf, self.instrument.session)

            forming = series._forming
            # Roll safety net: a forming bar from an earlier bucket (e.g. across a
            # gap) is finalised before the new one starts.
            if forming is not None and forming.timestamp != open_utc:
                self._finalize(series)
                forming = None

            if forming is None:
                forming = Bar(
                    timestamp=open_utc,
                    timeframe=tf,
                    open=base_bar.open,
                    high=base_bar.high,
                    low=base_bar.low,
                    close=base_bar.close,
                    volume=base_bar.volume,
                    is_closed=False,
                )
            else:
                forming = replace(
                    forming,
                    high=max(forming.high, base_bar.high),
                    low=min(forming.low, base_bar.low),
                    close=base_bar.close,
                    volume=forming.volume + base_bar.volume,
                )
            series._forming = forming

            # Eager finalise: this base bar completes the bucket (or hits session end).
            if base_end >= close_utc:
                self._finalize(series)

    @staticmethod
    def _finalize(series: TimeframeSeries) -> None:
        if series._forming is None:
            return
        series._closed.append(replace(series._forming, is_closed=True))
        series._forming = None

    def __getitem__(self, timeframe: Timeframe) -> TimeframeSeries:
        return self.series[timeframe]
