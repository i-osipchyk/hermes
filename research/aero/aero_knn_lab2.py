"""kNN lab, round 2 (see aero_knn_lab.py for the dataset and protocol).

E8  two-stage: the EA's observable filter first (level held >= 2h AND after 12:00 server),
    then the kNN on hand+raw, outcome target; top third of scores within the filtered set
E9  weighted distance: each standardized column scaled by |corr(column, R)| on 2018-22 rows only
E10 blend of the two configs most consistent across all checks in round 1 (E1 hand/lock/200 and
    E6 hand+raw/R/200), z-scored and averaged -- chosen AFTER seeing the checks: partly fitted
All selection thresholds come from 2019-22 scores.  Reported: lift of the selected set over the
universe it was chosen from, gold 2019-22 / 2023-26 / silver 2026, plus per-year lift on gold.

Run:  .venv/bin/python research/aero/aero_knn_lab2.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import aero_knn_lab as lab  # noqa: E402

RAW = [f"{b}_{i}" for b, n in lab.BLOCKS.items() for i in range(n)]
HR = lab.HAND + RAW


def filt(df: pd.DataFrame) -> np.ndarray:
    srv_h = (df.hcos.to_numpy(), df.hsin.to_numpy())
    hour = (np.degrees(np.arctan2(srv_h[1], srv_h[0])) % 360) / 15
    return ((np.expm1(df.lage) >= 2) & (hour >= 12)).to_numpy()


def report(name: str, wf: pd.DataFrame, s: np.ndarray, sil: pd.DataFrame, sv: np.ndarray,
           mask_wf: np.ndarray, mask_sil: np.ndarray, q: float) -> dict:
    is_ = (wf.t < lab.IS_END).to_numpy()
    base_is = mask_wf & is_ & ~np.isnan(s)
    thr = np.nanquantile(s[base_is], q)
    out = dict(config=name)
    for lab_, d, sc, m in (("IS", wf[is_], s[is_], mask_wf[is_]), ("OOS", wf[~is_], s[~is_], mask_wf[~is_]),
                           ("SIL", sil, sv, mask_sil)):
        ok = m & ~np.isnan(sc)
        top = ok & (sc >= thr)
        out[f"{lab_}_lift"] = d.r[top].mean() - d.r[ok].mean()
        out[f"{lab_}_top_n"] = int(top.sum())
        out[f"{lab_}_univ_R"] = d.r[ok].mean()
    ok = mask_wf & ~np.isnan(s)
    yr = wf.t.dt.year.to_numpy()
    out["years_positive"] = sum(
        (wf.r[ok & (yr == y) & (s >= thr)].mean() - wf.r[ok & (yr == y)].mean()) > 0
        for y in range(2019, 2027))
    print(out, flush=True)
    return out


def main(only_e8: bool = False) -> None:
    gold, silver = lab.build("XAUUSD"), lab.build("XAGUSD")
    for d in (gold, silver):
        d["lock"] = d.locked.astype(float)
    wf = gold[gold.t >= lab.WF_START].reset_index(drop=True)
    rows = []
    # E8 two-stage
    fg, fs = filt(gold), filt(silver)
    g8, w8 = gold[fg].reset_index(drop=True), filt(wf)
    for k in (25, 50):
        s = np.full(len(wf), np.nan)
        s[w8] = lab.knn_scores(g8, wf[w8], HR, "r", k, min_train=150)
        sv = np.full(len(silver), np.nan)
        sv[fs] = lab.knn_scores(g8, silver[fs], HR, "r", k, min_train=150)
        rows.append(report(f"E8 filter+hand+raw R k={k}", wf, s, silver, sv, w8, fs, 2 / 3))
    if only_e8:
        return
    # E9 weighted distance (weights from 2018-22 rows only)
    early = gold[gold.t < lab.IS_END]
    w = np.array([abs(np.corrcoef(early[c].fillna(early[c].median()), early.r)[0, 1]) for c in HR])
    w = np.nan_to_num(w) / np.nanmax(w)
    print("E9 top weights:", pd.Series(w, index=HR).sort_values(ascending=False).head(8).round(2).to_dict())
    allw, alls = np.ones(len(wf), bool), np.ones(len(silver), bool)
    for k in (50, 200):
        s = lab.knn_scores(gold, wf, HR, "r", k, weights=w)
        sv = lab.knn_scores(gold, silver, HR, "r", k, weights=w)
        rows.append(report(f"E9 weighted hand+raw R k={k}", wf, s, silver, sv, allw, alls, 0.9))
    # E10 blend (post-hoc)
    def z(x: np.ndarray, ref: np.ndarray) -> np.ndarray:
        return (x - np.nanmean(ref)) / np.nanstd(ref)
    is_ = (wf.t < lab.IS_END).to_numpy()
    a = lab.knn_scores(gold, wf, lab.HAND, "lock", 200)
    b = lab.knn_scores(gold, wf, HR, "r", 200)
    av, bv = lab.knn_scores(gold, silver, lab.HAND, "lock", 200), lab.knn_scores(gold, silver, HR, "r", 200)
    s = (z(a, a[is_]) + z(b, b[is_])) / 2
    sv = (z(av, a[is_]) + z(bv, b[is_])) / 2
    rows.append(report("E10 blend (post-hoc)", wf, s, silver, sv, allw, alls, 0.9))
    pd.DataFrame(rows).to_csv(lab.OUT / "aero_knn_lab2.csv", index=False)


if __name__ == "__main__":
    main(only_e8="--e8" in sys.argv)
