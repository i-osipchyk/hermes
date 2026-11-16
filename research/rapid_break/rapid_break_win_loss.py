"""What separates winning from losing Aero-exit trades on H1 n=2 fractal breaks?

Trades: rapid_break_aero_1h_2_all.parquet, TP model atr1h (Aero entry/exits, corrected 3h rule).
Features: all known at the trigger minute (fill type is known 1-2 min later, before any exit).
Protocol: rank features on IS 2018-22 only -- AUC for win, mean net R by quintile, and per-year
sign stability. Features stable in >= 4 of 5 IS years (and |AUC-0.5| >= 0.02) are combined into a
z-score composite with IS signs and IS means/sds; the composite's top/bottom quintile is scored on
the holdout (2023-01..2026-08-05) ONCE with `--holdout`.

Run:  .venv/bin/python research/rapid_break/rapid_break_win_loss.py [--holdout]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import rapid_break_aero_exits as rae  # noqa: E402
import rapid_break_events as rbe  # noqa: E402

OUT = rbe.OUT


def auc(score: np.ndarray, y: np.ndarray) -> float:
    ok = ~np.isnan(score)
    s, y = score[ok], y[ok]
    r = pd.Series(s).rank().to_numpy()
    n1 = y.sum()
    n0 = len(y) - n1
    return (r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0) if n1 and n0 else np.nan


def features() -> pd.DataFrame:
    tr = pd.read_parquet(OUT / "rapid_break_aero_1h_2_all.parquet")
    tr = tr[tr.tp == "atr1h"].reset_index(drop=True)
    ev = pd.read_parquet(OUT / "rapid_break_1h_2.parquet")
    tr = tr.join(ev[["level", "age_h", "overshoot", "v5", "v15", "v60", "pday_ext", "psess_ext"]],
                 on="idx")
    m = rbe.load()
    ctx = rbe.context(m)
    c, h, lo = (m[k].to_numpy() for k in ("close", "high", "low"))
    t = tr.tq.to_numpy()
    d = tr.side.to_numpy()
    a = tr.atr.to_numpy()
    sday = m.srv.dt.normalize().to_numpy()
    first = pd.Series(np.arange(len(m))).groupby(sday).transform("min").to_numpy()
    dh, dl = ctx["day"]
    ph, pl = ctx["pday"]
    a1h = rae.atr_ltf(m, "1h")
    daily = m.close.resample("1D").last().dropna()
    dend = (daily.index + pd.Timedelta("1D")).tz_convert(None).to_numpy()
    ts = m.index.tz_convert(None).to_numpy()
    pos = np.searchsorted(dend, ts[t], side="right") - 1
    c20 = daily.shift(20).to_numpy()[pos]
    c5 = daily.shift(5).to_numpy()[pos]

    def back(k):
        return c[np.clip(t - 1 - k, 0, None)]

    f = pd.DataFrame(index=tr.index)
    f["day_ext"] = (tr.tier == "day").astype(float)
    f["sess_ext"] = (tr.tier == "session").astype(float)
    f["pday_ext"] = tr.pday_ext.astype(float)
    f["psess_ext"] = tr.psess_ext.astype(float)
    f["long"] = (d > 0).astype(float)
    f["ny"] = tr.ny.astype(float)
    f["hour"] = tr.t.dt.hour
    f["weekday"] = tr.t.dt.weekday
    f["limit_fill"] = tr.limit.astype(float)
    f["age_h"] = np.log1p(tr.age_h)
    f["overshoot"] = tr.overshoot
    f["v5"], f["v15"], f["v60"] = tr.v5, tr.v15, tr.v60
    f["ret4h"] = d * (back(0) - back(240)) / a
    f["ret5d"] = d * (back(0) - c5) / a
    f["ret20d"] = d * (back(0) - c20) / a
    f["day_range"] = (dh[t] - dl[t]) / a
    f["from_day_open"] = d * (back(0) - m.open.to_numpy()[first[t]]) / a
    f["room_pday"] = d * (np.where(d > 0, ph[t], pl[t]) - tr.level) / a
    f["vol_ratio"] = a1h[t] / a                       # short-term vs 4h volatility (= TP / ATR4h)
    f["atr_level"] = a / tr.level * 1e3              # 4h ATR as % of price (vol regime)
    rng60 = np.array([h[max(i - 60, 0):i].max() - lo[max(i - 60, 0):i].min() for i in t])
    f["range60"] = rng60 / a
    # how far the market pulled back from the level after the swing formed (structure depth)
    f["press30"] = np.array([np.mean(np.abs(c[max(i - 30, 0):i] - lv) < 0.1 * aa)
                             for i, lv, aa in zip(t, tr.level, a, strict=True)])
    return tr, f


def main() -> None:
    tr, f = features()
    y = (tr.usd > 0).to_numpy()
    is_ = (tr.period == "IS").to_numpy()
    yrs = tr.t.dt.year.to_numpy()
    rows = []
    for col in f.columns:
        x = f[col].to_numpy(float)
        a_is = auc(x[is_], y[is_])
        by_year = [auc(x[is_ & (yrs == yy)], y[is_ & (yrs == yy)]) for yy in range(2018, 2023)]
        sign = np.sign(a_is - 0.5)
        stable = int(sum(np.sign(b - 0.5) == sign for b in by_year))
        r_is = pd.Series(tr.r[is_].to_numpy()).groupby(
            pd.qcut(pd.Series(x[is_]).rank(method="first"), 5, labels=False)).mean().to_numpy()
        rows.append(dict(feature=col, auc_IS=a_is, years_same_sign=stable,
                         **{f"R_q{i}": v for i, v in enumerate(r_is)}))
    tab = pd.DataFrame(rows).set_index("feature")
    tab["strength"] = (tab.auc_IS - 0.5).abs()
    tab = tab.sort_values("strength", ascending=False)
    print(f"IS trades {is_.sum()}, win {100 * y[is_].mean():.1f}%, mean R {tr.r[is_].mean():+.4f}")
    print("\nPer-feature, IS 2018-22 (AUC for win; R by feature quintile low->high):")
    print(tab.round(3).to_string())

    keep = tab[(tab.years_same_sign >= 4) & (tab.strength >= 0.02)]
    print(f"\nComposite features (stable >= 4/5 IS years, |AUC-0.5| >= 0.02): {list(keep.index)}")
    z = np.zeros(len(f))
    for col, row in keep.iterrows():
        x = f[col].astype(float)
        mu, sd = x[is_].mean(), x[is_].std()
        z += np.sign(row.auc_IS - 0.5) * ((x - mu) / sd).fillna(0).to_numpy()
    tr["score"] = z
    edges = np.quantile(z[is_], [0.2, 0.4, 0.6, 0.8])
    tr["sq"] = np.digitize(z, edges)
    periods = ["IS"] + (["HOLD"] if "--holdout" in sys.argv else [])
    for p in periods:
        x = tr[tr.period == p]
        print(f"\n{p}: composite AUC {auc(x.score.to_numpy(), (x.usd > 0).to_numpy()):.3f}")
        g = x.groupby("sq").agg(n=("r", "size"), win=("usd", lambda u: 100 * (u > 0).mean()),
                                R=("r", "mean"), ctrl_R=("ctrl_r", "mean"),
                                usd=("usd", "mean"))
        g["excess"] = g.R - g.ctrl_R
        print(g.round(4).to_string())
        top = x[x.sq == 4]
        print("  top quintile by year R:", top.groupby(top.t.dt.year).r.mean().round(3).to_dict())


if __name__ == "__main__":
    main()
