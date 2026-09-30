"""Disk cache for PIT enricher API responses.

Cache layout:
  <hermes_root>/.hermes_cache/pit/<ticker>/<YYYY-MM-DD>/<key>.json

Lives under ``.hermes_cache/`` with every other Hermes cache (bars, ai, runs,
reviews) so there is exactly one cache root to inspect, clear, or ignore.

Keys used by the built-in enrichers:
  yf_fundamentals   — yfinance income/balance sheet metrics
  edgar_sections    — extracted 10-K section text
  massive_news_<N>d — Polygon/Massive news snippets for N-day window
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def _find_project_root() -> Path:
    """Walk up from this file to find the nearest directory with pyproject.toml."""
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path(__file__).resolve().parents[5]  # fallback


_CACHE_ROOT = _find_project_root() / ".hermes_cache" / "pit"


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
