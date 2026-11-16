"""Point-in-time value panel for the S&P 500 (idea 21060772).

For every quarter-end rebalance date, and every stock in the index on that date, compute
earnings yield (E/P) and book yield (B/P) from what was knowable at that day's close:

* fundamentals: SEC EDGAR XBRL facts (data.sec.gov companyconcept), each value taken
  from the latest filing whose ``filed`` date is <= the rebalance date;
* price: Tiingo's *unadjusted* close on the rebalance date (shares are as reported, so
  market cap must use the unadjusted price);
* membership: sp500_history.csv (point-in-time constituents), delisted names included.

Massive's /vX/reference/financials was shut down 2026-10-09 and its successor is not on
our plan, so Massive is used only to map ticker -> CIK (its reference endpoint takes a
date, which handles reused tickers).

Output: research/output/value/panel.csv  (date, ticker, ep, bp, mcap, ...). The strategy
``strategies/value/sp500_value.py`` ranks from it. Run:

    .venv/bin/python research/value/build_panel.py                  # yfinance + Tiingo
    .venv/bin/python research/value/build_panel.py --yfinance-only  # free, survivor-biased

``--yfinance-only`` skips the Tiingo pass: the stocks yfinance lacks (mostly delisted, which
over-represents cheap stocks) get ``why=no_price`` and are never selected, so results are
optimistic. Use it to see whether there is anything worth paying Tiingo for.
"""

from __future__ import annotations

import bisect
import csv
import functools
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src")]
load_dotenv(ROOT / ".env")

from hermes import Timeframe  # noqa: E402
from hermes.data import ConstituentCalendar  # noqa: E402

CACHE = ROOT / ".hermes_cache" / "pit"
CIK_DIR = CACHE / "value_cik"
EDGAR_DIR = CACHE / "edgar_concepts"
PRICE_DIR = CACHE / "value_prices"
OUT = ROOT / "research" / "output" / "value"

WINDOW_START = date(2010, 12, 1)   # first rebalance is end-Dec 2010
WINDOW_END = date(2026, 9, 30)
PRICE_START = "2009-06-01"
PRICE_END = "2026-10-13"
D1 = Timeframe.parse("1d")
STALE_DAYS = 460   # a fundamental older than ~5 quarters is treated as missing
UA = {"User-Agent": "Hermes research ivan.osipchyk.work@gmail.com"}

