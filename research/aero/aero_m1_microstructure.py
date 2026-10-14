"""Does minute-level behaviour at the break explain the Aero EA's profitable selection?

aero_pattern_mining.py showed the EA's choices are ~85% predictable (AUC) from 5m-level
features, yet its picks earn +0.19R in the event sim while equally EA-like skipped breaks
earn ~0.  This asks whether the missing information is in the last minutes before the trigger.

Break minute  The first 1m bar inside the 5m event bar whose high (low) trades through
              level +/- $0.50.  Features use closed 1m bars before it, plus that minute's
              open -> trigger move (which has happened when the trigger prints).
Features      a1/a2/a3/a5/a10   distance from the level k minutes before the trigger minute
                                (close of minute j-k), ATR units -- approach speed
              t_010 / t_025     minutes since a close was >= 0.10 / 0.25 ATR from the level
              rng10_60          M1 range sum, last 10 min vs the 10-min average of the last hour
              vol3_60           tick volume, last 3 min vs the 3-min average of the last hour
              streak            consecutive closes in the break direction before the trigger
              straight10        |net| / gross path over the last 10 closes
              press30           minutes of the last 30 with the high within 0.10 ATR of the level
              rej30             minutes of the last 30 that touched within 0.05 ATR and closed
                                >= 0.10 ATR back (rejections from the level)
              trig_min          open -> trigger inside the trigger minute
              spike10           largest single M1 range in the last 10 min
Questions     1  AUC for "EA placed an order": all breaks, and within the 5m mimic's top decile
              2  5m mimic + M1 features, fitted on 2018-22 choices, AUC on 2023-26 choices
              3  event-sim net R (context, no new backtest) of the new mimic's top decile and of
                 the M1 features' quintiles, 2018-22 vs 2023-26

Run:  .venv/bin/python research/aero/aero_m1_microstructure.py   (after aero_pattern_mining.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.argv = [sys.argv[0], "5m"]
import aero_pattern_mining as apm  # noqa: E402

OUT = apm.OUT
M1 = Path(__file__).resolve().parents[2] / ".hermes_cache/bars/pepperstone/XAUUSD_1m.parquet"
SPLIT = apm.SPLIT
M1_FEATS = ["a1", "a2", "a3", "a5", "a10", "t_010", "t_025", "rng10_60", "vol3_60", "streak",
            "straight10", "press30", "rej30", "trig_min", "spike10"]


def m1_features(ev: pd.DataFrame) -> pd.DataFrame:
    m = pd.read_parquet(M1)
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    o, h, lo, c, v = (m[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    ts = m.index.to_numpy()
    start = np.searchsorted(ts, ev.t.to_numpy())
    rows = []
    for (_, e), s in zip(ev.iterrows(), start):
        d, L, a = e.side, e.level, e.atr
        trig = L + d * 0.5
        j = None
        for k in range(s, min(s + 5, len(m))):
            if (h[k] >= trig) if d > 0 else (lo[k] <= trig):
                j = k
                break
        if j is None or j < 61:
            rows.append({f: np.nan for f in M1_FEATS})
            continue
        dist = d * (L - c[j - 61:j]) / a                 # last 61 closes, oldest first
        rng = h[j - 60:j] - lo[j - 60:j]
        r = dict(a1=dist[-1], a2=dist[-2], a3=dist[-3], a5=dist[-5], a10=dist[-10])
        for thr, name in ((0.10, "t_010"), (0.25, "t_025")):
            far = np.flatnonzero(dist[::-1] >= thr)
            r[name] = far[0] + 1 if len(far) else 61
        r["rng10_60"] = rng[-10:].sum() / (rng.sum() / 6 + 1e-9)
        r["vol3_60"] = v[j - 3:j].sum() / (v[j - 60:j].sum() / 20 + 1e-9)
        moves = d * np.diff(c[j - 11:j])
        streak = 0
        for x in moves[::-1]:
            if x > 0:
                streak += 1
            else:
                break
        r["streak"] = streak
        r["straight10"] = abs(moves.sum()) / (np.abs(moves).sum() + 1e-9)
        hh = h[j - 30:j] if d > 0 else lo[j - 30:j]
        near = d * (L - hh) / a
        r["press30"] = int((near <= 0.10).sum())
        r["rej30"] = int(((near <= 0.05) & (dist[-30:] >= 0.10)).sum())
        r["trig_min"] = d * (trig - o[j]) / a
        r["spike10"] = rng[-10:].max() / a
        rows.append(r)
    return pd.concat([ev.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def main() -> None:
    ev = pd.read_parquet(OUT / "aero_pattern_mining.parquet")
    ev = m1_features(ev)
    ok = ev[M1_FEATS].notna().all(axis=1)
    print(f"breaks {len(ev)}, with M1 features {ok.sum()}; "
          f"EA picks {ev.ea.sum()} ({ev.ea[ok].sum()} with M1)")
    ev = ev[ok].reset_index(drop=True)
    y = ev.ea.to_numpy()
    top = (ev.mimic >= ev.mimic.quantile(0.9)).to_numpy()
    rows = [dict(feature=f, auc_all=apm.auc(ev[f].to_numpy(float), y),
                 auc_lookalikes=apm.auc(ev.loc[top, f].to_numpy(float), y[top]),
                 med_pick=np.nanmedian(ev.loc[y, f]), med_skip=np.nanmedian(ev.loc[~y, f]))
            for f in M1_FEATS]
    a = pd.DataFrame(rows)
    a["k"] = (a.auc_lookalikes - 0.5).abs()
    print("\n1) M1 features vs EA picks (AUC; look-alikes = 5m mimic top decile):")
    print(a.sort_values("k", ascending=False).drop(columns="k").round(3).to_string(index=False))

    print("\n2) mimic with and without M1 features (fit 2018-22 choices, score 2023-26)")
    tr = (ev.t < SPLIT).to_numpy()
    sets = (("5m only", apm.BASE + apm.EXTRA), ("5m + M1", apm.BASE + apm.EXTRA + M1_FEATS))
    for name, feats in sets:
        X = ev[feats].astype(float)
        mu, sd = X[tr].mean(), X[tr].std() + 1e-9
        Z = ((X - mu) / sd).fillna(0).to_numpy()
        w = apm.logistic(Z[tr], y[tr], lam=5.0)
        s = np.c_[np.ones(len(Z)), Z] @ w
        ev[f"score_{name}"] = s
        hi = s >= np.quantile(s[tr], 0.9)
        print(f"   {name:8s} AUC train {apm.auc(s[tr], y[tr]):.3f} test {apm.auc(s[~tr], y[~tr]):.3f} | "
              f"top decile: EA take {y[hi & ~tr].mean():.3f}, sim R 2018-22 {ev.net_r[hi & tr].mean():+.3f} "
              f"2023-26 {ev.net_r[hi & ~tr].mean():+.3f}")
        if name == "5m + M1":
            coef = pd.Series(w[1:], index=feats)
            m1 = coef[M1_FEATS].sort_values(key=np.abs, ascending=False)
            print("   M1 coefficients:", m1.round(2).to_dict())

    print("\n3) event-sim net R by M1 feature quintile (edges from 2018-22), context only:")
    for f in M1_FEATS:
        edges = np.unique(np.quantile(ev.loc[tr, f], [0, .2, .4, .6, .8, 1]))
        if len(edges) < 3:
            continue
        edges[0], edges[-1] = -np.inf, np.inf
        q = pd.cut(ev[f], edges, labels=False)
        t = pd.DataFrame({"18-22": ev[tr].groupby(q[tr]).net_r.mean(),
                          "23-26": ev[~tr].groupby(q[~tr]).net_r.mean(),
                          "EA_take": ev.groupby(q).ea.mean()}).round(3).T
        print(f"\n{f}")
        print(t.to_string())
    print(f"\nEA picks themselves: sim R 2018-22 {ev.net_r[y & tr].mean():+.3f} "
          f"2023-26 {ev.net_r[y & ~tr].mean():+.3f}")
    ev.to_parquet(OUT / "aero_m1_microstructure.parquet")


if __name__ == "__main__":
    main()
