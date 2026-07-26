"""Ticker lists ("universes") for multi-symbol backtests.

JSON files in ``./tickers/`` (relative to where hermes-ui is launched). Each is either
a bare array of tickers, or ``{"source": "<name>", "tickers": [...]}`` to pin the list to
a data source (e.g. an S&P list to yfinance). The UI offers these lists in the symbol
selector alongside a free-text single symbol.
"""

from __future__ import annotations

import json
from pathlib import Path

TICKERS_DIR = Path("tickers")


def universe_names(directory: Path = TICKERS_DIR) -> list[str]:
    directory = Path(directory)
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def load_universe(name: str, directory: Path = TICKERS_DIR) -> tuple[str | None, list[str]]:
    """Return ``(source_name_or_None, tickers)`` for the named list."""
    data = json.loads((Path(directory) / f"{name}.json").read_text())
    if isinstance(data, list):
        return None, [str(t) for t in data]
    return data.get("source"), [str(t) for t in data.get("tickers", [])]
