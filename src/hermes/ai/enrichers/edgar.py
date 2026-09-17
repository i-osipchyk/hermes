"""Point-in-time EDGAR 10-K fetcher.

Fetches the most recent 10-K whose EDGAR ``acceptedDate`` is on or before
``as_of``, extracts Items 1, 1A, and 7, and returns them as a text block.
Uses the public SEC EDGAR REST API — no API key required.

Results are cached to ``.cache/pit/`` so EDGAR is only hit once per
ticker/date combination.
"""

from __future__ import annotations

import os
import re
from datetime import date

import requests

from ._cache import cache_get, cache_set

_EDGAR_HEADERS = {"User-Agent": "hermes-trading research@example.com"}
_CHARS_PER_SECTION = int(os.getenv("CHARS_PER_SECTION", "3000"))

_SECTION_PATTERNS = [
    ("Item 1",  r"item\s*1[\.\s\u2014\-]+business"),
    ("Item 1A", r"item\s*1a[\.\s\u2014\-]+risk\s+factor"),
    ("Item 7",  r"item\s*7[\.\s\u2014\-]+management"),
]


def _get_cik(ticker: str) -> str:
    data = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers=_EDGAR_HEADERS,
        timeout=15,
    ).json()
    for entry in data.values():
        if entry["ticker"].upper() == ticker.upper():
            return str(entry["cik_str"]).zfill(10)
    raise ValueError(f"CIK not found for ticker {ticker!r}")


def _extract_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    for key, pattern in _SECTION_PATTERNS:
        matches = list(re.finditer(pattern, text, re.IGNORECASE))
        if not matches:
            continue
        m = matches[-1]
        sections[key] = text[m.start(): m.start() + _CHARS_PER_SECTION].strip()
    return sections


def _search_page(filings: dict, as_of_str: str, cik: str) -> dict[str, str] | None:
    for i, form in enumerate(filings["form"]):
        if form != "10-K":
            continue
        filed = (filings["filingDate"][i] or "")[:10]
        if filed > as_of_str:
            continue
        accession = filings["accessionNumber"][i].replace("-", "")
        primary = (filings.get("primaryDocument") or [""])[i] or ""
        doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{primary}"
        )
        html = requests.get(doc_url, headers=_EDGAR_HEADERS, timeout=30).text
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return _extract_sections(text)
    return None


def _fetch_filing(ticker: str, as_of: date) -> dict[str, str]:
    cik = _get_cik(ticker)
    subs = requests.get(
        f"https://data.sec.gov/submissions/CIK{cik}.json",
        headers=_EDGAR_HEADERS,
        timeout=15,
    ).json()
    as_of_str = as_of.isoformat()

    result = _search_page(subs["filings"]["recent"], as_of_str, cik)
    if result is not None:
        return result

    for page_meta in subs["filings"].get("files", []):
        if page_meta.get("filingFrom", "")[:10] > as_of_str:
            continue
        page = requests.get(
            f"https://data.sec.gov/submissions/{page_meta['name']}",
            headers=_EDGAR_HEADERS,
            timeout=15,
        ).json()
        result = _search_page(page, as_of_str, cik)
        if result is not None:
            return result
        cutoff = f"{int(as_of_str[:4]) - 2}-01-01"
        if page_meta.get("filingTo", "9999")[:10] < cutoff:
            break

    return {}


class EDGARFilingEnricher:
    """Appends the most recent 10-K sections (Items 1, 1A, 7) to the advisor
    context, filtered to filings available as of the bar date.

    No API key required — uses the public SEC EDGAR REST API.
    Set ``EDGAR_CHARS_PER_SECTION`` env var to control text length per section
    (default 3000 characters).
    """

    def enrich(self, ticker: str, as_of: date) -> str:
        cached = cache_get(ticker, as_of, "edgar_sections")
        if cached is not None:
            sections = cached
        else:
            try:
                sections = _fetch_filing(ticker, as_of)
            except Exception as exc:
                return f"10-K filing: unavailable ({exc})"
            cache_set(ticker, as_of, "edgar_sections", sections)

        if not sections:
            return ""

        lines = [f"10-K filing (as of {as_of}):"]
        for key in ("Item 1", "Item 1A", "Item 7"):
            text = sections.get(key)
            if text:
                lines.append(f"\n[{key}]\n{text}")
        return "\n".join(lines)
