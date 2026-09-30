"""Point-in-time fundamentals from Massive's (formerly Polygon.io) Financials API.

Unlike the yfinance-based enrichers, this is sourced directly from SEC 10-K/10-Q
XBRL filings and carries each period's real ``filing_date`` — true point-in-time,
not an assumed lag. History goes back well beyond yfinance's rolling ~4-5 fiscal
years (AAPL, for example, has annual filings back to FY2009 via this endpoint).

Both annual (10-K) and quarterly (10-Q) periods are fetched, so the "current"
period used at any ``as_of`` date is never more stale than one quarter. YoY
comparisons always match same-length, same-fiscal-period periods (Q3 vs Q3,
FY vs FY) to avoid seasonality artifacts from mixing quarterly and annual data.

Requires ``POLYGON_API_KEY`` (or ``MASSIVE_API_KEY``) in the environment.
"""

from __future__ import annotations

import json
import os
from datetime import date

import requests

from ._cache import _CACHE_ROOT

_BASE_URL = "https://api.polygon.io"
_TICKER_CACHE_DIR = _CACHE_ROOT / "_massive_financials"


def _get_key() -> str:
    key = os.getenv("POLYGON_API_KEY") or os.getenv("MASSIVE_API_KEY", "")
    if not key:
        raise RuntimeError(
            "Massive fundamentals require POLYGON_API_KEY (or MASSIVE_API_KEY) "
            "in the environment."
        )
    return key


def _line(section: dict, key: str) -> float | None:
    v = section.get(key)
    return v.get("value") if v else None


def _fetch_all_periods(ticker: str) -> list[dict]:
    """Fetch (and cache, once per ticker) every annual + quarterly period Massive
    has on file, normalised to flat dicts sorted by end_date descending.

    Cached at the ticker level (not per as_of date) since the underlying filing
    history doesn't depend on the query date — this keeps the total API call
    count to ~2 per ticker regardless of how many trades/dates are queried
    against it.
    """
    cache_path = _TICKER_CACHE_DIR / f"{ticker.upper()}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    api_key = _get_key()
    periods: list[dict] = []
    for timeframe in ("annual", "quarterly"):
        resp = requests.get(
            f"{_BASE_URL}/vX/reference/financials",
            params={"ticker": ticker, "timeframe": timeframe, "limit": 40, "apiKey": api_key},
            timeout=20,
        )
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        for r in resp.json().get("results", []):
            filing_date = r.get("filing_date")
            if not filing_date:
                continue  # e.g. a derived Q4 with no standalone SEC filing
            fin = r.get("financials", {})
            inc = fin.get("income_statement", {})
            bal = fin.get("balance_sheet", {})
            try:
                fiscal_year = int(r.get("fiscal_year"))
            except (TypeError, ValueError):
                continue
            periods.append({
                "fiscal_year": fiscal_year,
                "fiscal_period": r.get("fiscal_period"),  # "FY", "Q1".."Q4"
                "timeframe": timeframe,
                "end_date": r.get("end_date"),
                "filing_date": filing_date,
                "revenue": _line(inc, "revenues"),
                "gross_profit": _line(inc, "gross_profit"),
                "operating_income": _line(inc, "operating_income_loss"),
                "net_income": _line(inc, "net_income_loss"),
                "current_assets": _line(bal, "current_assets"),
                "current_liabilities": _line(bal, "current_liabilities"),
                "equity": _line(bal, "equity_attributable_to_parent") or _line(bal, "equity"),
                "liabilities": _line(bal, "liabilities"),
            })

    periods.sort(key=lambda p: p["end_date"], reverse=True)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(periods))
    return periods


def _latest_as_of(periods: list[dict], as_of: date) -> dict | None:
    """Most recently *filed* period (annual or quarterly) known as of ``as_of``."""
    as_of_str = as_of.isoformat()
    eligible = [p for p in periods if p["filing_date"] <= as_of_str]
    if not eligible:
        return None
    return max(eligible, key=lambda p: p["filing_date"])


def _yoy_prior(periods: list[dict], ref: dict) -> dict | None:
    """Same timeframe + fiscal_period, exactly one fiscal year before ``ref``."""
    for p in periods:
        if (p["timeframe"] == ref["timeframe"]
                and p["fiscal_period"] == ref["fiscal_period"]
                and p["fiscal_year"] == ref["fiscal_year"] - 1):
            return p
    return None


