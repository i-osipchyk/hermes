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

        sector = (t.info or {}).get("sector", "")
        base: dict = {"sector": sector} if sector else {}

        if inc is None or inc.empty or bal is None or bal.empty:
            cache_set(ticker, as_of, "yf_fundamentals", base)
            return base

        avail_inc = [c for c in inc.columns if c.date() + timedelta(days=_FILING_LAG_DAYS) <= as_of]
        avail_bal = [c for c in bal.columns if c.date() + timedelta(days=_FILING_LAG_DAYS) <= as_of]

        if not avail_inc or not avail_bal:
            cache_set(ticker, as_of, "yf_fundamentals", base)
            return base

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
