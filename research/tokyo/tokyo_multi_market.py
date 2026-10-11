"""Tokyo-range breakout across markets, TradingView session window, "held >= 2h" rule.

Range    high/low of the 00:00-09:00 UTC 5m bars (TradingView's Tokyo session), weekdays.
Break    first 5m bar in 09:00-20:00 UTC trading 0.02 x ATR(4h,20) beyond the range edge; each
         side is its own event.  No range-width filter.
Held     age = time from the last Tokyo bar that printed the broken extreme to the break bar;
         "held" = age >= 2h (the Aero finding).  Reported: all breaks vs held breaks.
Exits    5m bars, stop checked before target on every bar including the fill bar:
           G4   stop 1 ATR, flat at the close of the 20:55 UTC bar
           G2b  stop at the opposite range edge, target 2R, flat at 20:55 UTC
Units    R = (move - round-trip cost) / ATR, costs per market in COST (price units).
Periods  2018-22 vs 2023-26 (markets with history before 2023-09 only; others report 2023-26).

Run:  .venv/bin/python research/tokyo/tokyo_multi_market.py [MARKET ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aero"))
ONLY = sys.argv[1:]                     # optional market list
sys.argv = [sys.argv[0], "5m"]
import aero_knn_filter as akf  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BARS = ROOT / ".hermes_cache/bars/pepperstone"
OUT = akf.OUT
COST = {"XAUUSD": 0.20, "XAGUSD": 0.025, "EURUSD": 0.00012, "GBPUSD": 0.00016, "EURGBP": 0.00014,
        "USDJPY": 0.014, "NAS100": 1.5, "US500": 0.6, "GER40": 1.5, "UK100": 1.2, "JPN225": 10.0}
IS_END = pd.Timestamp("2023-01-01", tz="UTC")


def load(sym: str) -> pd.DataFrame:
    if sym == "XAGUSD":
        m = pd.read_parquet(BARS / "XAGUSD_1m.parquet")
        m.index = pd.to_datetime(m.ts, unit="s", utc=True)
        m = m.resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    else:
        m = pd.read_parquet(BARS / f"{sym}_5m.parquet")
        m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    m = m[m.index < "2026-10-01"].sort_index()
    m = m[~m.index.duplicated()]
    h4 = m.resample("4h").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    s = pd.Series(akf.wilder(akf.true_range(h4), 20).values, index=h4.index + pd.Timedelta("4h"))
    m["atr"] = s.reindex(m.index, method="ffill").to_numpy()
    return m


def exit_px(geo: str, d: int, entry: float, a: float, opp: float, j: int, last: int,
            o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray) -> float:
    if geo == "G4":
        stop, tp = entry - d * a, None
    else:
        stop = opp
        tp = entry + d * 2 * abs(entry - opp)
    for q in range(j, last + 1):
        if (lo[q] <= stop) if d > 0 else (h[q] >= stop):
            if q == j:                      # fill bar: path unknown, assume the stop (conservative)
                return stop
            return min(stop, o[q]) if d > 0 else max(stop, o[q])
        if tp is not None and ((h[q] >= tp) if d > 0 else (lo[q] <= tp)):
            return tp
    return c[last]


def events(sym: str) -> pd.DataFrame:
    m = load(sym)
    o, h, lo, c, atr = (m[k].to_numpy(float) for k in ("open", "high", "low", "close", "atr"))
    ts = m.index
    mins = (ts.hour * 60 + ts.minute).to_numpy()
    rows = []
    for dte, idx in pd.Series(np.arange(len(m)), index=ts.normalize()).groupby(level=0):
        if dte.weekday() >= 5:
            continue
        ii = idx.to_numpy()
        tok, win = ii[mins[ii] < 540], ii[(mins[ii] >= 540) & (mins[ii] < 1200)]
        upto = ii[mins[ii] <= 1255]
        if len(tok) < 50 or len(win) < 24 or np.isnan(atr[win[0]]):
            continue
        last = upto[-1]
        hi, lw = h[tok].max(), lo[tok].min()
        a = atr[win[0]]
        for d, L, opp in ((1, hi, lw), (-1, lw, hi)):
            trig = L + d * 0.02 * a
            hit = win[(h[win] >= trig) if d > 0 else (lo[win] <= trig)]
            if not len(hit):
                continue
            j = hit[0]
            entry = max(o[j], trig) if d > 0 else min(o[j], trig)
            setb = tok[np.flatnonzero((h[tok] >= L) if d > 0 else (lo[tok] <= L))[-1]]
            r = dict(t=ts[j], side=d, atr=a, range_atr=(hi - lw) / a,
                     age_h=(ts[j] - ts[setb]).total_seconds() / 3600)
            for g in ("G4", "G2b"):
                r[g] = (d * (exit_px(g, d, entry, a, opp, j, last, o, h, lo, c) - entry) - COST[sym]) / a
            rows.append(r)
    return pd.DataFrame(rows)


def main() -> None:
    syms = ONLY or [s for s in COST if (BARS / f"{s}_5m.parquet").exists() or s == "XAGUSD"]
    out, by_year = [], {}
    for sym in syms:
        ev = events(sym)
        ev.to_parquet(OUT / f"tokyo09_{sym}.parquet")
        held = ev.age_h >= 2
        is_ = ev.t < IS_END
        row = dict(market=sym, first=ev.t.min().strftime("%Y-%m"), n=len(ev), held_share=held.mean())
        for lab, msk in (("18-22", is_), ("23-26", ~is_)):
            for g in ("G4", "G2b"):
                row[f"{g} all {lab}"] = ev[msk][g].mean() if msk.any() else np.nan
                row[f"{g} held {lab}"] = ev[msk & held][g].mean() if (msk & held).any() else np.nan
        yh = ev[held].groupby(ev[held].t.dt.year).G4.mean()
        ya = ev.groupby(ev.t.dt.year).G4.mean()
        row["held>all years"] = f"{int((yh > ya.reindex(yh.index)).sum())}/{len(yh)}"
        row["held G4>0 years"] = f"{int((yh > 0).sum())}/{len(yh)}"
        by_year[sym] = pd.DataFrame({"all": ya, "held": yh})
        out.append(row)
        print(sym, "done", flush=True)
    t = pd.DataFrame(out).set_index("market")
    pd.set_option("display.width", 250)
    print("\nmean R per trade (after costs); 'held' = broken extreme >= 2h old")
    print(t.round(3).to_string())
    print("\nG4 by year, all vs held:")
    for sym, df in by_year.items():
        print(f"{sym}: " + "  ".join(f"{y}: {r['all']:+.2f}/{r['held']:+.2f}" for y, r in df.iterrows()))
    t.to_csv(OUT / "tokyo09_multi_market.csv")


if __name__ == "__main__":
    main()