def latest_period(ticker: str, as_of: date, timeframe: str | None = None) -> tuple[int | None, str | None]:
    """Fiscal year and period label (``"FY"``, ``"Q1"``..``"Q4"``) of the most
    recent SEC filing known as of ``as_of``, or ``(None, None)`` if none.

    ``timeframe``, if given, restricts the lookup to ``"annual"`` or
    ``"quarterly"`` filings only — e.g. call it once with each to get the
    latest 10-K and the latest 10-Q separately, rather than whichever of the
    two was filed most recently overall.

    A thin wrapper around the same lookup ``MassiveFundamentalsScreen``/
    ``MassiveFundamentalsEnricher`` use, for callers that only need to record
    *which* filing period applies (e.g. to look up the full raw financials
    for that period later) without computing the S1/S2 screen or a text
    snapshot.
    """
    periods = _fetch_all_periods(ticker)
    if timeframe is not None:
        periods = [p for p in periods if p["timeframe"] == timeframe]
    t0 = _latest_as_of(periods, as_of)
    if t0 is None:
        return None, None
    return t0["fiscal_year"], t0["fiscal_period"]


class MassiveFundamentalsScreen:
    """Deterministic S1/S2 filter using Massive's SEC-filings-based financials.

    Same S1 (margin expansion) / S2 (revenue re-acceleration or fast growth)
    semantics as ``YFinanceFundamentalsScreen``, but PIT-correct via real filing
    dates and aware of quarterly filings, so the reference period is never more
    stale than one quarter relative to ``as_of``.
    """

    def screen(self, ticker: str, as_of: date,
               min_margin_expansion_bps: float = 100.0,
               min_revenue_growth_pct: float = 10.0) -> dict:
        periods = _fetch_all_periods(ticker)
        t0 = _latest_as_of(periods, as_of)
        if t0 is None:
            return {"s1": None, "s2": None, "s1_detail": "no data", "s2_detail": "no data"}

        t1 = _yoy_prior(periods, t0)
        if t1 is None:
            return {"s1": None, "s2": None,
                     "s1_detail": "no prior-year period", "s2_detail": "no prior-year period"}
        t2 = _yoy_prior(periods, t1)

        s1, s1_detail = self._compute_s1(t0, t1, min_margin_expansion_bps)
        s2, s2_detail = self._compute_s2(t0, t1, t2, min_revenue_growth_pct)
        return {"s1": s1, "s2": s2, "s1_detail": s1_detail, "s2_detail": s2_detail}

    @staticmethod
    def _label(p: dict) -> str:
        return f"{p['fiscal_period']}{p['fiscal_year']}"

    def _compute_s1(self, t0: dict, t1: dict, min_bps: float) -> tuple[bool | None, str]:
        r0, r1 = t0["revenue"], t1["revenue"]
        if not r0 or not r1:
            return None, "missing revenue"

        g0 = t0["gross_profit"] / r0 if t0["gross_profit"] is not None else None
        g1 = t1["gross_profit"] / r1 if t1["gross_profit"] is not None else None
        if g0 is not None and g1 is not None:
            bps = (g0 - g1) * 10_000
            if bps >= min_bps:
                return True, (f"gross margin {g1*100:.1f}% → {g0*100:.1f}% (+{bps:.0f}bps) "
                              f"[{self._label(t1)}→{self._label(t0)}]")

        o0 = t0["operating_income"] / r0 if t0["operating_income"] is not None else None
        o1 = t1["operating_income"] / r1 if t1["operating_income"] is not None else None
        if o0 is not None and o1 is not None:
            bps = (o0 - o1) * 10_000
            if bps >= min_bps:
                return True, (f"op margin {o1*100:.1f}% → {o0*100:.1f}% (+{bps:.0f}bps) "
                              f"[{self._label(t1)}→{self._label(t0)}]")

        have_margins = g0 is not None and g1 is not None
        detail = f"gross: {g1*100:.1f}%→{g0*100:.1f}%" if have_margins else "no margin data"
        return False, detail

    def _compute_s2(self, t0: dict, t1: dict, t2: dict | None,
                     min_growth_pct: float) -> tuple[bool | None, str]:
        r0, r1 = t0["revenue"], t1["revenue"]
        if not r0 or not r1:
            return None, "missing revenue periods"

        g0 = (r0 - r1) / abs(r1) * 100
        if g0 < 0:
            return False, f"revenue declined {g0:.1f}% YoY [{self._label(t1)}→{self._label(t0)}]"

        if t2 is None or not t2["revenue"]:
            if g0 >= min_growth_pct:
                return True, (
                    f"revenue fast-growth: {g0:.1f}% YoY "
                    f"(≥{min_growth_pct:.0f}%, no prior YoY to check re-accel)"
                )
            return None, "missing prior-prior period for re-accel check"

        r2 = t2["revenue"]
        g1 = (r1 - r2) / abs(r2) * 100
        reaccel = g0 > g1
        fast = g0 >= min_growth_pct

        if reaccel:
            return True, f"revenue re-accel: {g1:.1f}% → {g0:.1f}% YoY"
        if fast:
            return True, f"revenue fast-growth: {g0:.1f}% YoY (≥{min_growth_pct:.0f}%)"
        return False, f"revenue decel & sub-threshold: {g1:.1f}% → {g0:.1f}% YoY"


