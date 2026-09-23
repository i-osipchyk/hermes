"""Point-in-time fundamental metrics from yfinance historical financials.

Uses yfinance annual income statement + balance sheet with a conservative
90-day filing lag: a fiscal year ending on date D is treated as publicly
available on D + 90 days.

RESTATEMENT RISK: yfinance may serve restated figures rather than as-reported
values for older periods. Treat results as approximate.
"""

from __future__ import annotations

import warnings
from datetime import date, timedelta

from ._cache import cache_get, cache_set

_FILING_LAG_DAYS = 90


def _row(df, *candidates):
    for name in candidates:
        if name in df.index:
            return df.loc[name]
    return None


class YFinanceFundamentalsEnricher:
    """Appends PIT fundamental metrics (ROE, D/E, P/E, P/B, EV/EBITDA…) to the
    advisor context using yfinance annual financials filtered by the 90-day lag.

    No API key required — uses the public yfinance library.
    """

    def enrich(self, ticker: str, as_of: date) -> str:
        metrics = self._fetch(ticker, as_of)
        if not metrics:
            return ""

        lines = ["Fundamentals (point-in-time, 90-day filing lag):"]
        # (label, as_percent) — ratio metrics stored as decimals need *100 for display
        fields = [
            ("sector",             "Sector",           False),
            ("trailingPE",         "Trailing P/E",     False),
            ("priceToBook",        "Price/Book",        False),
            ("enterpriseToEbitda", "EV/EBITDA",         False),
            ("returnOnEquity",     "ROE",               True),
            ("profitMargins",      "Profit margin",     True),
            ("debtToEquity",       "Debt/Equity",       False),
            ("currentRatio",       "Current ratio",     False),
        ]
        for key, label, as_pct in fields:
            val = metrics.get(key)
            if val is None:
                continue
            if isinstance(val, float):
                if as_pct:
                    lines.append(f"  {label}: {val * 100:.1f}%")
                else:
                    lines.append(f"  {label}: {val:.2f}")
            else:
                lines.append(f"  {label}: {val}")
        return "\n".join(lines)

    def _fetch(self, ticker: str, as_of: date) -> dict:
        cached = cache_get(ticker, as_of, "yf_fundamentals")
        if cached is not None:
            return cached

        try:
            import yfinance as yf
        except ImportError as exc:
            raise ImportError(
                "YFinanceFundamentalsEnricher requires yfinance. pip install yfinance"
            ) from exc

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t = yf.Ticker(ticker)
            inc = t.income_stmt
            bal = t.balance_sheet

        if inc is None or inc.empty or bal is None or bal.empty:
            cache_set(ticker, as_of, "yf_fundamentals", {})
            return {}

        avail_inc = [c for c in inc.columns if c.date() + timedelta(days=_FILING_LAG_DAYS) <= as_of]
        avail_bal = [c for c in bal.columns if c.date() + timedelta(days=_FILING_LAG_DAYS) <= as_of]

        if not avail_inc or not avail_bal:
            cache_set(ticker, as_of, "yf_fundamentals", {})
            return {}

        # Fetch sector only when we have real financial data to return.
        # t.info hits a separate slow quoteSummary endpoint — guard it.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                sector = (t.info or {}).get("sector", "")
        except Exception:
            sector = ""
        base: dict = {"sector": sector} if sector else {}

        latest_inc = inc[avail_inc[0]]
        latest_bal = bal[avail_bal[0]]

        def iv(row_name: str, *alts) -> float | None:
            v = _row(latest_inc, row_name, *alts)
            if v is None:
                return None
            val = v if not hasattr(v, "iloc") else v.iloc[0]
            try:
                f = float(val)
                return None if str(f) == "nan" else f
            except (TypeError, ValueError):
                return None

        def bv(row_name: str, *alts) -> float | None:
            v = _row(latest_bal, row_name, *alts)
            if v is None:
                return None
            val = v if not hasattr(v, "iloc") else v.iloc[0]
            try:
                f = float(val)
                return None if str(f) == "nan" else f
            except (TypeError, ValueError):
                return None

        net_income = iv("Net Income")
        revenue = iv("Total Revenue", "Operating Revenue")
        ebitda = iv("EBITDA", "Normalized EBITDA")
        eps = iv("Diluted EPS")
        equity = bv("Common Stock Equity", "Stockholders Equity")
        total_debt = bv("Total Debt")
        current_assets = bv("Current Assets")
        current_liabilities = bv("Current Liabilities")
        shares = bv("Ordinary Shares Number", "Share Issued")
        net_debt = bv("Net Debt")

        result: dict = dict(base)

        if total_debt is not None and equity and equity != 0:
            result["debtToEquity"] = (total_debt / equity) * 100
        if net_income is not None and equity and equity != 0:
            result["returnOnEquity"] = net_income / equity
        if current_assets is not None and current_liabilities and current_liabilities != 0:
            result["currentRatio"] = current_assets / current_liabilities
        if net_income is not None and revenue and revenue != 0:
            result["profitMargins"] = net_income / revenue

        # Reconstruct price-dependent ratios using PIT price
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                price_df = yf.download(
                    ticker,
                    start=as_of,
                    end=as_of + timedelta(days=7),
                    auto_adjust=True,
                    progress=False,
                )
            close = price_df["Close"]
            price = float(
                close.iloc[0].iloc[0] if hasattr(close.iloc[0], "iloc") else close.iloc[0]
            ) if not price_df.empty else None
        except Exception:
            price = None

        if price and price > 0:
            if eps and eps > 0:
                result["trailingPE"] = price / eps
            if equity is not None and shares and shares > 0:
                bps = equity / shares
                if bps > 0:
                    result["priceToBook"] = price / bps
            if ebitda and ebitda > 0 and shares:
                ev = (price * shares) + (net_debt if net_debt is not None else float(total_debt or 0))
                result["enterpriseToEbitda"] = ev / ebitda

        cache_set(ticker, as_of, "yf_fundamentals", result)
        return result


