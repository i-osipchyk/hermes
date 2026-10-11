"""Filters on the TradingView-window Tokyo breakout (range 00:00-09:00 UTC), gold.

Needs research/output/tokyo09_XAUUSD.parquet from tokyo_multi_market.py.  Exit = G4 (1 ATR stop,
flat at 20:55 UTC); R after costs.

  1  width filter, two yardsticks declared up front, top fifth with the cut-off from 2018-22:
       A  Tokyo range / ATR(4h,20)
       B  Tokyo range / median of the previous 20 Tokyo ranges
     chosen on 2018-22, then 2023-26 scored once (+ random-subset p, by year), silver for reference
  2  the "level held >= 2h" rule inside the width filter (2018-22 only)
  3  approach speed on wide days (2018-22 only): distance from the level 1/3/5 min before the
     trigger minute (1m), last 5m close, 2h before, minutes pressing within 0.1 ATR (last 30)

Run:  .venv/bin/python research/tokyo/tokyo_filters.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aero"))
import tokyo_multi_market as tm  # noqa: E402

OUT = tm.OUT
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
COST = tm.COST


def with_ranges(sym: str) -> pd.DataFrame:
    m = tm.load(sym)
    ts = m.index
    mins = ts.hour * 60 + ts.minute
    tok = m[(mins < 540) & (ts.weekday < 5)]
    dr = tok.groupby(tok.index.normalize()).agg(h=("high", "max"), l=("low", "min"), n=("high", "size"))
    dr = dr[dr.n >= 50]
    dr["rng"] = dr.h - dr.l
    dr["med20"] = dr.rng.shift(1).rolling(20, min_periods=15).median()   # previous sessions only
    ev = pd.read_parquet(OUT / f"tokyo09_{sym}.parquet")
    ev["date"] = ev.t.dt.normalize()
    ev = ev.join(dr[["h", "l", "rng", "med20"]], on="date")
    ev["rel20"] = ev.rng / ev.med20
    return ev


def width_filter(ev: pd.DataFrame, sym: str) -> tuple[str, np.ndarray]:
    is_ = (ev.t < IS_END).to_numpy()
    res = {}
    for name, col in (("A range/ATR(4h)", "range_atr"), ("B range/20-day median", "rel20")):
        cut = np.nanquantile(ev.loc[is_, col], 0.8)
        sel = (ev[col] >= cut).to_numpy()
        a = ev[sel & is_]
        pos = sum(a[a.t.dt.year == y].G4.mean() > 0 for y in range(2018, 2023))
        res[name] = (sel, dict(cutoff=round(cut, 3), n=len(a), R=round(a.G4.mean(), 3), years_pos=f"{pos}/5"))
    print(f"\n[1] {sym} width filter, 2018-22 (all breaks R {ev[is_].G4.mean():+.3f}):")
    print(pd.DataFrame({k: v[1] for k, v in res.items()}).T.to_string())
    best = max(res, key=lambda k: res[k][1]["R"])
    return best, res[best][0]


def main() -> None:
    gold = with_ranges("XAUUSD")
    best, sel = width_filter(gold, "XAUUSD")
    is_ = (gold.t < IS_END).to_numpy()
    o = gold[sel & ~is_]
    rng = np.random.default_rng(0)
    pool = gold[~is_].G4.to_numpy()
    draws = np.array([rng.choice(pool, len(o), replace=False).mean() for _ in range(5000)])
    print(f"chosen: {best}; 2023-26 once: n={len(o)} R {o.G4.mean():+.3f} win {100 * (o.G4 > 0).mean():.1f}% "
          f"vs all {gold[~is_].G4.mean():+.3f}; P(random >=) = {(draws >= o.G4.mean()).mean():.3f}")
    print("by year:", gold[sel].groupby(gold[sel].t.dt.year).G4.mean().round(3).to_dict())
    silver = with_ranges("XAGUSD")
    col = "range_atr" if best.startswith("A") else "rel20"
    s_is = (silver.t < IS_END).to_numpy()
    s_sel = (silver[col] >= np.nanquantile(silver.loc[s_is, col], 0.8)).to_numpy()
    for lab, m in (("2018-22", s_is), ("2023-26", ~s_is)):
        x = silver[s_sel & m]
        gross = (x.G4 + COST["XAGUSD"] / x.atr).mean()
        print(f"silver {lab}: R {x.G4.mean():+.3f}, before costs {gross:+.3f} (n={len(x)})")

    I = gold[is_]
    held = I.age_h >= 2
    print("\n[2] width filter x level held >= 2h, gold 2018-22:")
    for name, col in (("A", "range_atr"), ("B", "rel20")):
        w = I[col] >= np.nanquantile(I[col], 0.8)
        for lab, m in (("any age", w), ("held >= 2h", w & held), ("held < 2h", w & ~held)):
            print(f"  {name} {lab:11s} n={int(m.sum()):4d} R {I[m].G4.mean():+.3f}")

    print("\n[3] approach speed on wide days (B), gold 2018-22, thirds:")
    m1 = pd.read_parquet(tm.BARS / "XAUUSD_1m.parquet")
    m1.index = pd.to_datetime(m1.ts, unit="s", utc=True)
    c1, h1, l1, t1 = (m1.close.to_numpy(float), m1.high.to_numpy(float), m1.low.to_numpy(float),
                      m1.index.tz_convert(None).to_numpy())
    m5 = tm.load("XAUUSD")
    c5, t5 = m5.close.to_numpy(float), m5.index.tz_convert(None).to_numpy()
    W = gold[(gold.t < IS_END) & (gold.rel20 >= np.nanquantile(I.rel20, 0.8))].copy()
    W["level"] = np.where(W.side > 0, W.h, W.l)
    feats = {k: [] for k in ("a1", "a3", "a5", "gap5m", "ext2h", "press30")}
    for e in W.itertuples():
        trig = e.level + e.side * 0.02 * e.atr
        s = np.searchsorted(t1, e.t.tz_convert(None).to_datetime64())
        j = next((k for k in range(s, min(s + 5, len(c1)))
                  if ((h1[k] >= trig) if e.side > 0 else (l1[k] <= trig))), None)
        p5 = np.searchsorted(t5, e.t.tz_convert(None).to_datetime64())
        if j is None or j < 121:
            for v in feats.values():
                v.append(np.nan)
            continue
        d, lvl, a = e.side, e.level, e.atr
        for k, px in (("a1", c1[j - 1]), ("a3", c1[j - 3]), ("a5", c1[j - 5]), ("gap5m", c5[p5 - 1]),
                      ("ext2h", c1[j - 120])):
            feats[k].append(d * (lvl - px) / a)
        near = e.side * (e.level - (h1[j - 30:j] if e.side > 0 else l1[j - 30:j])) / e.atr
        feats["press30"].append(int((near <= 0.1).sum()))
    for k, v in feats.items():
        W[k] = v
        g = W.groupby(pd.qcut(W[k], 3, duplicates="drop"), observed=True).G4
        parts = [f"{iv}: n={n} R {r:+.3f}" for iv, n, r in zip(g.size().index, g.size(), g.mean())]
        print(f"  {k:8s} " + "  ".join(parts))


if __name__ == "__main__":
    main()