def _period_by_year_quarter(periods: list[dict], fiscal_year: int, fiscal_period: str) -> dict | None:
    """Exact period lookup by fiscal year + period label (e.g. 2023, ``"Q2"``).

    ``fiscal_period == "FY"`` implies the annual timeframe, anything else
    (``"Q1"``..``"Q4"``) implies quarterly — Polygon's ``annual``/``quarterly``
    endpoints only ever produce one or the other, so the timeframe never needs
    its own column.
    """
    timeframe = "annual" if fiscal_period == "FY" else "quarterly"
    for p in periods:
        if (
            p["timeframe"] == timeframe
            and p["fiscal_year"] == fiscal_year
            and p["fiscal_period"] == fiscal_period
        ):
            return p
    return None


def screen_period(ticker: str, fiscal_year: int, fiscal_period: str,
                   min_margin_expansion_bps: float = 100.0,
                   min_revenue_growth_pct: float = 10.0) -> dict:
    """Same S1/S2 semantics as ``MassiveFundamentalsScreen.screen``, but keyed
    directly by the filing period — e.g. the ``fundamentals_year``/
    ``fundamentals_quarter`` columns a caller may have already recorded —
    instead of an ``as_of`` date. Lets a downstream script reconstruct the
    signals from an already-built dataset without redoing the as-of-date
    lookup (and without needing the original ``as_of`` date at all).
    """
    periods = _fetch_all_periods(ticker)
    t0 = _period_by_year_quarter(periods, fiscal_year, fiscal_period)
    if t0 is None:
        return {"s1": None, "s2": None, "s1_detail": "period not found", "s2_detail": "period not found"}

    t1 = _yoy_prior(periods, t0)
    if t1 is None:
        return {"s1": None, "s2": None,
                "s1_detail": "no prior-year period", "s2_detail": "no prior-year period"}
    t2 = _yoy_prior(periods, t1)

    screen = MassiveFundamentalsScreen()
    s1, s1_detail = screen._compute_s1(t0, t1, min_margin_expansion_bps)
    s2, s2_detail = screen._compute_s2(t0, t1, t2, min_revenue_growth_pct)
    return {"s1": s1, "s2": s2, "s1_detail": s1_detail, "s2_detail": s2_detail}


class MassiveFundamentalsEnricher:
    """Appends a PIT profitability/leverage snapshot to the advisor context,
    sourced from the same Massive SEC-filings data as ``MassiveFundamentalsScreen``.

    Deliberately does not include valuation multiples (P/E, P/B, EV/EBITDA) —
    those need a PIT price/shares join this filings-only endpoint doesn't carry;
    pair with a price-based enricher separately if valuation ratios are needed.
    """

    def enrich(self, ticker: str, as_of: date) -> str:
        periods = _fetch_all_periods(ticker)
        t0 = _latest_as_of(periods, as_of)
        if t0 is None:
            return ""

        rev, gp, oi, ni = t0["revenue"], t0["gross_profit"], t0["operating_income"], t0["net_income"]
        ca, cl, eq, liab = t0["current_assets"], t0["current_liabilities"], t0["equity"], t0["liabilities"]

        lines = [
            f"Fundamentals (point-in-time, SEC {t0['timeframe']} filing "
            f"{t0['fiscal_period']}{t0['fiscal_year']}, filed {t0['filing_date']}):"
        ]
        if rev and gp is not None:
            lines.append(f"  Gross margin: {gp / rev * 100:.1f}%")
        if rev and oi is not None:
            lines.append(f"  Operating margin: {oi / rev * 100:.1f}%")
        if rev and ni is not None:
            lines.append(f"  Net margin: {ni / rev * 100:.1f}%")
        if ni is not None and eq:
            lines.append(f"  ROE: {ni / eq * 100:.1f}%")
        if ca is not None and cl:
            lines.append(f"  Current ratio: {ca / cl:.2f}")
        if liab is not None and eq:
            lines.append(f"  Liabilities/Equity: {liab / eq * 100:.1f}%")

        return "\n".join(lines) if len(lines) > 1 else ""