INCOME = [("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss"),
          ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic")]
EQUITY = [("us-gaap", "StockholdersEquity"),
          ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")]
SHARES = [("dei", "EntityCommonStockSharesOutstanding"),
          ("us-gaap", "CommonStockSharesOutstanding")]
# Multi-class issuers (Visa, Comcast, CME, ...) tag cover-page shares per class, which the
# API drops; their diluted weighted-average count is one total, in the traded class's units.
DILUTED = [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding")]
# Intangible capital (idea f99af81f): R&D, and 30% of SG&A as organization capital.
RND = [("us-gaap", "ResearchAndDevelopmentExpense"),
       ("us-gaap", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost")]
SGA = [("us-gaap", "SellingGeneralAndAdministrativeExpense")]
# Issuers that split SG&A into two lines (Microsoft, Alphabet, ...): summed when SGA is absent.
SGA_PARTS = [("us-gaap", "SellingAndMarketingExpense"),
             ("us-gaap", "GeneralAndAdministrativeExpense")]
CONCEPTS = INCOME + EQUITY + SHARES + DILUTED + RND + SGA + SGA_PARTS
# Perpetual-inventory constants (Peters & Taylor 2017; Eisfeldt, Kim & Papanikolaou 2020).
INTANGIBLE_DEPRECIATION = 0.20
INITIAL_GROWTH = 0.10      # first stock = first flow / (depreciation + growth)
ORG_SHARE = 0.30           # share of SG&A that is investment in organization capital
# Berkshire reports shares in class-A equivalents (1 A = 1,500 B), so BRK.B x shares is
# meaningless without a per-class ratio; it is one stock, so it is left out.
EXCLUDE = {"BRK.B", "BRK.A"}


class RateLimiter:
    def __init__(self, per_second: float) -> None:
        self.gap = 1.0 / per_second
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        time.sleep(max(0.0, t - now))


EDGAR_RL = RateLimiter(8)


def get_json(url: str, *, headers=None, params=None, rl: RateLimiter | None = None):
    for attempt in range(20):
        if rl:
            rl.wait()
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code == 429:   # Tiingo's hourly allocation: wait it out
            print(f"  429 on {url.split('?')[0]}, sleeping 5 min", file=sys.stderr)
            time.sleep(300)
            continue
        if r.status_code in (500, 502, 503):
            time.sleep(min(2 ** attempt, 60))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"gave up on {url}")


# ── ticker -> CIK ───────────────────────────────────────────────────────────

def ciks_for(ticker: str, first: date, last: date) -> list[str]:
    """Every CIK that held ``ticker`` during its membership (a company can re-domicile
    under a new CIK, e.g. ExxonMobil's 2026 holding company)."""
    path = CIK_DIR / f"{ticker}.json"
    if path.exists():
        return json.loads(path.read_text())["ciks"]
    key = os.environ["MASSIVE_API_KEY"]
    ciks: list[str] = []
    for params in ({"ticker": ticker, "date": first.isoformat()},
                   {"ticker": ticker, "date": last.isoformat()},
                   {"ticker": ticker, "active": "false"}):
        if ciks and "active" in params:
            break
        d = get_json("https://api.polygon.io/v3/reference/tickers", params=params,
                     headers={"Authorization": f"Bearer {key}"}) or {}
        for r in d.get("results", []):
            if r.get("cik") and r["cik"] not in ciks:
                ciks.append(r["cik"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ticker": ticker, "ciks": ciks}))
    return ciks


# ── EDGAR facts ─────────────────────────────────────────────────────────────

def _facts(units: dict) -> list[dict]:
    out = []
    for unit, rows in units.items():
        if unit not in ("USD", "shares") or not isinstance(rows, list):
            continue
        out += [{k: r.get(k) for k in ("start", "end", "val", "filed", "form", "accn")}
                for r in rows if r.get("filed") and r.get("end")]
    return out


def concept(cik: str, tax: str, name: str) -> list[dict]:
    """One XBRL concept's facts. The per-concept endpoint sometimes answers with an empty
    ``{"USD": {}}`` for data that exists (Coca-Cola, 2026-10), so an empty answer is
    retried from the company's full companyfacts document."""
    path = EDGAR_DIR / f"{int(cik):010d}" / f"{tax}_{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    d = get_json(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{int(cik):010d}/{tax}/{name}.json",
                 headers=UA, rl=EDGAR_RL)
    facts = _facts(d.get("units", {})) if d else []
    if not facts:
        full = companyfacts(cik)
        facts = _facts(full.get(tax, {}).get(name, {}).get("units", {}))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(facts))
    return facts


_companyfacts: dict[str, dict] = {}
_cf_lock = threading.Lock()


def companyfacts(cik: str) -> dict:
    """The concepts we use from data.sec.gov companyfacts (fetched once per CIK per run)."""
    with _cf_lock:
        if cik in _companyfacts:
            return _companyfacts[cik]
    d = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json",
                 headers=UA, rl=EDGAR_RL) or {}
    keep = {tax: {n: d.get("facts", {}).get(tax, {}).get(n, {}) for t2, n in CONCEPTS if t2 == tax}
            for tax in ("us-gaap", "dei")}
    with _cf_lock:
        _companyfacts[cik] = keep
    return keep


def _d(s: str) -> date:
    return date.fromisoformat(s)


def merged(ciks: list[str], names) -> list[dict]:
    """Facts for ``names`` across every CIK, tagged with the concept's priority."""
    out = []
    for prio, (tax, name) in enumerate(names):
        for cik in ciks:
            out += [{**f, "prio": prio} for f in concept(cik, tax, name)]
    return out


def known(facts: list[dict], as_of: date) -> list[dict]:
    """Facts filed by ``as_of``; per (start, end) the preferred concept's latest filing."""
    best: dict[tuple, dict] = {}
    for f in facts:
        if _d(f["filed"]) > as_of:
            continue
        k = (f.get("start"), f["end"])
        if k not in best or (f["prio"], best[k]["filed"]) < (best[k]["prio"], f["filed"]):
            best[k] = f
    return list(best.values())


def ttm_income(facts: list[dict], as_of: date) -> tuple[float, date] | None:
    """Trailing-twelve-month value: latest fiscal year + current YTD - prior-year YTD."""
    k = [f for f in known(facts, as_of) if f.get("start")]
    for f in k:
        f["_days"] = (_d(f["end"]) - _d(f["start"])).days
    annual = [f for f in k if 350 <= f["_days"] <= 380]
    if not annual:
        return None
    a = max(annual, key=lambda f: f["end"])
    a_end = _d(a["end"])
    ytd = [f for f in k if f["_days"] < 350 and _d(f["end"]) > a_end
           and abs((_d(f["start"]) - a_end).days - 1) <= 10]
    if ytd:
        y = max(ytd, key=lambda f: f["end"])
        y_end = _d(y["end"])
        prior = [f for f in k if abs(f["_days"] - y["_days"]) <= 10
                 and abs((y_end - _d(f["end"])).days - 365) <= 10]
        if prior:
            p = max(prior, key=lambda f: f["filed"])
            return a["val"] + y["val"] - p["val"], y_end
    return a["val"], a_end


def latest_instant(facts: list[dict], as_of: date, sum_classes: bool = False):
    k = [f for f in facts if _d(f["filed"]) <= as_of and not f.get("start")
         and (as_of - _d(f["end"])).days <= STALE_DAYS]
    if not k:
        return None
    top = min(f["prio"] for f in k)
    k = [f for f in k if f["prio"] == top]
    last = max(k, key=lambda f: (f["end"], f["filed"]))
    if not sum_classes:
        return last["val"], _d(last["end"])
    same = {f["val"] for f in k if f["accn"] == last["accn"] and f["end"] == last["end"]}
    return sum(same), _d(last["end"])


def latest_duration(facts: list[dict], as_of: date):
    """Most recent period's value (shortest period on ties), e.g. a quarter's diluted shares."""
    k = [f for f in known(facts, as_of) if f.get("start")
         and (as_of - _d(f["end"])).days <= STALE_DAYS]
    if not k:
        return None
    last = max(k, key=lambda f: (f["end"], f["start"]))
    return last["val"], _d(last["end"])


# ── prices ─────────────────────────────────────────────────────────────────
#
# yfinance where it has the ticker (free), Tiingo only where it does not: Tiingo's free tier
# is 50 requests/hour and 500 symbols/month, so it cannot carry all ~830. Each stock comes
# from one vendor for its whole history. Two things are written per ticker:
#   * the unadjusted close, for market cap (shares are as reported at the time), and
#   * dividend+split adjusted daily bars in Hermes' BarCache, for the backtest itself.
# research/output/value/vendors.json records which vendor each ticker came from.

TIINGO_RL = RateLimiter(45 / 3600)
_vendor_lock = threading.Lock()


def yf_ticker(ticker: str) -> str:
    return ticker.replace(".", "-")


def _yfinance(ticker: str) -> dict[str, float] | None:
    import yfinance as yf

    from hermes import Symbol, YFinanceSource

    df = yf.Ticker(yf_ticker(ticker)).history(
        start=PRICE_START, end=PRICE_END, auto_adjust=False, actions=True)
    df = df.dropna(subset=["Close"])
    if df.empty:
        return None
    # yfinance's unadjusted Close is still split-adjusted: undo every later split.
    splits = df["Stock Splits"].replace(0, 1.0)
    later = splits[::-1].cumprod()[::-1].shift(-1, fill_value=1.0)
    raw = df["Close"] * later
    src = YFinanceSource()
    src.history(src.get_instrument(Symbol(yf_ticker(ticker), "yfinance")), D1,
                datetime.fromisoformat(PRICE_START).replace(tzinfo=UTC),
                datetime.fromisoformat(PRICE_END).replace(tzinfo=UTC))
    return {ts.strftime("%Y-%m-%d"): float(c) for ts, c in raw.items()}


def _tiingo(ticker: str) -> dict[str, float] | None:
    from hermes import Bar, Symbol, TiingoSource
    from hermes.data import BarCache

    rows = get_json(f"https://api.tiingo.com/tiingo/daily/{yf_ticker(ticker)}/prices",
                    headers={"Authorization": f"Token {os.environ['TIINGO_API_KEY']}"},
                    params={"startDate": PRICE_START, "endDate": PRICE_END}, rl=TIINGO_RL)
    if not rows:
        return None
    bars = [Bar(datetime.fromisoformat(r["date"].replace("Z", "+00:00")).astimezone(UTC), D1,
                float(r["adjOpen"]), float(r["adjHigh"]), float(r["adjLow"]),
                float(r["adjClose"]), float(r.get("adjVolume") or 0.0))
            for r in rows if None not in (r.get("adjOpen"), r.get("adjHigh"),
                                          r.get("adjLow"), r.get("adjClose"))]
    inst = TiingoSource().get_instrument(Symbol(ticker, "tiingo"))
    cache = BarCache()
    cache.write(inst, D1, bars)
    cache.mark_checked(inst, D1, bars[0].timestamp, bars[-1].timestamp)
    return {r["date"][:10]: float(r["close"]) for r in rows if r.get("close")}


_yf_missing: set[str] = set()


def prices(ticker: str, allow_tiingo: bool = True) -> dict[str, float] | None:
    """Unadjusted daily closes for ``ticker`` (empty when no vendor has it; None when only
    Tiingo is left to try and ``allow_tiingo`` is off)."""
    path = PRICE_DIR / f"{ticker}.json"
    if path.exists():
        return json.loads(path.read_text())["close"]
    if ticker in _yf_missing and not allow_tiingo:
        return None
    vendor, closes = "yfinance", _yfinance(ticker)
    if closes is None:
        if not allow_tiingo:
            _yf_missing.add(ticker)   # not cached on disk: a later full run tries Tiingo
            return None
        vendor, closes = "tiingo", _tiingo(ticker)
    if closes is None:
        vendor, closes = None, {}
        print(f"  no prices: {ticker}", file=sys.stderr)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"vendor": vendor, "close": closes}))
    return closes


