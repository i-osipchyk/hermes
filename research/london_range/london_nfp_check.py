"""IS diagnosis of the NFP-Friday pocket in G_far (london_bracket_study.py). Not a trial.

Does it survive (a) entering after the print (08:35 / 08:45 ET open instead of 08:30) and
(b) a print-time cost ($1.00/oz round trip instead of $0.20)? Same range, target, stop, flat.
Also: the same rule on non-NFP Fridays as a control.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import london_bracket_study as lb  # noqa: E402


def main(holdout: bool = False) -> None:
    m = lb.load(holdout)
    o, h, lo = (m[k].to_numpy() for k in ("open", "high", "low"))
    ny, ldn = m.index.tz_convert(lb.NY), m.index.tz_convert(lb.LDN)
    nym = np.asarray(ny.hour * 60 + ny.minute)
    ldnm = np.asarray(ldn.hour * 60 + ldn.minute)
    nyd, ldd = np.asarray(ny.date), np.asarray(ldn.date)
    out = []
    for day, ii in pd.Series(np.arange(len(m))).groupby(nyd):
        ii = ii.to_numpy()
        ts = pd.Timestamp(day)
        if ts.weekday() != 4 or m.index[ii[0]] < m.attrs["lo"]:
            continue
        rng = ii[(ldnm[ii] >= 480) & (ldd[ii] == day) & (nym[ii] < 510)]
        end = ii[nym[ii] == 1005]
        if len(rng) < 40 or not len(end):
            continue
        H, L = h[rng].max(), lo[rng].min()
        for ent in (510, 515, 525):
            s = ii[nym[ii] == ent]
            if not len(s):
                continue
            f = s[0]
            p = o[f]
            if not (L < p < H):
                continue
            d = 1 if H - p > p - L else -1
            tp, sl = (H, L) if d > 0 else (L, H)
            px, why = lb.walk(d, p, sl, tp, h, lo, o, f, end[0])
            risk = abs(p - sl)
            for cost in (0.2, 1.0):
                out.append(dict(day=day, nfp=ts.day <= 7, entry=f"{ent // 60:02d}:{ent % 60:02d}",
                                cost=cost, R=(d * (px - p) - cost) / risk, rr=abs(tp - p) / risk))
    t = pd.DataFrame(out)
    t["year"] = pd.to_datetime(t.day).dt.year
    g = t.groupby(["nfp", "entry", "cost"]).R
    print(pd.DataFrame({"n": g.size(), "win": g.apply(lambda x: (x > 0).mean()), "netR": g.mean(),
                        "t": g.mean() / (g.std() / np.sqrt(g.size())),
                        "medR_if_win": t[t.R > 0].groupby(["nfp", "entry", "cost"]).R.median()}).round(3))
    print("\nNFP, 08:30 entry, $1.00 cost, by year:")
    q = t[(t.nfp) & (t.entry == "08:30") & (t.cost == 1.0)]
    print(q.groupby("year").R.agg(["size", "mean", "sum"]).round(2))
    print("\ntop-5 share of total positive R:",
          round(q.R.nlargest(5).sum() / q.R[q.R > 0].sum(), 2), " sum R", round(q.R.sum(), 1))


if __name__ == "__main__":
    main("--holdout" in sys.argv)
