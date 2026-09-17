"""Disk cache for PIT enricher API responses.

Cache layout:
  <hermes_root>/.cache/pit/<ticker>/<YYYY-MM-DD>/<key>.json

Keys used by the built-in enrichers:
  yf_fundamentals   — yfinance income/balance sheet metrics
  edgar_sections    — extracted 10-K section text
  massive_news_<N>d — Polygon/Massive news snippets for N-day window
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

# Resolve to the repo root regardless of where Python is invoked from.
_CACHE_ROOT = Path(__file__).parents[5] / ".cache" / "pit"


def _path(ticker: str, as_of: date, key: str) -> Path:
    return _CACHE_ROOT / ticker / as_of.isoformat() / f"{key}.json"


def cache_get(ticker: str, as_of: date, key: str) -> dict | list | None:
    p = _path(ticker, as_of, key)
    if p.exists():
        return json.loads(p.read_text())
    return None


def cache_set(ticker: str, as_of: date, key: str, data: dict | list) -> None:
    p = _path(ticker, as_of, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, default=str))
