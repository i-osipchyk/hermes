"""Ticker lists ("universes") for multi-symbol backtests.

Two kinds of universe:

* **Static** — JSON files in ``./tickers/``.  Each is either a bare array of tickers,
  or ``{"source": "<name>", "tickers": [...]}`` to pin the list to a data source.

* **Calendar** — backed by a :class:`~hermes.data.ConstituentCalendar` CSV in the
  working directory.  The UI runs these via
  :class:`~hermes.backtest.UniverseBacktest` so each leg is clipped to the symbol's
  point-in-time membership window (no survivorship bias, no look-ahead).

  Registered calendar universes (CSV must exist in cwd):

  +---------+----------------------+
  | Name    | CSV file             |
  +=========+======================+
  | sp500   | sp500_history.csv    |
  +---------+----------------------+
"""

from __future__ import annotations

import json
from pathlib import Path

TICKERS_DIR = Path("tickers")

# Calendar-backed universes: display name → CSV filename (resolved in cwd).
_CALENDAR_CSVS: dict[str, str] = {
    "sp500": "sp500_history.csv",
}


# ---------------------------------------------------------------------------
# Static JSON universes
# ---------------------------------------------------------------------------

def universe_names(directory: Path = TICKERS_DIR) -> list[str]:
    directory = Path(directory)
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def load_universe(name: str, directory: Path = TICKERS_DIR) -> tuple[str | None, list[str]]:
    """Return ``(source_name_or_None, tickers)`` for a static JSON universe."""
    data = json.loads((Path(directory) / f"{name}.json").read_text())
    if isinstance(data, list):
        return None, [str(t) for t in data]
    return data.get("source"), [str(t) for t in data.get("tickers", [])]


# ---------------------------------------------------------------------------
# Calendar universes
# ---------------------------------------------------------------------------

def calendar_universe_names() -> list[str]:
    """Return names of calendar universes whose CSV file exists in the cwd."""
    return [name for name, csv in _CALENDAR_CSVS.items() if Path(csv).exists()]


def is_calendar_universe(name: str) -> bool:
    return name in _CALENDAR_CSVS and Path(_CALENDAR_CSVS[name]).exists()


def load_calendar(name: str):
    """Return a :class:`~hermes.data.ConstituentCalendar` for the named universe."""
    from hermes.data import ConstituentCalendar
    return ConstituentCalendar.from_snapshot_csv(Path(_CALENDAR_CSVS[name]))
