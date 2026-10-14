"""Walk-forward kNN over gold day-high/low breaks with the findings' features, traded with the
Aero EA's entry model, and compared with the EA's own trades.

Universe  current-day high/low breaks (aero_new_features.parquet), 2018-01..2026-08-06, each
          with its outcome under the EA's mechanics (1m sim, net $/oz).  Variants: all breaks, or
          only levels held >= 2h.
Features  level   log level age, pullback depth, touches
          arrival a1, a3 (1m), press30, gap_now (5m), m30_4 (2h extension)
          timing  hour sin/cos
          context r20d, r26w, previous day's candle
          new     room_min, room_pdpw (log), lock_atr, lock_path60, release 0-30 min, 30 min-8 h
          compact set: level age, pullback, a1, press30, room_min, lock_atr, lock_path60, release
Model     monthly expanding walk-forward from 2019-01; training rows are breaks whose outcome had
          resolved (exit) before the month began; z-scores from training rows only; score =
          distance-weighted mean net $/oz of the k nearest; trade when score > thr.
Selection universe x feature set x k x thr chosen on 2019-22 by the traded mean $/oz (>= 100
          trades); 2023-01..2026-08 scored once.
Trading   EA entry model from the sim (limit at the level, chase after ~1.5 min), abandoned when
          the chase is > $2.90 beyond the level, one position at a time, <= 2 trades per broker day.
Compared  the EA's logged trades over the same window: P&L per trade, overlap, correlation of daily
          and monthly P&L (1 oz per trade on both sides).

Run:  .venv/bin/python research/aero/aero_knn_v2.py   (after aero_new_features.py)
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.argv = [sys.argv[0], "5m"]
import aero_approach_speed as aas  # noqa: E402
import aero_daybreak_sim as ads  # noqa: E402
import aero_m1_microstructure as amm  # noqa: E402
import aero_pattern_mining as apm  # noqa: E402

OUT = ads.OUT
WF_START = pd.Timestamp("2019-01-01", tz="UTC")
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
OOS_END = pd.Timestamp("2026-08-06", tz="UTC")
FULL = ["lage", "pull_depth", "touches", "a1", "a3", "press30", "gap_now", "m30_4", "hour_sin",
        "hour_cos", "r20d", "r26w", "prevday_dir", "lroom_min", "lroom_pdpw", "lock_atr",
        "lock_path60", "rel_recent", "rel_later"]
COMPACT = ["lage", "pull_depth", "a1", "press30", "lroom_min", "lock_atr", "lock_path60", "rel_recent"]
KS, THRS = (25, 50, 100, 200), (0.0, 0.5, 1.0)
MAX_CHASE = 2.90


def build() -> pd.DataFrame:
    cache = OUT / "aero_knn_v2_features.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    ev = pd.read_parquet(OUT / "aero_new_features.parquet")
    five = pd.read_parquet(OUT / "aero_knn_events_5m.parquet")[
        ["t", "side", "level", "r20d", "r26w", "touches", "hour_sin", "hour_cos"]]
    ev = ev.merge(five, on=["t", "side", "level"], how="left")
    ev = aas.add_features(ev)
    ev = apm.add_extra(ev)
    ev = amm.m1_features(ev)
    ev["lage"] = np.log1p(ev.level_age)
    ev["lroom_min"] = np.log1p(ev.room_min.clip(upper=100))
    ev["lroom_pdpw"] = np.log1p(ev.room_pdpw.clip(upper=100))
    ev["rel_recent"] = (ev.since_release <= 30).astype(float)
    ev["rel_later"] = ((ev.since_release > 30) & (ev.since_release <= 480)).astype(float)
    ev = ev.loc[:, ~ev.columns.duplicated()]
    ev.to_parquet(cache)
    return ev


def walk_forward(ev: pd.DataFrame, feats: list[str]) -> dict[int, np.ndarray]:
    X = ev[feats].to_numpy(float)
    y = ev.usd.to_numpy(float)
    t = ev.t.dt.tz_convert(None).to_numpy()
    ex = ev.exit_time.dt.tz_convert(None).to_numpy()
    out = {k: np.full(len(ev), np.nan) for k in KS}
    months = pd.date_range(WF_START, OOS_END + pd.offsets.MonthBegin(), freq="MS", tz="UTC")
    for a, b in zip(months[:-1], months[1:]):
        a64, b64 = a.tz_convert(None).to_datetime64(), b.tz_convert(None).to_datetime64()
        test = np.flatnonzero((t >= a64) & (t < b64))
        train = np.flatnonzero(ex < a64)
        if len(test) == 0 or len(train) < 300:
            continue
        mu, sd = np.nanmean(X[train], 0), np.nanstd(X[train], 0) + 1e-9
        Xtr, Xte = np.nan_to_num((X[train] - mu) / sd), np.nan_to_num((X[test] - mu) / sd)
        d2 = (Xte ** 2).sum(1)[:, None] + (Xtr ** 2).sum(1)[None, :] - 2 * Xte @ Xtr.T
        dist = np.sqrt(np.maximum(d2, 0))
        order = np.argsort(dist, axis=1)
        for k in KS:
            nn = order[:, :k]
            w = 1 / (np.take_along_axis(dist, nn, 1) + 1e-6)
            out[k][test] = (w * y[train][nn]).sum(1) / w.sum(1)
    return out


def server_day(t: pd.Series) -> pd.Series:
    return (t.dt.tz_convert("America/New_York").dt.tz_localize(None) + pd.Timedelta(hours=7)).dt.normalize()


def trade(ev: pd.DataFrame) -> pd.DataFrame:
    """EA pacing over candidate breaks (already scored/selected): flat only, <= 2 per broker day,
    chase abandoned beyond MAX_CHASE."""
    x = ev[~((ev.kind == "chase") & (ev.side * (ev.fill - ev.level) > MAX_CHASE))].sort_values("t")
    srv_day = server_day(x.t)
    keep, free, per_day = [], pd.Timestamp.min.tz_localize("UTC"), {}
    for (i, r), sd in zip(x.iterrows(), srv_day):
        if r.t < free or per_day.get(sd, 0) >= 2:
            continue
        keep.append(i)
        free = r.exit_time
        per_day[sd] = per_day.get(sd, 0) + 1
    return x.loc[keep]


def stats(usd: pd.Series) -> dict:
    pos, neg = usd[usd > 0].sum(), -usd[usd < 0].sum()
    return dict(trades=len(usd), win=round(100 * (usd > 0).mean(), 1), usd_oz=round(usd.mean(), 2),
                total_oz=round(usd.sum(), 1), pf=round(pos / neg, 2) if neg else np.inf)


def main() -> None:
    ev = build()
    ev = ev[ev.t < OOS_END].reset_index(drop=True)
    held = (ev.level_age >= 2).to_numpy()
    scores = {}
    for uni, mask in (("all", np.ones(len(ev), bool)), ("held", held)):
        sub = ev[mask].reset_index()
        for fname, feats in (("full", FULL), ("compact", COMPACT)):
            sc = walk_forward(sub, feats)
            for k in KS:
                full = np.full(len(ev), np.nan)
                full[sub["index"].to_numpy()] = sc[k]
                scores[(uni, fname, k)] = full
    is_ = ((ev.t >= WF_START) & (ev.t < IS_END)).to_numpy()
    oos = (ev.t >= IS_END).to_numpy()
    grid = []
    for (uni, fname, k), thr in itertools.product(scores, THRS):
        sel = np.nan_to_num(scores[(uni, fname, k)], nan=-1e9) > thr
        tr = trade(ev[sel & is_])
        grid.append(dict(universe=uni, features=fname, k=k, thr=thr, **stats(tr.usd)))
    g = pd.DataFrame(grid)
    print("IN-SAMPLE 2019-22 (walk-forward predictions, EA entry model + pacing):")
    print(g.sort_values("usd_oz", ascending=False).to_string(index=False))
    best = g[g.trades >= 100].sort_values("usd_oz", ascending=False).iloc[0]
    key = (best.universe, best.features, int(best.k))
    print(f"\nSELECTED on 2019-22: {key}, thr={best.thr}")

    sel = np.nan_to_num(scores[key], nan=-1e9) > best.thr
    ours = trade(ev[sel & oos])
    base_all = trade(ev[oos])
    base_held = trade(ev[oos & held])
    eps = ads.ea_trades()
    ea = eps[(eps.open >= IS_END) & (eps.open < OOS_END)]
    print("\nOUT OF SAMPLE 2023-01..2026-08 (scored once):")
    tab = pd.DataFrame({
        "kNN strategy": stats(ours.usd),
        "all breaks, EA pacing": stats(base_all.usd),
        "held>=2h breaks, EA pacing": stats(base_held.usd),
        "EA actual (log)": stats(ea.usd_oz),
    }).T
    print(tab.to_string())
    rng = np.random.default_rng(0)
    pool = ev[oos & held] if best.universe == "held" else ev[oos]
    rand = [trade(pool.sample(frac=min(1.0, sel[oos].mean() * len(ev[oos]) / len(pool)),
                              random_state=int(s))).usd.mean() for s in rng.integers(0, 1e6, 200)]
    print(f"random selection, same rate, same pacing: mean $/oz {np.mean(rand):+.2f} "
          f"(P(random >= kNN) = {np.mean(np.array(rand) >= ours.usd.mean()):.3f})")
    print("\nby year ($/oz per trade, trades):")
    yr = pd.DataFrame({"kNN": ours.groupby(ours.t.dt.year).usd.mean(),
                       "kNN n": ours.groupby(ours.t.dt.year).size(),
                       "EA": ea.groupby(ea.open.dt.year).usd_oz.mean(),
                       "EA n": ea.groupby(ea.open.dt.year).size()})
    print(yr.round(2).to_string())

    # overlap with the EA's choices
    sel_oos = ev[sel & oos]
    ea_oos = ev[oos & ev.ea.to_numpy()]
    print(f"\noverlap: kNN-selected breaks the EA also ordered {sel_oos.ea.mean() * 100:.1f}% "
          f"(base rate {ev[oos].ea.mean() * 100:.1f}%); EA-ordered day breaks the kNN selected "
          f"{100 * sel[oos & ev.ea.to_numpy()].mean():.1f}%; kNN traded trades that EA ordered "
          f"{100 * ours.ea.mean():.1f}%")
    scored = oos & ~np.isnan(scores[key])
    pct = pd.Series(scores[key][scored]).rank(pct=True).to_numpy()[ev.ea.to_numpy()[scored]].mean()
    print(f"kNN score of EA-ordered breaks it scored: percentile {100 * pct:.1f} (50 = unrelated); "
          f"EA-ordered day breaks OOS {len(ea_oos)}, of them in the kNN universe "
          f"{int(ev.ea.to_numpy()[scored].sum())}")
    d_ours = ours.groupby(server_day(ours.t)).usd.sum()
    d_ea = ea.groupby(server_day(ea.open)).usd_oz.sum()
    idx = pd.date_range(min(d_ours.index.min(), d_ea.index.min()),
                        max(d_ours.index.max(), d_ea.index.max()))
    D = pd.DataFrame({"ours": d_ours.reindex(idx, fill_value=0), "ea": d_ea.reindex(idx, fill_value=0)})
    D = D[(D.ours != 0) | (D.ea != 0)]
    M = D.groupby(D.index.to_period("M")).sum()
    print(f"P&L correlation with the EA: daily {D.ours.corr(D.ea):.3f} (active days {len(D)}), "
          f"monthly {M.ours.corr(M.ea):.3f} ({len(M)} months)")
    ours.to_parquet(OUT / "aero_knn_v2_trades.parquet")


if __name__ == "__main__":
    main()
