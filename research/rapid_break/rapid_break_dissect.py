"""In-sample (2018-22) dissection of rapid_break events: where does the day-tier drift live,
and what is the fractal-only/slow pocket.  Diagnosis only -- nothing here is scored on 2023+."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[2] / "research" / "output"
COST = 0.20


def show(ev: pd.DataFrame, by, H: int = 120) -> None:
    ev = ev.assign(net=ev[f"d{H}"] - COST / ev.atr, exc=ev[f"d{H}"] - ev[f"c{H}"])
    t = ev.groupby(by, observed=True).agg(n=("net", "count"), gross=(f"d{H}", "mean"),
                                          excess=("exc", "mean"), net=("net", "mean"),
                                          cost=("atr", lambda a: (COST / a).mean()))
    print(t.round(4).to_string(), "\n")


for rule, n in [("30min", 1), ("1h", 2)]:
    ev = pd.read_parquet(OUT / f"rapid_break_{rule}_{n}.parquet")
    ev = ev[ev.t < "2023-01-01"].copy()
    edges = ev.v15.quantile([0.2, 0.4, 0.6, 0.8]).to_numpy()
    ev["vq"] = np.digitize(ev.v15, edges)
    ev["year"] = ev.t.dt.year
    ev["hour"] = (ev.t.dt.hour // 3) * 3
    ev["agebin"] = pd.cut(ev.age_h, [0, 2, 6, 24, 72, 1e9])
    print(f"########## {rule} n={n}")
    day = ev[ev.tier == "day"]
    print("DAY tier, H=120: year / side / utc hour / age / vq")
    for by in ("year", "side", "hour", "agebin", "vq"):
        show(day, by)
    fr = ev[ev.tier == "fractal"].copy()
    # is the fractal-only level ABOVE today's whole range (older level) or inside it?
    print("FRACTAL-only, H=60: age bin x slow(vq<=2)")
    fr["slow"] = fr.vq <= 2
    show(fr, ["slow", "agebin"], 60)
    show(fr, ["slow", "year"], 60)