def vendor(ticker: str) -> str | None:
    path = PRICE_DIR / f"{ticker}.json"
    return json.loads(path.read_text())["vendor"] if path.exists() else None


# ── panel ──────────────────────────────────────────────────────────────────

def rebalance_dates(trading_days: list[str]) -> list[date]:
    """Last trading day of each Mar/Jun/Sep/Dec."""
    out: dict[tuple[int, int], str] = {}
    for d in trading_days:
        y, m = int(d[:4]), int(d[5:7])
        if m in (3, 6, 9, 12):
            out[(y, m)] = max(out.get((y, m), d), d)
    return sorted(_d(v) for v in out.values()
                  if WINDOW_START <= _d(v) <= WINDOW_END)


def prepare(ticker: str, cal: ConstituentCalendar) -> tuple[str, list[str]]:
    try:
        return _prepare(ticker, cal)
    except Exception as e:  # one bad ticker must not sink an hours-long fetch; rerun retries
        print(f"  FAILED {ticker}: {e!r}", file=sys.stderr)
        return ticker, []


def _prepare(ticker: str, cal: ConstituentCalendar) -> tuple[str, list[str]]:
    first, last = cal.membership_window(ticker, WINDOW_START, WINDOW_END)
    ciks = ciks_for(ticker, first, last)
    prices(ticker, allow_tiingo=False)   # Tiingo is paced separately, one at a time
    for cik in ciks:
        for tax, name in CONCEPTS:
            concept(cik, tax, name)
    return ticker, ciks


