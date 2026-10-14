"""Mine the Aero EA's 2018-2026.08 tester log for the pattern behind its selection.

No backtest: labels are the EA's own choices and its own logged P&L.  The only simulated
quantity read here is net_r from aero_knn_filter.py (5m), shown for context.

Universe  5m day-range break events (research/output/aero_approach_speed.parquet, which is
          aero_knn_filter's 5m events + approach-speed features + EA labels, restricted to
          moments the EA was free to act).
Extra     pull_depth  how far price retreated from the level between setting it and breaking
                      it, in ATR(4h,20) -- "held through a pullback"
features  set_hour    server hour at which the broken extreme was set
          round10     distance of the level to the nearest $10, in $
          dom / prev_higher / has_later   M30 n=1 swing dominance at the same level
          vs_dayopen, prevday_dir, beyond_pd   daily context
Parts     1  univariate AUC of every feature for "EA placed an order" (all breaks, and old
             levels >= 2h only, where most picks live)
          2  a mimic: L2 logistic regression and a depth-3 tree, fitted on 2018-22 choices,
             scored (AUC) on 2023-26 choices
          3  the EA's own trades: features of winners vs losers (logged P&L per episode)

Run:  .venv/bin/python research/aero/aero_pattern_mining.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.argv = [sys.argv[0], "5m"]
import aero_approach_speed as aas  # noqa: E402
import aero_knn_filter as akf  # noqa: E402

OUT = akf.OUT
SPLIT = pd.Timestamp("2023-01-01", tz="UTC")
BASE = ["r30m", "r2h", "r24h", "r5d", "r20d", "r13w", "r26w", "pos_week", "pos_month", "dist_pd",
        "dist_pw", "vr", "dayrange", "level_age", "n_breaks", "touches", "hour_sin", "hour_cos",
        "dow_sin", "dow_cos", "m30_1", "m30_2", "m30_4", "gap_now", "intrabar", "m30_range",
        "since_far", "eff_1h"]
EXTRA = ["pull_depth", "set_hour", "round10", "vs_dayopen", "prevday_dir", "beyond_pd",
         "prev_higher", "has_later", "srv_hour"]


def auc(x: np.ndarray, y: np.ndarray) -> float:
    ok = ~np.isnan(x)
    x, y = x[ok], y[ok]
    r = pd.Series(x).rank().to_numpy()
    n1, n0 = y.sum(), len(y) - y.sum()
    return float((r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else np.nan


def add_extra(ev: pd.DataFrame) -> pd.DataFrame:
    m = akf.load()
    m = m[m.index >= pd.Timestamp("2017-11-01", tz="UTC")]
    hi, lo = m.high.to_numpy(), m.low.to_numpy()
    pos = m.index.get_indexer(ev.t)
    srv = m.srv
    dopen = m.groupby("sday").open.first()
    dclose = m.groupby("sday").close.last()
    dhi, dlo = m.groupby("sday").high.max(), m.groupby("sday").low.min()
    days = dopen.index.to_numpy()
    sday = m.sday.to_numpy()
    rows = []
    for (_, e), p in zip(ev.iterrows(), pos):
        d, L, a = e.side, e.level, e.atr
        set_p = int(round(p - e.level_age * 12))
        w = slice(max(set_p, 0), p)
        depth = (L - lo[w].min()) / a if d > 0 else (hi[w].max() - L) / a
        k = np.searchsorted(days, sday[p]) - 1
        prev = days[k]
        rows.append(dict(
            pull_depth=depth,
            set_hour=srv.iloc[max(set_p, 0)].hour + srv.iloc[max(set_p, 0)].minute / 60,
            round10=abs(L - 10 * round(L / 10)),
            vs_dayopen=d * (L - dopen[sday[p]]) / a,
            prevday_dir=d * np.sign(dclose[prev] - dopen[prev]),
            beyond_pd=d * (L - (dhi[prev] if d > 0 else dlo[prev])) / a,
            srv_hour=srv.iloc[p].hour + srv.iloc[p].minute / 60,
        ))
    return pd.concat([ev.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def add_dominance(ev: pd.DataFrame) -> pd.DataFrame:
    cols = ["t", "side", "level", "prev_higher", "has_later"]
    sw = pd.read_parquet(OUT / "aero_swing_30min_1.parquet")[cols]
    sw = sw.rename(columns={"t": "t_sw", "level": "level_sw"})
    out = pd.merge_asof(ev.sort_values("t"), sw.sort_values("t_sw"), left_on="t", right_on="t_sw",
                        by="side", tolerance=pd.Timedelta("5min"), direction="nearest")
    near = (out.level - out.level_sw).abs() < 1.5
    for c in ("prev_higher", "has_later"):
        out[c] = np.where(near, out[c].astype(float), np.nan)
    return out.drop(columns=["t_sw", "level_sw"])


def logistic(X: np.ndarray, y: np.ndarray, lam: float = 1.0, iters: int = 400) -> np.ndarray:
    Xb = np.c_[np.ones(len(X)), X]
    w = np.zeros(Xb.shape[1])
    pos_w = (len(y) - y.sum()) / y.sum()                 # class balance
    sw = np.where(y, pos_w, 1.0)
    for _ in range(iters):                               # Newton steps
        p = 1 / (1 + np.exp(-Xb @ w))
        g = Xb.T @ (sw * (p - y)) + lam * np.r_[0, w[1:]]
        H = (Xb * (sw * p * (1 - p))[:, None]).T @ Xb + lam * np.diag(np.r_[0, np.ones(len(w) - 1)])
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


def tree(X: pd.DataFrame, y: np.ndarray, depth: int, min_leaf: int = 300, path: str = "") -> list:
    """Greedy Gini tree; returns leaf rules (path, n, take rate)."""
    rate = y.mean()
    if depth == 0 or len(y) < 2 * min_leaf:
        return [(path or "all", len(y), rate)]
    best = None
    for c in X.columns:
        x = X[c].to_numpy()
        for q in np.unique(np.nanquantile(x, np.linspace(0.05, 0.95, 19))):
            left = x <= q
            nl, nr = left.sum(), (~left).sum()
            if nl < min_leaf or nr < min_leaf:
                continue
            gl, gr = y[left].mean(), y[~left].mean()
            gini = nl * gl * (1 - gl) + nr * gr * (1 - gr)
            if best is None or gini < best[0]:
                best = (gini, c, q, left)
    if best is None:
        return [(path or "all", len(y), rate)]
    _, c, q, left = best
    pre = f"{path} & " if path else ""
    return (tree(X[left], y[left], depth - 1, min_leaf, f"{pre}{c}<={q:.2f}")
            + tree(X[~left], y[~left], depth - 1, min_leaf, f"{pre}{c}>{q:.2f}"))


def ea_episodes() -> pd.DataFrame:
    """The EA's own trades: open time (UTC), side, logged P&L, legs."""
    text = aas.LOG.read_bytes().decode("utf-16")
    text = text[text.index("from 2018.01.01"):]
    pos, cost, cur, eps = 0.0, 0.0, None, []
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 5 or p[3] != "Trades":
            continue
        m = re.match(r"(\d{4}\.\d\d\.\d\d \d\d:\d\d:\d\d)\s+deal #\d+ (buy|sell) ([\d.]+) "
                     r"XAUUSD at ([\d.]+)", p[4])
        if not m:
            continue
        ts, s, lot, px = m.groups()
        q, px = float(lot) * (1 if s == "buy" else -1), float(px)
        if abs(pos) < 1e-9:
            cur = dict(open=aas.server_to_utc(pd.Timestamp(ts.replace(".", "-", 2))),
                       side=1 if s == "buy" else -1, pnl=0.0, legs=0)
        if abs(pos) < 1e-9 or np.sign(q) == np.sign(pos):
            cost += q * px
            pos += q
            cur["legs"] += 1
        else:
            avg = cost / pos
            cur["pnl"] += -q * (px - avg) * 100
            cost -= -q * avg
            pos += q
            if abs(pos) < 1e-9:
                pos = cost = 0.0
                eps.append(cur)
    return pd.DataFrame(eps)


