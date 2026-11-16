"""Pre-declared candidates for idea 16458973, scored per period from the event files.

T1 FAITHFUL  (the idea as stated): 1h n=2 fractal breaks, fast arrival (v15 in the top 40% of
             IS), every tier, size 1x fractal / 1.5x session / 2x day, exit H=120 min.
T2 REFINED   (from the IS dissection): 1h n=2 fractal that is the current day extreme, break
             12:00-18:00 UTC (NY), no speed condition, size 1x, exit H=120 min.
Exits compared on IS only: fixed-time (unstopped) vs 0.5 ATR stop.  Units: ATR(4h,20) per trade;
net = gross - $0.20/oz (1x) or $0.40/oz (2x costs).

Run:  .venv/bin/python research/rapid_break/rapid_break_candidates.py [--holdout]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[2] / "research" / "output"
W = {"fractal": 1.0, "session": 1.5, "day": 2.0}


def load() -> pd.DataFrame:
    ev = pd.read_parquet(OUT / "rapid_break_1h_2.parquet")
    edges = ev.loc[ev.t < "2023-01-01", "v15"].quantile([0.2, 0.4, 0.6, 0.8]).to_numpy()
    ev["fast"] = ev.v15 >= edges[2]
    ev["ny"] = ev.t.dt.hour.between(12, 17)
    ev["period"] = np.where(ev.t < "2023-01-01", "IS 2018-22", "HOLDOUT 2023-26.08")
    return ev


def score(ev: pd.DataFrame, w: pd.Series, col: str, stopped: bool) -> dict:
    ok = ev[col].notna()
    ev, w = ev[ok], w[ok]
    unit = 0.5 if stopped else 1.0          # R -> ATR for the stopped column
    g = ev[col] * unit
    out = {"n": len(ev)}
    for k, cost in (("net1x", 0.20), ("net2x", 0.40)):
        pnl = w * (g - cost / ev.atr)
        out[k] = pnl.sum() / w.sum()
        out[k + "_t"] = pnl.mean() / pnl.std() * np.sqrt(len(pnl))
    out["gross"] = (w * g).sum() / w.sum()
    drift, ctrl = ev["d" + col[1:]], ev["c" + col[1:]]
    out["excess_vs_ctrl"] = (w * (drift - ctrl)).sum() / w.sum()
    yrs = (w * (g - 0.20 / ev.atr)).groupby(ev.t.dt.year).sum()
    out["pos_years"] = f"{(yrs > 0).sum()}/{len(yrs)}"
    return out


def main() -> None:
    ev = load()
    periods = ["IS 2018-22"] + (["HOLDOUT 2023-26.08"] if "--holdout" in sys.argv else [])
    rows = []
    for p in periods:
        e = ev[ev.period == p]
        t1 = e[e.fast]
        t2 = e[(e.tier == "day") & e.ny]
        cands = (("T1 faithful", t1, t1.tier.map(W)), ("T2 refined", t2, pd.Series(1.0, t2.index)))
        for name, sub, w in cands:
            for col, stopped in (("d120", False), ("r120", True), ("d60", False)):
                if p != "IS 2018-22" and col != "r120":
                    continue                     # exit fixed on IS; holdout sees one column
                rows.append({"period": p, "cand": name, "exit": col, **score(sub, w, col, stopped)})
    print(pd.DataFrame(rows).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