def annual_flows(facts: list[dict], as_of: date) -> list[tuple[date, float]]:
    """Fiscal-year values filed by ``as_of``, one per fiscal-year end, oldest first."""
    by_end: dict[str, dict] = {}
    for f in known(facts, as_of):
        if not f.get("start") or not 350 <= (_d(f["end"]) - _d(f["start"])).days <= 380:
            continue
        cur = by_end.get(f["end"])
        if cur is None or (f["prio"], cur["filed"]) < (cur["prio"], f["filed"]):
            by_end[f["end"]] = f
    return [(_d(e), float(by_end[e]["val"])) for e in sorted(by_end)]


def sga_flows(ciks: list[str], as_of: date) -> list[tuple[date, float]]:
    flows = annual_flows(merged(ciks, SGA), as_of)
    if flows:
        return flows
    parts = [dict(annual_flows(merged(ciks, [c]), as_of)) for c in SGA_PARTS]
    return [(e, parts[0][e] + parts[1][e]) for e in sorted(set(parts[0]) & set(parts[1]))]


def capital(flows: list[tuple[date, float]], as_of: date) -> float:
    """Perpetual-inventory stock of capitalised spending, depreciated to ``as_of``.
    No reported flows means no capital (banks, most retailers report no R&D)."""
    if not flows:
        return 0.0
    k = max(flows[0][1], 0.0) / (INTANGIBLE_DEPRECIATION + INITIAL_GROWTH)
    for (prev, _), (end, val) in zip(flows, flows[1:], strict=False):
        k = k * (1 - INTANGIBLE_DEPRECIATION) ** ((end - prev).days / 365.25) + max(val, 0.0)
    return k * (1 - INTANGIBLE_DEPRECIATION) ** ((as_of - flows[-1][0]).days / 365.25)


# 12-1 momentum (idea f62f0c77): return from t-252 to t-21 trading days, dividend-adjusted.
MOM_LOOKBACK, MOM_SKIP = 252, 21
BARS = ROOT / ".hermes_cache" / "bars"


@functools.cache
def adjusted_closes(ticker: str) -> tuple[list[str], list[float]]:
    """Adjusted daily closes (from the bar cache prices() filled), keyed by NY trading date."""
    import pandas as pd

    v = vendor(ticker)
    if v is None:
        return [], []
    name = yf_ticker(ticker) if v == "yfinance" else ticker
    path = BARS / v / f"{name}_1d.parquet"
    if not path.exists():
        return [], []
    df = pd.read_parquet(path).sort_values("ts")
    ny = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("America/New_York")
    return list(ny.dt.strftime("%Y-%m-%d")), [float(c) for c in df["close"]]


