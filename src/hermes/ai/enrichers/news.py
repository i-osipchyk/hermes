"""Point-in-time news enricher via Polygon.io / Massive.com.

Fetches articles published in the window [as_of - days_back, as_of] for
the given ticker.  Results are cached so the API is only hit once per
ticker/date combination.

Requires ``POLYGON_API_KEY`` (or ``MASSIVE_API_KEY`` for the legacy domain)
in the environment.

Free-tier note: Polygon's free tier is rate-limited (~5 req/min).
The enricher retries with exponential backoff on 429 responses.
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta

import requests

from ._cache import cache_get, cache_set

_BASE_URL = "https://api.polygon.io"
_DEFAULT_DAYS_BACK = 30
_DEFAULT_NEWS_COUNT = int(os.getenv("NEWS_COUNT", "10"))


def _get_key() -> str:
    key = os.getenv("POLYGON_API_KEY") or os.getenv("MASSIVE_API_KEY", "")
    if not key:
        raise RuntimeError(
            "PolygonNewsEnricher requires POLYGON_API_KEY (or MASSIVE_API_KEY) "
            "in the environment."
        )
    return key


class PolygonNewsEnricher:
    """Appends recent news headlines to the advisor context using the
    Polygon.io (formerly Massive.com) news API.

    Args:
        days_back:  Window size in days ending on the bar date (default 30).
        news_count: Maximum number of headlines to include (default 10).
    """

    def __init__(
        self,
        days_back: int = _DEFAULT_DAYS_BACK,
        news_count: int = _DEFAULT_NEWS_COUNT,
    ) -> None:
        self.days_back = days_back
        self.news_count = news_count

    def enrich(self, ticker: str, as_of: date) -> str:
        snippets = self._fetch(ticker, as_of)
        if not snippets:
            return ""
        lines = [f"Recent news ({self.days_back}-day window ending {as_of}):"]
        for i, s in enumerate(snippets, 1):
            lines.append(f"  {i}. {s}")
        return "\n".join(lines)

    def _fetch(self, ticker: str, as_of: date) -> list[str]:
        cache_key = f"polygon_news_{self.days_back}d"
        cached = cache_get(ticker, as_of, cache_key)
        if cached is not None:
            return cached

        api_key = _get_key()
        from_date = as_of - timedelta(days=self.days_back)

        params = {
            "ticker": ticker,
            "published_utc.gte": from_date.isoformat(),
            "published_utc.lte": as_of.isoformat(),
            "limit": min(self.news_count * 2, 50),
            "sort": "published_utc",
            "order": "desc",
            "apiKey": api_key,
        }

        for attempt in range(4):
            resp = requests.get(
                f"{_BASE_URL}/v2/reference/news",
                params=params,
                timeout=20,
            )
            if resp.status_code != 429:
                break
            sleep_sec = 15 * (2 ** attempt)
            time.sleep(sleep_sec)

        resp.raise_for_status()
        data = resp.json()

        snippets: list[str] = []
        for article in data.get("results", []):
            title = article.get("title", "")
            description = article.get("description", "")
            if title:
                snippets.append(f"{title} — {description}".strip(" —"))

        result = snippets[: self.news_count]
        cache_set(ticker, as_of, cache_key, result)
        return result
