"""Dissect the value baseline (idea 21060772) on in-sample data only.

Portfolio sorts straight from the point-in-time panel: every quarter, bucket the scored
S&P 500 names into quintiles of a valuation yield and measure each bucket's next-quarter
return (close of the rebalance day to close of the next one, dividend-adjusted yfinance
bars). This is a diagnosis, not a trial -- it scores no strategy, it shows *where* the
value premium did or did not show up:

* monotonicity: does return rise from expensive (Q5) to cheap (Q1)?
* by year: is the shortfall one regime or every year?
* sector: does ranking within an SEC SIC division (cheap *for its industry*) behave
  differently from the raw ranking, which is mostly a bet on energy and banks?
* quality: cheap and profitable vs cheap and loss-making (value traps).

Window: rebalances up to 2021-09-30 (returns end 2021-12-31). The holdout is not read.
Output: research/output/value/dissect_*.csv and a printed summary.

    .venv/bin/python research/value/dissect.py [metric ...]    # default: ep bp
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "research" / "output" / "value"
BARS = ROOT / ".hermes_cache" / "bars" / "yfinance"
SIC_DIR = ROOT / ".hermes_cache" / "pit" / "value_sic"
UA = {"User-Agent": "Hermes research ivan.osipchyk.work@gmail.com"}
IS_LAST_REBALANCE = "2021-09-30"
METRICS = [m for m in sys.argv[1:] if not m.startswith("-")] or ["ep", "bp"]


def sic(cik: str) -> int | None:
    path = SIC_DIR / f"{cik}.json"
    if not path.exists():
        time.sleep(0.12)
        r = requests.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                         headers=UA, timeout=30)
        code = r.json().get("sic") if r.ok else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sic": code}))
    code = json.loads(path.read_text())["sic"]
    return int(code) if code else None


def division(code: int | None) -> str:
    """SEC SIC division, with finance and utilities split out (they drive B/P)."""
    if code is None:
        return "?"
    for lo, hi, name in [(100, 999, "agri"), (1000, 1499, "mining/energy"),
                         (1500, 1799, "construction"), (2000, 3999, "manufacturing"),
                         (4000, 4899, "transport/telecom"), (4900, 4999, "utilities"),
                         (5000, 5999, "trade"), (6000, 6799, "finance"),
                         (7000, 8999, "services")]:
        if lo <= code <= hi:
            return name
    return "other"


def closes(tickers: list[str]) -> pd.DataFrame:
    cols = {}
    for t in tickers:
        p = BARS / f"{t.replace('.', '-')}_1d.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            idx = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("America/New_York")
            cols[t] = pd.Series(df["close"].values, index=idx.dt.date.astype(str))
    return pd.DataFrame(cols).sort_index()


def main() -> None:
    panel = pd.read_csv(OUT / "panel.csv")
    panel = panel[panel["why"].isna() & (panel["date"] <= IS_LAST_REBALANCE)].copy()
    dates = sorted(panel["date"].unique())
    px = closes(sorted(panel["ticker"].unique()))
    nxt = dict(zip(dates, [*dates[1:], "2021-12-31"], strict=True))

    def fwd(row) -> float:
        a, b = row["date"], nxt[row["date"]]
        s = px.get(row["ticker"])
        if s is None:
            return np.nan
        s = s.loc[a:b].dropna()
        if s.empty or s.index[0] != a:
            return np.nan
        return s.iloc[-1] / s.iloc[0] - 1   # a name dropped mid-quarter keeps its last close

    panel["ret"] = panel.apply(fwd, axis=1)
    panel = panel.dropna(subset=["ret"])
    panel["div"] = [division(sic(c.split()[0])) for c in panel["ciks"].astype(str)]
    panel["year"] = panel["date"].str[:4]
    panel["ew"] = panel.groupby("date")["ret"].transform("mean")
    panel["xs"] = panel["ret"] - panel["ew"]

    def quint(g: pd.Series) -> pd.Series:   # 1 = cheapest (highest yield)
        return pd.qcut(g.rank(method="first", ascending=False), 5, labels=[1, 2, 3, 4, 5])

    for m in METRICS:
        s = panel.dropna(subset=[m])
        s = s.assign(q=s.groupby("date")[m].transform(quint).astype(int),
                     qs=s.groupby(["date", "div"])[m].transform(
                         lambda g: quint(g) if len(g) >= 5 else pd.Series(np.nan, g.index)))
        print(f"\n=== {m.upper()}: mean next-quarter excess return vs EW, % (n quarters="
              f"{s['date'].nunique()}) ===")
        raw = s.groupby("q")["xs"].mean() * 100
        neu = s.dropna(subset=["qs"]).groupby("qs")["xs"].mean() * 100
        print(pd.DataFrame({"raw quintile": raw.round(2),
                            "within-sector quintile": neu.round(2).values}))
        by_year = s[s["q"] == 1].groupby("year")["xs"].mean().mul(100).round(2)
        by_year_n = s[s["qs"] == 1].groupby("year")["xs"].mean().mul(100).round(2)
        print("cheapest quintile excess by year (raw | within-sector):")
        print(pd.DataFrame({"raw": by_year, "sector-neutral": by_year_n}).T.to_string())
        q1 = s[s["q"] == 1]
        print("cheapest quintile, sector mix vs universe (%):")
        mix = pd.DataFrame({"Q1": q1["div"].value_counts(normalize=True),
                            "all": s["div"].value_counts(normalize=True)}).mul(100).round(1)
        print(mix.sort_values("Q1", ascending=False).head(6).to_string())
        print("Q1 excess by sector (%/qtr, n):")
        print(q1.groupby("div")["xs"].agg(["mean", "count"]).assign(
            mean=lambda d: (d["mean"] * 100).round(2)).sort_values("count",
                                                                    ascending=False).head(6))
        s.to_csv(OUT / f"dissect_{m}.csv", index=False)

    # Quality: among the cheapest B/P quintile, profitable vs loss-making.
    s = panel.dropna(subset=["bp"])
    s = s.assign(q=s.groupby("date")["bp"].transform(quint).astype(int))
    q1 = s[s["q"] == 1]
    print("\n=== cheapest B/P quintile split by trailing earnings ===")
    print(q1.assign(profitable=q1["ep"] > 0).groupby("profitable")["xs"]
          .agg(["mean", "count"]).assign(mean=lambda d: (d["mean"] * 100).round(2)))


if __name__ == "__main__":
    sys.exit(main())