def momentum(ticker: str, rd: date) -> float | None:
    days, closes = adjusted_closes(ticker)
    i = bisect.bisect_right(days, rd.isoformat()) - 1
    if i < MOM_LOOKBACK or days[i] != rd.isoformat():
        return None
    return closes[i - MOM_SKIP] / closes[i - MOM_LOOKBACK] - 1


def score(ciks: list[str], px: float | None, rd: date) -> dict:
    if not ciks:
        return {"why": "no_cik"}
    if px is None:
        return {"why": "no_price"}
    sh = latest_instant(merged(ciks, SHARES), rd, sum_classes=True) or \
        latest_duration(merged(ciks, DILUTED), rd)
    if sh is None or sh[0] <= 0:
        return {"why": "no_shares"}
    mcap = sh[0] * px
    out: dict = {"mcap": mcap, "why": ""}
    ni = ttm_income(merged(ciks, INCOME), rd)
    if ni is not None and (rd - ni[1]).days <= STALE_DAYS:
        out["ep"] = ni[0] / mcap
    eq = latest_instant(merged(ciks, EQUITY), rd)
    if eq is not None:
        out["bp"] = eq[0] / mcap
        k_rd = capital(annual_flows(merged(ciks, RND), rd), rd)
        k_org = ORG_SHARE * capital(sga_flows(ciks, rd), rd)
        out["rd_cap"] = k_rd / mcap
        out["org_cap"] = k_org / mcap
        out["ibp"] = (eq[0] + k_rd) / mcap
        out["ibp_org"] = (eq[0] + k_rd + k_org) / mcap
    if "ep" not in out and "bp" not in out:
        return {"why": "no_fundamentals"}
    # A yield this far out is a units / share-count / reused-ticker error, not a cheap stock.
    if not (-2 < out.get("ep", 0) < 1 and -5 < out.get("bp", 0) < 20
            and out.get("ibp_org", 0) < 20):
        return {"why": "implausible", "mcap": mcap}
    return out


def main() -> None:
    cal = ConstituentCalendar.from_snapshot_csv(ROOT / "sp500_history.csv")
    tickers = [t for t in cal.ever_member(WINDOW_START, WINDOW_END) if t not in EXCLUDE]
    print(f"{len(tickers)} ever-members {WINDOW_START}..{WINDOW_END}", file=sys.stderr)

    ciks: dict[str, list[str]] = {}
    with ThreadPoolExecutor(6) as ex:
        for i, (t, c) in enumerate(ex.map(lambda t: prepare(t, cal), tickers), 1):
            ciks[t] = c
            if i % 50 == 0:
                print(f"  fetched {i}/{len(tickers)}", file=sys.stderr)

    yf_only = "--yfinance-only" in sys.argv
    pending = [] if yf_only else [t for t in tickers if vendor(t) is None and ciks[t]]
    print(f"{len(pending)} tickers left for Tiingo (~{len(pending) / 45:.1f} h)", file=sys.stderr)
    for i, t in enumerate(pending, 1):
        try:
            prices(t)
        except Exception as e:
            print(f"  FAILED tiingo {t}: {e!r}", file=sys.stderr)
        if i % 10 == 0:
            print(f"  tiingo {i}/{len(pending)}", file=sys.stderr)

    dates = rebalance_dates(sorted(prices("SPY")))
    rows = []
    for rd in dates:
        for t in cal.symbols_on(rd):
            if t in EXCLUDE:
                continue
            px = (prices(t, allow_tiingo=not yf_only) or {}).get(rd.isoformat())
            rows.append({"date": rd.isoformat(), "ticker": t, "price": px,
                         "ep": None, "bp": None, "ibp": None, "ibp_org": None,
                         "rd_cap": None, "org_cap": None, "mcap": None,
                         "mom": momentum(t, rd) if px is not None else None,
                         **score(ciks[t], px, rd), "ciks": " ".join(ciks[t])})
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "panel.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    (OUT / "vendors.json").write_text(json.dumps({t: vendor(t) for t in tickers}, indent=0))
    print(Counter(vendor(t) for t in tickers), file=sys.stderr)
    print(f"{len(dates)} rebalances, {len(rows)} rows -> {OUT / 'panel.csv'}", file=sys.stderr)
    print(Counter(r["why"] or "ok" for r in rows), file=sys.stderr)


if __name__ == "__main__":
    main()
