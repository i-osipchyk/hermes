"""Walk-forward kNN filter over gold day-range breakouts (idea 87eb623e, Aero MT5 replica).

Question: can a kNN over observable, causal features pick the day-range breaks that win --
the way the Aero MT5 EA claims to -- when it is only ever allowed to learn from the past?

Events   Every 15m bar that trades $0.50 through the running high/low of the broker day
         (server day = 17:00 New York), inside 02:30-23:00 server time.  A bar that breaks
         both sides is skipped (order unknowable).
Outcome  The aero_replica_atr exits, simulated on 15m bars: entry at level +/- $0.50 (or the
         bar open if it gapped through), TP level + 0.33 x ATR(4h,20), catastrophe stop
         3.6 x ATR, and once >= 3h old and underwater at a bar close a stop at entry -/+
         0.5 x ATR (exit at that close if already beyond).  Stop-first on a bar that touches
         both.  10-day cap.  Target = net R = (P&L/oz - $0.20 costs) / ATR.
Features Known at the OPEN of the breaking bar (closed bars only; ATR from closed 4h bars):
         returns over 30m/2h/24h/5d/20d/13w/26w, position in week/month range, distance to the
         previous day/week extreme, ATR ratio, day range so far, level age, prior breaks and
         touches today, server hour and weekday (cyclic).  All direction-signed.
kNN      Monthly walk-forward from 2019-01: the model for month M is fitted on events whose
         outcome had RESOLVED (exit_time) before M starts -- an automatic embargo -- with
         z-scores from that training set only.  Score = distance-weighted mean net R of the
         k nearest neighbours; trade when score > thr.
Protocol k / thr / feature set chosen on the 2019-2022 walk-forward predictions only, then
         2023-01..2026-09 scored once.  Baselines: all events, and random subsets of the same
         size (the selection must beat chance, not zero).

Fill-bar target: the entry is a stop order, so price cannot reach the target without first
crossing the trigger -- a target touched on the entry bar is booked (stop-first if the
catastrophe stop was touched on that bar too).  The first version skipped the entry bar.

Run:  .venv/bin/python research/aero/aero_knn_filter.py [5m|15m]   (default 15m)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "research" / "output"
TF = sys.argv[1] if len(sys.argv) > 1 else "15m"
TF_MIN = int(TF.rstrip("m"))
PER_H = 60 // TF_MIN
BARS = ROOT / ".hermes_cache" / "bars" / "pepperstone" / f"XAUUSD_{TF}.parquet"

BREAK_USD = 0.50
TP_ATR, GRID_ATR, CAT_ATR = 0.33, 0.5, 3.6
GRID_AFTER_BARS = 3 * PER_H     # 3h
MAX_BARS = 10 * 24 * PER_H
COST_USD = 0.20                 # spread 0.12 + commission ~0.07 per oz, round trip
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
WF_START = pd.Timestamp("2019-01-01", tz="UTC")

FEATURES = [
    "r30m", "r2h", "r24h", "r5d", "r20d", "r13w", "r26w",
    "pos_week", "pos_month", "dist_pd", "dist_pw",
    "vr", "dayrange", "level_age", "n_breaks", "touches",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
]
COMPACT = ["r2h", "r24h", "r5d", "pos_week", "dist_pd", "vr", "dayrange", "hour_sin", "hour_cos"]


def wilder(tr: pd.Series, n: int) -> pd.Series:
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def true_range(b: pd.DataFrame) -> pd.Series:
    pc = b.close.shift()
    return pd.concat([b.high - b.low, (b.high - pc).abs(), (b.low - pc).abs()], axis=1).max(axis=1)


def load() -> pd.DataFrame:
    m = pd.read_parquet(BARS)
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    m = m[["open", "high", "low", "close"]]
    srv = m.index.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    m["srv"] = srv
    m["sday"] = srv.normalize()
    return m


def closed_series(m: pd.DataFrame, s: pd.Series, period_end: pd.Series) -> np.ndarray:
    """Value of a per-period series as known at each 15m bar's open (period fully closed)."""
    known = pd.Series(s.values, index=period_end.values).sort_index()
    return known.reindex(m.index, method="ffill").values


