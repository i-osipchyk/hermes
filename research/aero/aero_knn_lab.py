"""kNN lab: different ways to run a kNN over gold day-high/low breaks.

Dataset (cached per symbol): every 1m bar trading `trig` beyond the running broker-day high/low,
02:30-23:00 server, at most one event per side per 5 minutes.  For each:
  outcome  R under the EA's 2026 gold trade geometry in ATR(4h,20) units (aero_silver.simulate):
           stationary across years, unlike the fixed-dollar outcome used in aero_knn_v2
  lock     whether the +0.32 ATR lock was reached (the EA's real "win")
  ea       the EA placed an order on this break (tester logs)
  hand     level age, pullback depth, touches, 1m arrival (a1, a3), press30, hour, 20-day trend,
           room to previous day/week extreme, recent-path vs ATR
  raw      price shape before the trigger, (close - level) * side / ATR:
           w1  last 30 closed 1m closes      w5  last 24 closed 5m closes
           wh  last 24 closed H1 closes      wd  last 20 closed D1 closes (server days)
Walk-forward: monthly, expanding, training rows resolved before the month; z-scores per column
from training rows; distance-weighted mean target of the k nearest.
Selection: configs compared on gold 2019-22 walk-forward predictions only (top-decile mean R).
Checks: gold 2023-26 (seen before -> weak), silver 2026 scored by a gold-trained model (clean).

Run:  .venv/bin/python research/aero/aero_knn_lab.py
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
import aero_silver as asv  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = akf.OUT
BARS = ROOT / ".hermes_cache/bars/pepperstone"
WF_START = pd.Timestamp("2019-01-01", tz="UTC")
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
HAND = ["lage", "pull", "touch", "a1", "a3", "press30", "hsin", "hcos", "r20d", "room_pd", "room_pw",
        "path_atr"]
BLOCKS = {"w1": 30, "w5": 24, "wh": 24, "wd": 20}
SYMS = {"XAUUSD": dict(trig=0.50, start="2017-12-01", end="2026-08-06", log="20261009 copy.log",
                       marker="XAUUSD,H1: testing of", tol=1.5),
        "XAGUSD": dict(trig=0.05, start="2025-11-03", end="2026-08-06", log="20261009 copy 2.log",
                       marker="XAGUSD,H1: testing of", tol=0.15)}


def load(sym: str, start: str, end: str) -> pd.DataFrame:
    m = pd.read_parquet(BARS / f"{sym}_1m.parquet")
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    lo_t, hi_t = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=12)
    m = m[(m.index >= lo_t) & (m.index < hi_t)]
    srv = m.index.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    m["sday"], m["smin"] = srv.normalize(), srv.hour * 60 + srv.minute
    h4 = m.resample("4h").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    s = pd.Series(akf.wilder(akf.true_range(h4), 20).values, index=h4.index + pd.Timedelta("4h"))
    m["atr"] = s.reindex(m.index, method="ffill").to_numpy()
    return m


def closed_closes(m: pd.DataFrame, rule: str) -> tuple[np.ndarray, np.ndarray]:
    if rule == "D":
        b = m.groupby("sday").close.last()
        ends = (b.index + pd.Timedelta(days=1) - pd.Timedelta(hours=7)).tz_localize("America/New_York")
        return ends.tz_convert("UTC").tz_convert(None).to_numpy(), b.to_numpy()
    b = m.close.resample(rule).last().dropna()
    return (b.index + pd.Timedelta(rule)).tz_convert(None).to_numpy(), b.to_numpy()


def build(sym: str) -> pd.DataFrame:
    cache = OUT / f"aero_lab_{sym}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    cfg = SYMS[sym]
    m = load(sym, cfg["start"], cfg["end"])
    asv.START = pd.Timestamp(cfg["start"], tz="UTC") + pd.Timedelta(days=35)
    asv.END = pd.Timestamp(cfg["end"], tz="UTC")
    ev = asv.events(m, cfg["trig"])
    ev["b5"] = ev.t.dt.floor("5min")
    ev = ev.drop_duplicates(["side", "b5"]).reset_index(drop=True)
    ev = asv.simulate(m, ev, cfg["trig"]).dropna(subset=["r"]).reset_index(drop=True)
    h, lo, c = m.high.to_numpy(), m.low.to_numpy(), m.close.to_numpy()
    atr = m.atr.to_numpy()
    ts = m.index.tz_convert(None).to_numpy()
    sday = m.sday.to_numpy()
    dd = m.groupby("sday").agg(h=("high", "max"), l=("low", "min"))
    wk = m.groupby(m.sday.dt.to_period("W-SAT")).agg(h=("high", "max"), l=("low", "min"))
    ends = {k: closed_closes(m, r) for k, r in (("w5", "5min"), ("wh", "1h"), ("wd", "D"))}
    d1_end, d1_close = ends["wd"]
    rows, raws = [], []
    for e in ev.itertuples():
        j, d, L, a = e.i, e.side, e.level, atr[e.i]
        set_j = int(j - round(e.age_h * 60))
        seg_lo, seg_hi = lo[set_j:j], h[set_j:j]
        dist = d * (L - c[j - 61:j]) / a
        k20 = np.searchsorted(d1_end, ts[j], side="right") - 1
        prev = dd.index[dd.index < sday[j]]
        pw = wk.index[wk.index < pd.Timestamp(sday[j]).to_period("W-SAT")]
        pdx = (dd.h[prev[-1]] if d > 0 else dd.l[prev[-1]]) if len(prev) else np.nan
        pwx = (wk.h[pw[-1]] if d > 0 else wk.l[pw[-1]]) if len(pw) else np.nan
        near = d * (L - (h[j - 30:j] if d > 0 else lo[j - 30:j])) / a
        rows.append(dict(
            lage=np.log1p(e.age_h),
            pull=((L - seg_lo.min()) if d > 0 else (seg_hi.max() - L)) / a if len(seg_lo) else 0.0,
            touch=int(((seg_hi >= L - 0.1 * a) if d > 0 else (seg_lo <= L + 0.1 * a))[1:].sum()),
            a1=dist[-1], a3=dist[-3], press30=int((near <= 0.10).sum()),
            hsin=np.sin(2 * np.pi * m.smin.iat[j] / 1440), hcos=np.cos(2 * np.pi * m.smin.iat[j] / 1440),
            r20d=d * (c[j - 1] - d1_close[max(k20 - 19, 0)]) / a if k20 >= 0 else np.nan,
            room_pd=np.clip(d * (pdx - L) / a, -5, 5), room_pw=np.clip(d * (pwx - L) / a, -5, 5),
            path_atr=np.abs(np.diff(c[j - 61:j])).sum() / a))
        rv = [d * (c[j - BLOCKS["w1"]:j] - L) / a]
        for k in ("w5", "wh", "wd"):
            e_, cl = ends[k]
            p = np.searchsorted(e_, ts[j], side="right")
            win = cl[max(p - BLOCKS[k], 0):p]
            win = np.r_[np.full(BLOCKS[k] - len(win), win[0] if len(win) else c[j - 1]), win]
            rv.append(d * (win - L) / a)
        raws.append(np.concatenate(rv))
    cols = [f"{b}_{i}" for b, n in BLOCKS.items() for i in range(n)]
    ds = pd.concat([ev, pd.DataFrame(rows), pd.DataFrame(np.vstack(raws), columns=cols)], axis=1)
    ds["exit_time"] = ds.t + pd.Timedelta(days=3)       # treat as resolved only 3 days later
    ds = label(ds, sym, cfg)
    ds.to_parquet(cache)
    return ds


def label(ds: pd.DataFrame, sym: str, cfg: dict) -> pd.DataFrame:
    pat = re.compile(rf"(\d{{4}}\.\d\d\.\d\d \d\d:\d\d:\d\d)\s+(buy|sell) limit [\d.]+ "
                     rf"{sym} at ([\d.]+) sl:")
    rows, on = [], False
    with open(aas.LOGS / cfg["log"], encoding="utf-16") as fh:
        for line in fh:
            if cfg["marker"] in line:
                on, rows = True, []
                continue
            if on:
                p = line.rstrip("\n").split("\t")
                mm = pat.match(p[4]) if len(p) >= 5 and p[3] == "Trade" else None
                if mm:
                    rows.append((aas.server_to_utc(pd.Timestamp(mm.group(1).replace(".", "-", 2))),
                                 1 if mm.group(2) == "buy" else -1, float(mm.group(3))))
    ds["ea"] = False
    tv = ds.t.dt.tz_convert(None).to_numpy()
    for t, side, px in rows:
        dt = np.abs(tv - t.tz_convert(None).to_datetime64())
        near = np.flatnonzero((dt <= np.timedelta64(3, "m")) & (ds.side.to_numpy() == side))
        if len(near):
            dist = np.abs(ds.level.to_numpy()[near] - px)
            if dist.min() < cfg["tol"]:
                ds.loc[ds.index[near[dist.argmin()]], "ea"] = True
    return ds


def knn_scores(train: pd.DataFrame, test: pd.DataFrame, cols: list[str], target: str, k: int,
               weights: np.ndarray | None = None, min_train: int = 1000) -> np.ndarray:
    """Distance-weighted mean target of the k nearest training rows resolved before each test month
    (or before each test row's month when the test set is another symbol)."""
    X, y = train[cols].to_numpy(float), train[target].to_numpy(float)
    rt = train.exit_time.dt.tz_convert(None).to_numpy()
    T = test[cols].to_numpy(float)
    out = np.full(len(test), np.nan)
    months = pd.DatetimeIndex(test.t.dt.tz_convert(None)).to_period("M")
    for mo in months.unique():
        idx = np.flatnonzero(months == mo)
        cut = mo.start_time.to_datetime64()
        tr = np.flatnonzero(rt < cut)
        if len(tr) < min_train:
            continue
        mu, sd = np.nanmean(X[tr], 0), np.nanstd(X[tr], 0) + 1e-9
        if weights is not None:
            sd = sd / weights
        Xt = np.nan_to_num((X[tr] - mu) / sd)
        for chunk in np.array_split(idx, max(1, len(idx) // 400)):
            Q = np.nan_to_num((T[chunk] - mu) / sd)
            d2 = (Q ** 2).sum(1)[:, None] + (Xt ** 2).sum(1)[None, :] - 2 * Q @ Xt.T
            nn = np.argpartition(d2, k, axis=1)[:, :k]
            dist = np.sqrt(np.maximum(np.take_along_axis(d2, nn, 1), 0))
            w = 1 / (dist + 1e-6)
            out[chunk] = (w * y[tr][nn]).sum(1) / w.sum(1)
    return out


def evaluate(df: pd.DataFrame, score: np.ndarray, thr: float) -> dict:
    ok = ~np.isnan(score)
    d, s = df[ok], score[ok]
    top = s >= thr
    dec = pd.qcut(s, 5, labels=False, duplicates="drop")
    q = d.r.groupby(dec).mean()
    rho = pd.Series(q.values).corr(pd.Series(range(len(q))), method="spearman") if len(q) > 2 else np.nan
    return dict(n=int(ok.sum()), top_n=int(top.sum()), top_R=d.r[top].mean(), all_R=d.r.mean(),
                lift=d.r[top].mean() - d.r.mean(), q_rho=rho,
                ea_auc=akf_auc(s, d.ea.to_numpy()) if d.ea.any() else np.nan)


def akf_auc(x: np.ndarray, y: np.ndarray) -> float:
    r = pd.Series(x).rank().to_numpy()
    n1, n0 = y.sum(), len(y) - y.sum()
    return float((r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def main() -> None:
    gold, silver = build("XAUUSD"), build("XAGUSD")
    print(f"gold breaks {len(gold)} (EA {int(gold.ea.sum())}), "
          f"silver breaks {len(silver)} (EA {int(silver.ea.sum())})")
    raw = {b: [f"{b}_{i}" for i in range(n)] for b, n in BLOCKS.items()}
    sets = {
        "E1 hand": HAND,
        "E2 raw all": sum(raw.values(), []),
        "E3 raw 1m": raw["w1"], "E3 raw 5m": raw["w5"], "E3 raw H1": raw["wh"], "E3 raw D1": raw["wd"],
        "E6 hand+raw": HAND + sum(raw.values(), []),
    }
    gold["lock"] = gold.locked.astype(float)
    silver["lock"] = silver.locked.astype(float)
    gold["ea_f"] = gold.ea.astype(float)
    wf = gold[gold.t >= WF_START].reset_index(drop=True)
    is_ = (wf.t < IS_END).to_numpy()
    rows = []
    for name, cols in sets.items():
        for tgt in ("r", "lock", "ea_f"):
            if tgt != "r" and name not in ("E1 hand", "E2 raw all", "E6 hand+raw"):
                continue
            for k in (50, 200):
                s = knn_scores(gold, wf, cols, tgt, k)
                thr = np.nanquantile(s[is_], 0.9)
                ins, oos = evaluate(wf[is_], s[is_], thr), evaluate(wf[~is_], s[~is_], thr)
                sv = knn_scores(gold, silver, cols, tgt, k)
                sil = evaluate(silver, sv, thr)
                rows.append(dict(set=name, target=tgt, k=k,
                                 IS_lift=ins["lift"], IS_rho=ins["q_rho"],
                                 OOS_lift=oos["lift"], OOS_rho=oos["q_rho"], OOS_eaAUC=oos["ea_auc"],
                                 SIL_lift=sil["lift"], SIL_top_n=sil["top_n"], SIL_eaAUC=sil["ea_auc"]))
                print(rows[-1], flush=True)
    res = pd.DataFrame(rows)
    res.to_csv(OUT / "aero_knn_lab.csv", index=False)
    print("\nsorted by in-sample lift (the only legitimate selection criterion):")
    print(res.sort_values("IS_lift", ascending=False).round(3).to_string(index=False))


if __name__ == "__main__":
    main()
