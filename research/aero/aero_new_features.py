"""Five untested features for a kNN over gold day-high/low breaks.

Data: research/output/aero_daybreak_sim.parquet (27,603 current-day high/low breaks, 1m-simulated
with the EA's mechanics, EA labels).  Outcomes are judged on 2018-2022 ONLY; 2023-26 is kept
untouched for a later one-shot test.  EA-choice AUCs use the whole period (labels, not outcomes).

  1 room       distance to the next obstacle beyond the level, in $: nearest unbroken H1 swing
               (n=1, <= 60 days old), previous day / previous week extreme; separately the next
               round $50.  Capped at $100 when there is none.
  2 lock_vol   the EA's $12 lock as a multiple of the last hour's 1m path (sum of |close changes|),
               and as a multiple of ATR(4h,20)
  3 confluence other level types within $1 of the level: H1 swing, H4 swing, previous-day extreme,
               round $10
  4 news       proxy only (no calendar locally): minutes since 08:30 / 10:00 / 14:00 New York and
               whether that minute printed a release-sized 1m bar today (range >= 3x the median of
               the hour before)
  5 post       after the trigger (usable only for the chase decision): did price retest the level
               within ~1.5 min (limit filled) and, if not, how far the chase entry was beyond it

Run:  .venv/bin/python research/aero/aero_new_features.py   (after aero_daybreak_sim.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.argv = [sys.argv[0], "5m"]
import aero_daybreak_sim as ads  # noqa: E402
import aero_pattern_mining as apm  # noqa: E402
import aero_swing_dominance as asd  # noqa: E402

OUT = ads.OUT
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
LOCK = 12.0


class RangeMax:
    """Sparse table for O(1) range max."""

    def __init__(self, x: np.ndarray) -> None:
        self.t = [x]
        k = 1
        while 2 * k <= len(x):
            p = self.t[-1]
            self.t.append(np.maximum(p[:-k], p[k:]))
            k *= 2

    def q(self, a: int, b: int) -> float:          # max of x[a:b], b > a
        a, b = int(a), int(b)
        j = (b - a).bit_length() - 1
        return max(self.t[j][a], self.t[j][b - (1 << j)])


def unbroken(table: tuple, d: int, t: np.datetime64, j: int, before: int,
             hmax: RangeMax, lmin: RangeMax) -> np.ndarray:
    """Swings of side d confirmed within 60 days before t and before 1m index `before`,
    never traded through up to minute j."""
    conf, px, idx = table
    lo_i = np.searchsorted(conf, t - np.timedelta64(60, "D"))
    hi_i = np.searchsorted(conf, t, side="right")
    px_w, idx_w = px[lo_i:hi_i], idx[lo_i:hi_i]
    keep = idx_w < before
    px_w, idx_w = px_w[keep], idx_w[keep]
    ok = np.array([(hmax.q(i0, j) < v) if d > 0 else (-lmin.q(i0, j) > v)
                   for v, i0 in zip(px_w, idx_w)], bool)
    return px_w[ok] if len(px_w) else px_w


def swing_table(m1: pd.DataFrame, rule: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    b = m1.resample(rule).agg({"high": "max", "low": "min"}).dropna()
    s = pd.DataFrame(asd.swings(b, 1), columns=["conf", "kind", "px"])
    return b, s


def add_features(ev: pd.DataFrame, m1: pd.DataFrame) -> pd.DataFrame:
    ts1 = m1.index.tz_convert(None).to_numpy()
    o, h, lo, c = (m1[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    hmax, lmin = RangeMax(h), RangeMax(-lo)
    srv = m1.index.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    sday = srv.normalize()
    dd = pd.DataFrame({"h": h, "l": lo, "d": sday}).groupby("d").agg(h=("h", "max"), l=("l", "min"))
    wk = (pd.DataFrame({"h": h, "l": lo, "w": srv.to_period("W-SAT")})
          .groupby("w").agg(h=("h", "max"), l=("l", "min")))
    et = m1.index.tz_convert("America/New_York")
    et_min = (et.hour * 60 + et.minute).to_numpy()
    et_day = et.normalize().tz_localize(None).to_numpy()
    rng = h - lo
    sw = {}
    for name, rule in (("h1", "1h"), ("h4", "4h")):
        _, tab = swing_table(m1, rule)
        for kind in (1, -1):
            x = tab[tab.kind == kind].sort_values("conf")
            sw[(name, kind)] = (x.conf.to_numpy(), x.px.to_numpy(),
                                np.searchsorted(ts1, x.conf.to_numpy()))
    day_start = pd.Series(np.arange(len(sday)), index=sday).groupby(level=0).first()
    rows = []
    pos = np.searchsorted(ts1, ev.t.dt.tz_convert(None).to_numpy())
    for (_, e), p in zip(ev.iterrows(), pos):
        d, L, a = e.side, e.level, e.atr
        trig = L + d * 0.5
        j = next((k for k in range(p, min(p + 5, len(m1)))
                  if ((h[k] >= trig) if d > 0 else (lo[k] <= trig))), p)
        t = ts1[j]
        d0 = day_start[sday[j]]
        r = {}

        # 1 room to the next obstacle
        h1_live = unbroken(sw[("h1", d)], d, t, j, j, hmax, lmin)
        ahead = h1_live[(h1_live > trig) if d > 0 else (h1_live < trig)]
        r["room_swing"] = float(np.abs(ahead - L).min()) if len(ahead) else 100.0
        sd = sday[j]
        prev = dd.index[dd.index < sd]
        pw = wk.index[wk.index < srv[j].to_period("W-SAT")]
        ext = []
        if len(prev):
            ext.append(dd.h[prev[-1]] if d > 0 else dd.l[prev[-1]])
        if len(pw):
            ext.append(wk.h[pw[-1]] if d > 0 else wk.l[pw[-1]])
        beyond = [abs(x - L) for x in ext if (x > trig if d > 0 else x < trig)]
        r["room_pdpw"] = min(beyond) if beyond else 100.0
        r["room_min"] = min(r["room_swing"], r["room_pdpw"])
        nxt50 = (np.floor(trig / 50) + 1) * 50 if d > 0 else (np.ceil(trig / 50) - 1) * 50
        r["room_r50"] = abs(nxt50 - L)
        # 2 the $12 lock against recent volatility
        path = np.abs(np.diff(c[max(j - 61, 0):j])).sum()
        r["lock_path60"] = LOCK / path if path > 0 else np.nan
        r["lock_atr"] = LOCK / a
        # 3 confluence: OTHER levels within $1 (swings confirmed before today began)
        conf_n = 0
        for name in ("h1", "h4"):
            live = unbroken(sw[(name, d)], d, t, j, d0, hmax, lmin)
            conf_n += int(np.any(np.abs(live - L) <= 1.0))
        conf_n += int(any(abs(x - L) <= 1.0 for x in ext))
        conf_n += int(abs(L - 10 * round(L / 10)) <= 1.0)
        r["confluence"] = conf_n
        # 4 scheduled-release proxy
        today = et_day[j]
        best_since, spike_any = 9999.0, 0
        for hhmm in (8 * 60 + 30, 10 * 60, 14 * 60):
            if et_min[j] <= hhmm:
                continue
            k0 = j - (et_min[j] - hhmm)
            if k0 < 61 or et_day[k0] != today or et_min[k0] != hhmm:
                continue
            spike = rng[k0] >= 3 * np.median(rng[k0 - 60:k0])
            if spike:
                spike_any = 1
                best_since = min(best_since, et_min[j] - hhmm)
        r["since_release"] = best_since
        r["release_day"] = spike_any
        rows.append(r)
    f = pd.DataFrame(rows)
    out = pd.concat([ev.reset_index(drop=True), f], axis=1)
    # 5 post-trigger
    out["retest"] = (out.kind == "limit").astype(int)
    out["chase_dist"] = np.where(out.kind == "chase", out.side * (out.fill - out.level), np.nan)
    return out


def quint(x: pd.DataFrame, f: str, edges_from: pd.DataFrame) -> pd.DataFrame:
    e = np.unique(np.nanquantile(edges_from[f], [0, .2, .4, .6, .8, 1]))
    if len(e) < 3:
        q = x[f]
        lab = q.astype(str)
    else:
        e[0], e[-1] = -np.inf, np.inf
        lab = pd.cut(x[f], e)
    g = x.groupby(lab, observed=True)
    return pd.DataFrame({"n": g.size(), "win%": 100 * g.usd.apply(lambda s: (s > 0).mean()),
                         "$/oz": g.usd.mean(), "EA_take%": 100 * g.ea.mean()}).round(2)


def main() -> None:
    sim = pd.read_parquet(OUT / "aero_daybreak_sim.parquet")
    ages = pd.read_parquet(OUT / "aero_knn_events_5m.parquet")[["t", "side", "level", "level_age"]]
    sim = sim.merge(ages, on=["t", "side", "level"], how="left")
    ev = add_features(sim, ads.load_m1())
    ev.to_parquet(OUT / "aero_new_features.parquet")
    feats = ["room_swing", "room_pdpw", "room_min", "room_r50", "lock_path60", "lock_atr",
             "confluence", "since_release", "release_day", "retest", "chase_dist"]
    y = ev.ea.to_numpy()
    held = (ev.level_age >= 2).to_numpy()
    print(f"breaks {len(ev)} (held >= 2h: {held.sum()}), EA orders {y.sum()}\n")
    print("EA-choice AUC (whole period; labels, not outcomes):")
    print(pd.DataFrame([dict(feature=f, all=apm.auc(ev[f].to_numpy(float), y),
                             held2h=apm.auc(ev.loc[held, f].to_numpy(float), y[held]),
                             med_ea=np.nanmedian(ev.loc[y, f]), med_skip=np.nanmedian(ev.loc[~y, f]))
                        for f in feats]).round(3).to_string(index=False))
    is_ = (ev.t < IS_END).to_numpy()
    base = ev[is_]
    print(f"\nOUTCOMES 2018-22 only. Baseline: all {100 * (base.usd > 0).mean():.1f}% win, "
          f"{base.usd.mean():+.2f} $/oz | held>=2h {100 * (base[held[is_]].usd > 0).mean():.1f}% win, "
          f"{base[held[is_]].usd.mean():+.2f} $/oz")
    for f in feats:
        print(f"\n== {f}")
        a = quint(base, f, base)
        b = quint(base[held[is_]], f, base[held[is_]])
        print("all breaks:")
        print(a.T.to_string())
        print("held >= 2h:")
        print(b.T.to_string())


if __name__ == "__main__":
    main()
