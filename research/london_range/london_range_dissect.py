"""Dissect the IS event study (london_range_study.py must have run). Diagnosis, not trials.

For each conditioning variable: London-edge touch rate vs a shuffled control drawn from days
in the same bucket (so distance mix matches), and the mean net ATR of each rule per bucket.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[2] / "research/output/london_range"


def ctl(e: pd.DataFrame, side: str, rng) -> float:
    dist, mfe = (e.up, e.mfe_up) if side == "h" else (e.dn, e.mfe_dn)
    return float(np.mean([(mfe.to_numpy() >= rng.permutation(dist.to_numpy())).mean()
                          for _ in range(50)]))


def main() -> None:
    ev = pd.read_csv(OUT / "events_is.csv")
    tr = pd.read_csv(OUT / "trades_is.csv")
    ev = ev[pd.to_datetime(ev.day).dt.year >= 2018]
    tr = tr[pd.to_datetime(tr.day).dt.year >= 2018]
    ev["width"] = (ev.H - ev.L) / ev.atr
    ev["med20"] = ev.width.rolling(20, min_periods=15).median().shift()
    ev["wrel"] = ev.width / ev.med20
    ev["pos"] = (ev.p0 - ev.L) / (ev.H - ev.L)
    rng = np.random.default_rng(3)
    cuts = {
        "wrel (vs prev 20)": pd.qcut(ev.wrel, 4),
        "width ATR": pd.qcut(ev.width, 4),
        "p0 pos in range": pd.cut(ev.pos, [0, .2, .4, .6, .8, 1]),
        "weekday": pd.to_datetime(ev.day).dt.weekday,
    }
    for name, b in cuts.items():
        print(f"\n== {name}")
        print("bucket                 n  touchH ctlH   touchL ctlL  | A_far  A_near B_fade C_first")
        e2 = ev.assign(b=b).dropna(subset=["b"])
        t2 = tr.merge(e2[["day", "b"]], on="day")
        piv = t2.pivot_table(index="b", columns="rule", values="r", aggfunc="mean", observed=True)
        for bk, e in e2.groupby("b", observed=True):
            p = piv.loc[bk] if bk in piv.index else {}
            print(f"{str(bk):20s}{len(e):5d}  {e.touch_h.mean():.3f} {ctl(e,'h',rng):.3f}  "
                  f"{e.touch_l.mean():.3f} {ctl(e,'l',rng):.3f}  | "
                  + " ".join(f"{p.get(r, np.nan):+.3f}" for r in ("A_far", "A_near", "B_fade", "C_first")))
    print("\n== by side (mean net ATR, n)")
    print(tr.groupby(["rule", "d"]).r.agg(["mean", "count"]).round(3))
    print("\n== B_fade by entry hour ET")
    print(tr[tr.rule == "B_fade"].groupby("hour").r.agg(["mean", "count"]).round(3))
    b = tr[tr.rule == "B_fade"]
    print(f"\nB_fade: target {b.tgt.median():.2f} ATR; win {np.mean(b.r>0):.3f}; "
          f"mean R {b.R.mean():+.3f}; top-10 share of gross wins "
          f"{b.r.nlargest(10).sum()/b.r[b.r>0].sum():.2f}")


if __name__ == "__main__":
    main()