def main() -> None:
    ev = pd.read_parquet(OUT / "aero_approach_speed.parquet")
    ev = add_dominance(add_extra(ev))
    feats = BASE + EXTRA
    y = ev.ea.to_numpy()
    old = ev.level_age.to_numpy() >= 2
    print(f"universe {len(ev)} day-range breaks (EA free), EA orders matched {y.sum()}\n")

    rows = [dict(feature=f, auc_all=auc(ev[f].to_numpy(float), y),
                 auc_old=auc(ev.loc[old, f].to_numpy(float), y[old]),
                 med_ea=np.nanmedian(ev.loc[y, f]), med_skip=np.nanmedian(ev.loc[~y, f]))
            for f in feats]
    a = pd.DataFrame(rows)
    a["strength"] = (a.auc_all - 0.5).abs()
    print("1) which features separate EA picks (AUC; 0.5 = none):")
    print(a.sort_values("strength", ascending=False).drop(columns="strength").round(3).to_string(index=False))

    print("\n2) mimic fitted on 2018-22 choices, scored on 2023-26 choices")
    tr = (ev.t < SPLIT).to_numpy()
    X = ev[feats].astype(float)
    mu, sd = X[tr].mean(), X[tr].std() + 1e-9
    Z = ((X - mu) / sd).fillna(0).to_numpy()
    w = logistic(Z[tr], y[tr], lam=5.0)
    score = np.c_[np.ones(len(Z)), Z] @ w
    print(f"   logistic AUC  train {auc(score[tr], y[tr]):.3f}   test {auc(score[~tr], y[~tr]):.3f}")
    coef = pd.Series(w[1:], index=feats).sort_values(key=np.abs, ascending=False)
    print("   largest standardized coefficients:", coef.head(10).round(2).to_dict())
    leaves = tree(X[tr].fillna(X[tr].median()), y[tr], depth=3)
    print("   depth-3 tree (2018-22) leaves, sorted by take rate:")
    for path, n, r in sorted(leaves, key=lambda x: -x[2]):
        print(f"     take {r:.3f}  n={n:5d}  {path}")
    ev["mimic"] = score
    dec = pd.qcut(ev.loc[~tr, "mimic"], 10, labels=False)
    print("\n   2023-26 by mimic decile: EA take rate | event-sim net R (context only)")
    print(pd.DataFrame({"ea_take": ev.loc[~tr, "ea"].groupby(dec).mean(),
                        "sim_R": ev.loc[~tr, "net_r"].groupby(dec).mean()}).round(3).T.to_string())

    print("\n3) EA's own trades: winners vs losers (logged P&L)")
    eps = ea_episodes()
    picks = ev[ev.ea].sort_values("t")
    eps = pd.merge_asof(eps.sort_values("open"), picks, left_on="open", right_on="t", by="side",
                        tolerance=pd.Timedelta("10min"), direction="backward").dropna(subset=["t"])
    win = (eps.pnl > 0).to_numpy()
    print(f"   episodes matched to a break: {len(eps)} (win rate {win.mean():.3f})")
    r = pd.DataFrame([dict(feature=f, auc_win=auc(eps[f].to_numpy(float), win),
                           med_win=np.nanmedian(eps.loc[win, f]), med_loss=np.nanmedian(eps.loc[~win, f]))
                      for f in feats])
    r["strength"] = (r.auc_win - 0.5).abs()
    top = r.sort_values("strength", ascending=False).head(12).drop(columns="strength")
    print(top.round(3).to_string(index=False))
    ev.to_parquet(OUT / "aero_pattern_mining.parquet")


if __name__ == "__main__":
    main()