def build_events(m: pd.DataFrame) -> pd.DataFrame:
    n = len(m)
    o, h, lo, c = (m[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = m.index

    # --- higher-timeframe context, each known only after its bar closes ---------------
    h4 = m.resample("4h", on="srv").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    h4_end = (h4.index + pd.Timedelta("4h")).tz_localize(None)
    atr20 = wilder(true_range(h4), 20)
    atr120 = wilder(true_range(h4), 120)
    d1 = m.groupby("sday").agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    d1_end = d1.index + pd.Timedelta("1D")
    atrd = wilder(true_range(d1), 14)

    srv_ts = m.srv.to_numpy()

    def at_open(series: pd.Series, ends) -> np.ndarray:
        s = pd.Series(series.values, index=pd.DatetimeIndex(ends)).sort_index()
        pos = np.searchsorted(s.index.values, srv_ts, side="right") - 1
        out = np.where(pos >= 0, s.values[np.clip(pos, 0, None)], np.nan)
        return out

    a4 = at_open(atr20, h4_end)
    a4l = at_open(atr120, h4_end)
    ad = at_open(atrd, d1_end)
    d_close = d1.close
    c5, c20, c65, c130 = (at_open(d_close.shift(k - 1), d1_end) for k in (5, 20, 65, 130))
    pdh, pdl = at_open(d1.high, d1_end), at_open(d1.low, d1_end)

    wk = m.srv.dt.to_period("W-SAT")      # week starts Sunday (server)
    mo = m.srv.dt.to_period("M")
    w_hi = m.high.groupby(wk.values).cummax().shift().where(wk.ne(wk.shift()) == False)  # noqa: E712
    w_lo = m.low.groupby(wk.values).cummin().shift().where(wk.ne(wk.shift()) == False)  # noqa: E712
    mo_hi = m.high.groupby(mo.values).cummax().shift().where(mo.ne(mo.shift()) == False)  # noqa: E712
    mo_lo = m.low.groupby(mo.values).cummin().shift().where(mo.ne(mo.shift()) == False)  # noqa: E712
    wkly = m.groupby(wk.values).agg(high=("high", "max"), low=("low", "min"))
    prev_wh = wk.map(wkly.high.shift()).to_numpy()
    prev_wl = wk.map(wkly.low.shift()).to_numpy()
    w_hi, w_lo, mo_hi, mo_lo = (x.to_numpy() for x in (w_hi, w_lo, mo_hi, mo_lo))

    sday = m.sday.to_numpy()
    mins = (m.srv.dt.hour * 60 + m.srv.dt.minute).to_numpy()
    dow = m.srv.dt.dayofweek.to_numpy()

    rows = []
    day = None
    for t in range(1, n):
        if sday[t] != day:
            day, dhi, dlo, dhi_t, dlo_t = sday[t], h[t], lo[t], t, t
            nb = {1: 0, -1: 0}
            touch = {1: 0, -1: 0}
            continue
        if 150 <= mins[t] < 23 * 60 and not np.isnan(a4[t]) and not np.isnan(ad[t]):
            up = h[t] >= dhi + BREAK_USD
            dn = lo[t] <= dlo - BREAK_USD
            if up != dn:
                d = 1 if up else -1
                level = dhi if up else dlo
                trig = level + d * BREAK_USD
                fill = max(o[t], trig) if up else min(o[t], trig)
                res = simulate(t, d, level, fill, a4[t], o, h, lo, c, n)
                if res is not None:
                    pc = c[t - 1]
                    wr = w_hi[t] - w_lo[t]
                    mr = mo_hi[t] - mo_lo[t]
                    rows.append(dict(
                        t=idx[t], side=d, level=level, fill=fill, atr=a4[t],
                        exit_time=idx[res[0]], net_r=(res[1] - COST_USD) / a4[t],
                        gross_usd=res[1], how=res[2],
                        r30m=d * (pc - c[t - 1 - PER_H // 2]) / a4[t],
                        r2h=d * (pc - c[t - 1 - 2 * PER_H]) / a4[t],
                        r24h=d * (pc - c[max(t - 1 - 24 * PER_H, 0)]) / a4[t],
                        r5d=d * (pc - c5[t]) / ad[t], r20d=d * (pc - c20[t]) / ad[t],
                        r13w=d * (pc - c65[t]) / ad[t], r26w=d * (pc - c130[t]) / ad[t],
                        pos_week=((level - w_lo[t]) / wr if d > 0 else (w_hi[t] - level) / wr)
                        if wr > 0 else np.nan,
                        pos_month=((level - mo_lo[t]) / mr if d > 0 else (mo_hi[t] - level) / mr)
                        if mr > 0 else np.nan,
                        dist_pd=(pdh[t] - level) / ad[t] if d > 0 else (level - pdl[t]) / ad[t],
                        dist_pw=(prev_wh[t] - level) / ad[t] if d > 0 else (level - prev_wl[t]) / ad[t],
                        vr=a4[t] / a4l[t], dayrange=(dhi - dlo) / ad[t],
                        level_age=(t - (dhi_t if d > 0 else dlo_t)) / PER_H,
                        n_breaks=nb[d], touches=touch[d],
                        hour_sin=np.sin(2 * np.pi * mins[t] / 1440),
                        hour_cos=np.cos(2 * np.pi * mins[t] / 1440),
                        dow_sin=np.sin(2 * np.pi * dow[t] / 7), dow_cos=np.cos(2 * np.pi * dow[t] / 7),
                    ))
                nb[d] += 1
        # update the running range with bar t (closed) and count near-touches of the extremes
        if a4[t] == a4[t]:
            if h[t] < dhi and dhi - h[t] <= 0.1 * a4[t]:
                touch[1] += 1
            if lo[t] > dlo and lo[t] - dlo <= 0.1 * a4[t]:
                touch[-1] += 1
        if h[t] > dhi:
            dhi, dhi_t, touch[1] = h[t], t, 0
        if lo[t] < dlo:
            dlo, dlo_t, touch[-1] = lo[t], t, 0
    return pd.DataFrame(rows)


def simulate(t, d, level, fill, atr, o, h, lo, c, n):
    """Replica exits from the entry bar on. Returns (exit_bar, gross $/oz, reason)."""
    tp = level + d * TP_ATR * atr
    stop = level - d * CAT_ATR * atr
    armed = False
    # entry bar: the target lies beyond the trigger, so touching it came after the fill
    if (h[t] >= tp) if d > 0 else (lo[t] <= tp):
        if (lo[t] <= stop) if d > 0 else (h[t] >= stop):
            return t, d * (stop - fill), "stop"
        exit_px = max(tp, fill) if d > 0 else min(tp, fill)   # gapped past the target
        return t, d * (exit_px - fill), "tp"
    for j in range(t + 1, min(t + MAX_BARS, n)):
        if d > 0:
            if lo[j] <= stop:
                return j, min(o[j], stop) - fill, "stop"
            if h[j] >= tp:
                return j, max(o[j], tp) - fill, "tp"
        else:
            if h[j] >= stop:
                return j, fill - max(o[j], stop), "stop"
            if lo[j] <= tp:
                return j, fill - min(o[j], tp), "tp"
        if not armed and j - t + 1 >= GRID_AFTER_BARS and d * (c[j] - fill) < 0:
            armed = True
            g = fill - d * GRID_ATR * atr
            if d * (c[j] - g) <= 0:
                return j, d * (c[j] - fill), "grid"
            stop = g
    j = min(t + MAX_BARS, n) - 1
    return (j, d * (c[j] - fill), "cap") if j > t else None


def knn_walkforward(ev: pd.DataFrame, feats: list[str], ks: list[int]) -> pd.DataFrame:
    """Monthly expanding-window kNN; returns a score column per k for every event >= WF_START."""
    X = ev[feats].to_numpy(float)
    y = ev.net_r.to_numpy(float)
    out = {k: np.full(len(ev), np.nan) for k in ks}
    months = pd.date_range(WF_START, ev.t.max() + pd.offsets.MonthBegin(), freq="MS", tz="UTC")
    for a, b in zip(months[:-1], months[1:]):
        test = np.flatnonzero((ev.t >= a).to_numpy() & (ev.t < b).to_numpy())
        train = np.flatnonzero((ev.exit_time < a).to_numpy())
        if len(test) == 0 or len(train) < 500:
            continue
        mu, sd = np.nanmean(X[train], 0), np.nanstd(X[train], 0) + 1e-9
        Xtr = np.nan_to_num((X[train] - mu) / sd)
        Xte = np.nan_to_num((X[test] - mu) / sd)
        d2 = (Xte ** 2).sum(1)[:, None] + (Xtr ** 2).sum(1)[None, :] - 2 * Xte @ Xtr.T
        dist = np.sqrt(np.maximum(d2, 0))
        order = np.argsort(dist, axis=1)
        for k in ks:
            nn = order[:, :k]
            w = 1 / (np.take_along_axis(dist, nn, 1) + 1e-6)
            out[k][test] = (w * y[train][nn]).sum(1) / w.sum(1)
    return pd.DataFrame({f"s{k}": v for k, v in out.items()}, index=ev.index)


def sequential(sel: pd.DataFrame) -> pd.Series:
    """One position at a time: take a selected event only if flat."""
    taken, free = [], pd.Timestamp.min.tz_localize("UTC")
    for i, r in sel.sort_values("t").iterrows():
        if r.t >= free:
            taken.append(i)
            free = r.exit_time
    return sel.loc[taken, "net_r"]


def stats(r: pd.Series) -> dict:
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    return dict(n=len(r), mean_r=round(r.mean(), 4), win=round((r > 0).mean(), 3),
                pf=round(pos / neg, 3) if neg else np.inf, sum_r=round(r.sum(), 1))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cache = OUT / f"aero_knn_events_{TF}.parquet"
    if cache.exists():
        ev = pd.read_parquet(cache)
    else:
        ev = build_events(load())
        ev.to_parquet(cache)
    ev = ev.sort_values("t").reset_index(drop=True)
    print(f"events {len(ev)}  {ev.t.min():%Y-%m-%d}..{ev.t.max():%Y-%m-%d}")
    print("all events by year:")
    print(ev.groupby(ev.t.dt.year).net_r.agg(["size", "mean"]).round(4).T.to_string())

    ks = [25, 50, 100, 200, 400]
    thrs = [0.0, 0.05, 0.10]
    sets = {"full": FEATURES, "compact": COMPACT}
    scores = {name: knn_walkforward(ev, f, ks) for name, f in sets.items()}

    wf = ev.t >= WF_START
    is_, oos = wf & (ev.t < IS_END), ev.t >= IS_END
    print("\nbaseline all events  IS", stats(ev.net_r[is_]), " OOS", stats(ev.net_r[oos]))
    print("baseline sequential  IS", stats(sequential(ev[is_])), " OOS", stats(sequential(ev[oos])))

    grid = []
    for name, sc in scores.items():
        for k in ks:
            for thr in thrs:
                pick = sc[f"s{k}"] > thr
                s_is = stats(ev.net_r[is_ & pick])
                grid.append(dict(set=name, k=k, thr=thr, **{f"is_{a}": b for a, b in s_is.items()}))
    g = pd.DataFrame(grid)
    print("\nIN-SAMPLE grid (2019-2022 walk-forward predictions):")
    print(g.to_string(index=False))

    # selection: best IS mean R per event among configs keeping >= 300 IS events
    best = g[g.is_n >= 300].sort_values("is_mean_r", ascending=False).iloc[0]
    print(f"\nSELECTED on IS: set={best.set} k={best.k} thr={best.thr}")
    sc = scores[best.set][f"s{int(best.k)}"]
    pick = sc > best.thr
    sel_oos = ev[oos & pick]
    print("OOS selected (per event):", stats(sel_oos.net_r))
    print("OOS selected (sequential):", stats(sequential(sel_oos)))
    print("OOS skipped  (per event):", stats(ev.net_r[oos & ~pick]))

    rng = np.random.default_rng(0)
    pool = ev.net_r[oos].to_numpy()
    rand = np.array([rng.choice(pool, len(sel_oos), replace=False).mean() for _ in range(2000)])
    p = (rand >= sel_oos.net_r.mean()).mean()
    print(f"random subsets of {len(sel_oos)}: mean R {rand.mean():.4f} sd {rand.std():.4f}; "
          f"P(random >= selected) = {p:.3f}")
    print("OOS selected by year:")
    print(sel_oos.groupby(sel_oos.t.dt.year).net_r.agg(["size", "mean"]).round(4).T.to_string())
    print("\nOOS score deciles (selected config) -> mean net R:")
    q = pd.qcut(sc[oos], 10, labels=False, duplicates="drop")
    print(ev.net_r[oos].groupby(q).mean().round(4).to_string())
    pd.concat([ev, *[s.add_prefix(n + "_") for n, s in scores.items()]], axis=1).to_parquet(
        OUT / f"aero_knn_scored_{TF}.parquet")


if __name__ == "__main__":
    main()
