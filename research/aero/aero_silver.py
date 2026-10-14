"""Does the Aero EA's break selection transfer to silver?

The EA's kNN cases come from gold.  On silver its dollar exits do not transfer (a +$12 lock is
~13% of the price), so silver P&L says nothing about selection.  Here every silver break is scored
with the EA's 2026 gold trade geometry expressed in ATR(4h,20) units, and the EA's silver picks are
compared with all silver breaks.  Gold over the same window is the reference.

Geometry (gold 2026: ATR ~37, lock at +$12 -> +$10, trail $2 behind in $0.35 steps, TP $20):
  lock at +0.32 ATR -> stop to +0.27 ATR, then trail 0.054 ATR behind in 0.0095 ATR steps;
  TP +0.54 ATR; at 3h if under water a stop at -0.5 ATR (exit at the close if beyond);
  catastrophe -3.6 ATR; entry = limit at the level if the next minute retests it, else the
  next minute's open (chase).  Costs 0.005 ATR.  Outcome in R = move / ATR.
Events  every 1m bar trading `trig` beyond the running broker-day high/low (02:30-23:00 server);
        gold trig $0.50, silver $0.05 (the EA's observed silver offset).
Labels  the EA's limit orders from the tester logs (the last run in each log), same side, within
        +-2 minutes, level within $1.5 (gold) / $0.15 (silver).

Run:  .venv/bin/python research/aero/aero_silver.py
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

ROOT = Path(__file__).resolve().parents[2]
OUT = akf.OUT
BARS = ROOT / ".hermes_cache/bars/pepperstone"
START, END = pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-08-06", tz="UTC")
G = dict(lock_at=0.32, lock=0.27, gap=0.054, step=0.0095, tp=0.54, grid=0.5, cat=3.6, cost=0.005)


def load(sym: str) -> pd.DataFrame:
    m = pd.read_parquet(BARS / f"{sym}_1m.parquet")
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    m = m[(m.index >= START - pd.Timedelta(days=40)) & (m.index < END + pd.Timedelta(days=12))]
    srv = m.index.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    m["sday"], m["smin"] = srv.normalize(), srv.hour * 60 + srv.minute
    h4 = m.resample("4h").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    a = akf.wilder(akf.true_range(h4), 20)
    s = pd.Series(a.values, index=h4.index + pd.Timedelta("4h"))
    m["atr"] = s.reindex(m.index, method="ffill").to_numpy()
    return m


def events(m: pd.DataFrame, trig: float) -> pd.DataFrame:
    h, lo, sday, smin = m.high.to_numpy(), m.low.to_numpy(), m.sday.to_numpy(), m.smin.to_numpy()
    rows, day = [], None
    for t in range(len(m)):
        if sday[t] != day:
            day, hi, lw, hi_t, lw_t = sday[t], h[t], lo[t], t, t
            continue
        if 150 <= smin[t] < 1380 and m.index[t] >= START and m.index[t] < END:
            up, dn = h[t] >= hi + trig, lo[t] <= lw - trig
            if up != dn:
                d, L, set_t = (1, hi, hi_t) if up else (-1, lw, lw_t)
                rows.append(dict(i=t, t=m.index[t], side=d, level=L, age_h=(t - set_t) / 60))
        if h[t] > hi:
            hi, hi_t = h[t], t
        if lo[t] < lw:
            lw, lw_t = lo[t], t
    return pd.DataFrame(rows)


def simulate(m: pd.DataFrame, ev: pd.DataFrame, trig: float) -> pd.DataFrame:
    o, h, lo, c, atr = (m[k].to_numpy(float) for k in ("open", "high", "low", "close", "atr"))
    smin = m.smin.to_numpy()
    out = []
    for e in ev.itertuples():
        j, d, L, a = e.i, e.side, e.level, atr[e.i]
        if not a == a or j + 2 >= len(m):
            out.append(dict(r=np.nan, kind=None))
            continue
        k = j + 1
        if (lo[k] <= L) if d > 0 else (h[k] >= L):
            entry, kind, f = (min(L, o[k]) if d > 0 else max(L, o[k])), "limit", k
        else:
            entry, kind, f = o[k + 1], "chase", k + 1
        tp, stop = entry + d * G["tp"] * a, entry - d * G["cat"] * a
        locked = checked = False
        res = None
        for q in range(f + 1, min(f + 14400, len(m))):
            if (lo[q] <= stop) if d > 0 else (h[q] >= stop):
                res = min(stop, o[q]) if d > 0 else max(stop, o[q])
                break
            if (h[q] >= tp) if d > 0 else (lo[q] <= tp):
                res = tp
                break
            best = d * ((h[q] if d > 0 else lo[q]) - entry) / a
            if best >= G["lock_at"]:
                steps = max(np.floor((best - G["gap"] - G["lock"]) / G["step"]), 0)
                new = entry + d * (G["lock"] + steps * G["step"]) * a
                stop = max(stop, new) if d > 0 else min(stop, new)
                locked = True
            if not checked and q - f >= 180 and 150 <= smin[q] < 1380:
                checked = True
                if d * (c[q] - entry) < 0:
                    g = entry - d * G["grid"] * a
                    if d * (c[q] - g) <= 0:
                        res = c[q]
                        break
                    stop = max(stop, g) if d > 0 else min(stop, g)
        if res is None:
            res = c[min(f + 14399, len(m) - 1)]
        out.append(dict(r=d * (res - entry) / a - G["cost"], kind=kind, locked=locked))
    return pd.concat([ev.reset_index(drop=True), pd.DataFrame(out)], axis=1)


def orders(log: Path, run_marker: str, sym: str) -> pd.DataFrame:
    pat = re.compile(rf"(\d{{4}}\.\d\d\.\d\d \d\d:\d\d:\d\d)\s+(buy|sell) limit [\d.]+ {sym} at ([\d.]+) sl:")
    rows, on = [], False
    with open(log, encoding="utf-16") as fh:
        for line in fh:
            if run_marker in line:
                on, rows = True, []          # keep only the last run in the file
                continue
            if not on:
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) < 5 or p[3] != "Trade":
                continue
            mm = pat.match(p[4])
            if mm:
                t = aas.server_to_utc(pd.Timestamp(mm.group(1).replace(".", "-", 2)))
                rows.append((t, 1 if mm.group(2) == "buy" else -1, float(mm.group(3))))
    o = pd.DataFrame(rows, columns=["t", "side", "px"])
    return o[(o.t >= START) & (o.t < END)]


def label(ev: pd.DataFrame, od: pd.DataFrame, tol: float) -> pd.DataFrame:
    ev = ev.copy()
    ev["ea"] = False
    tv = ev.t.dt.tz_convert(None).to_numpy()
    for t, side, px in od.itertuples(index=False):
        near = np.flatnonzero((np.abs(tv - t.tz_convert(None).to_datetime64()) <= np.timedelta64(2, "m"))
                              & (ev.side.to_numpy() == side))
        if len(near):
            dist = np.abs(ev.level.to_numpy()[near] - px)
            if dist.min() < tol:
                ev.loc[ev.index[near[dist.argmin()]], "ea"] = True
    return ev


def report(name: str, ev: pd.DataFrame, n_orders: int) -> None:
    ev = ev.dropna(subset=["r"])
    a, p = ev, ev[ev.ea]
    held = ev.age_h >= 2

    def s(x: pd.DataFrame) -> str:
        return (f"n={len(x):6d}  win {100 * (x.r > 0).mean():5.1f}%  "
                f"lock reached {100 * x.locked.mean():5.1f}%"
                f"  mean R {x.r.mean():+.3f}")
    print(f"\n{name}: EA orders {n_orders}, matched to a day-high/low break {int(ev.ea.sum())}")
    print("  all breaks       ", s(a))
    print("  held >= 2h       ", s(a[held]))
    print("  EA picks         ", s(p))
    print(f"  EA picks: level held >= 2h {100 * (p.age_h >= 2).mean():.0f}% (all breaks "
          f"{100 * held.mean():.0f}%), median level age {p.age_h.median():.1f}h vs {a.age_h.median():.2f}h; "
          f"limit fills {100 * (p.kind == 'limit').mean():.0f}% vs {100 * (a.kind == 'limit').mean():.0f}%")
    rng = np.random.default_rng(0)
    draws = np.array([rng.choice(a.r.to_numpy(), len(p), replace=False).mean() for _ in range(5000)])
    print(f"  random sets of {len(p)} breaks: mean R {draws.mean():+.3f} (sd {draws.std():.3f}); "
          f"P(random >= EA picks) = {(draws >= p.r.mean()).mean():.4f}")


def main() -> None:
    for sym, trig, log, marker, tol in (
        ("XAUUSD", 0.50, aas.LOGS / "20261009 copy.log", "XAUUSD,H1: testing of", 1.5),
        ("XAGUSD", 0.05, aas.LOGS / "20261009 copy 2.log", "XAGUSD,H1: testing of", 0.15),
    ):
        m = load(sym)
        ev = simulate(m, events(m, trig), trig)
        od = orders(log, marker, sym)
        ev = label(ev, od, tol)
        ev.to_parquet(OUT / f"aero_silver_{sym}.parquet")
        report(sym, ev, len(od))


if __name__ == "__main__":
    main()