class YFinanceFundamentalsScreen:
    """Deterministic S1/S2 filter using yfinance annual financials + 90-day PIT lag.

    screen() returns a dict with boolean signals and detail strings.
    Reuses the same .cache/pit/ disk cache as YFinanceFundamentalsEnricher.
    """

    CACHE_KEY = "fund_screen"

    def screen(self, ticker: str, as_of: date,
               min_margin_expansion_bps: float = 100.0,
               min_revenue_growth_pct: float = 10.0) -> dict:
        """
        Returns:
          {
            "s1": bool | None,   # None = insufficient data
            "s2": bool | None,
            "s1_detail": str,
            "s2_detail": str,
          }
        """
        data = self._fetch(ticker, as_of)
        if data is None:
            return {"s1": None, "s2": None, "s1_detail": "no data", "s2_detail": "no data"}

        s1, s1_detail = self._compute_s1(data, min_margin_expansion_bps)
        s2, s2_detail = self._compute_s2(data, min_revenue_growth_pct)
        return {"s1": s1, "s2": s2, "s1_detail": s1_detail, "s2_detail": s2_detail}

    def _fetch(self, ticker: str, as_of: date) -> dict | None:
        cached = cache_get(ticker, as_of, self.CACHE_KEY)
        if cached is not None:
            return cached or None  # {} sentinel = no data

        try:
            import yfinance as yf
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                t = yf.Ticker(ticker)
                inc = t.income_stmt
        except Exception:
            cache_set(ticker, as_of, self.CACHE_KEY, {})
            return None

        if inc is None or inc.empty:
            cache_set(ticker, as_of, self.CACHE_KEY, {})
            return None

        avail = [c for c in inc.columns if c.date() + timedelta(days=_FILING_LAG_DAYS) <= as_of]
        if len(avail) < 3:   # need t0, t1, t2 for S2 (two growth rates)
            cache_set(ticker, as_of, self.CACHE_KEY, {})
            return None

        def get_row(name, *alts):
            for n in (name, *alts):
                if n in inc.index:
                    row = inc.loc[n]
                    vals = [row[avail[i]] for i in range(3)]
                    return [float(v) if str(float(v)) != "nan" else None for v in vals]
            return [None, None, None]

        revenue   = get_row("Total Revenue", "Operating Revenue")
        gross     = get_row("Gross Profit")
        operating = get_row("Operating Income", "EBIT")

        result = {
            "revenue": revenue,      # [t0, t1, t2]
            "gross":   gross,
            "operating": operating,
        }
        cache_set(ticker, as_of, self.CACHE_KEY, result)
        return result

    def _compute_s1(self, data, min_bps) -> tuple[bool | None, str]:
        """Gross OR operating margin expanded >= min_bps bps YoY (t0 vs t1)."""
        rev0, rev1 = data["revenue"][0], data["revenue"][1]
        if not rev0 or not rev1:
            return None, "missing revenue"

        # Gross margin
        g0 = data["gross"][0] / rev0 if data["gross"][0] is not None else None
        g1 = data["gross"][1] / rev1 if data["gross"][1] is not None else None
        if g0 is not None and g1 is not None:
            bps = (g0 - g1) * 10_000
            if bps >= min_bps:
                return True, f"gross margin {g1*100:.1f}% → {g0*100:.1f}% (+{bps:.0f}bps)"

        # Operating margin fallback
        o0 = data["operating"][0] / rev0 if data["operating"][0] is not None else None
        o1 = data["operating"][1] / rev1 if data["operating"][1] is not None else None
        if o0 is not None and o1 is not None:
            bps = (o0 - o1) * 10_000
            if bps >= min_bps:
                return True, f"op margin {o1*100:.1f}% → {o0*100:.1f}% (+{bps:.0f}bps)"

        detail = f"gross: {g1*100:.1f}%→{g0*100:.1f}%" if g0 is not None else "no margin data"
        return False, detail

    def _compute_s2(self, data, min_growth_pct) -> tuple[bool | None, str]:
        """Revenue re-accelerating (t0_growth > t1_growth) OR t0 growth > min_growth_pct."""
        r0, r1, r2 = data["revenue"]
        if None in (r0, r1, r2) or r1 == 0 or r2 == 0:
            return None, "missing revenue periods"

        g0 = (r0 - r1) / abs(r1) * 100   # YoY growth ending at t0
        g1 = (r1 - r2) / abs(r2) * 100   # YoY growth ending at t1

        if g0 < 0:
            return False, f"revenue declined {g0:.1f}% YoY"

        reaccel = g0 > g1
        fast    = g0 >= min_growth_pct

        if reaccel:
            return True, f"revenue re-accel: {g1:.1f}% → {g0:.1f}% YoY"
        if fast:
            return True, f"revenue fast-growth: {g0:.1f}% YoY (≥{min_growth_pct:.0f}%)"

        return False, f"revenue decel & sub-threshold: {g1:.1f}% → {g0:.1f}% YoY"
