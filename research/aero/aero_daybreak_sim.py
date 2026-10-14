"""The Aero EA's entry/exit mechanics applied to every current-day high/low breakout.

Simulated on 1m bars, per break (each break independently), 2018-01-01..2026-08-06, inside
02:30-23:00 server:
  Trigger  first minute trading $0.50 through the running broker-day high/low.
  Entry    limit at the level, live ~1.5 min: filled if the NEXT minute trades at the level
           (at the level, or the open if better); otherwise a market chase at the open of the
           minute after.  (The trigger minute itself is not used: its path order is unknown.)
  Exits    from the minute after the fill, each minute in this order:
             stop      low (high) through the stop -> exit at the stop, or the open if gapped
             target    entry +/- $20
             trail     once price is $12 in profit the stop locks entry +/- $10, then follows
                       $2 behind in $0.35 steps (log: first lock always +$10.00, steps $0.35)
             3h rule   at the first in-session minute >= 180 min after entry, if the close is
                       under water, a stop at entry -/+ 0.5 x ATR(4h,20); if already beyond,
                       exit at that close (the recovery grid's first leg closes the basket)
             catastrophe stop entry -/+ 3.6 x ATR; 10-day cap
  Costs    $0.20/oz round trip.  R = net $/oz / ATR.
Compared:  all breaks; breaks the EA ordered (same simulation); the EA's own logged result on
           those trades; and all breaks taken one position at a time.

Run:  .venv/bin/python research/aero/aero_daybreak_sim.py   (after aero_knn_filter.py 5m)
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
M1 = Path(__file__).resolve().parents[2] / ".hermes_cache/bars/pepperstone/XAUUSD_1m.parquet"
START, END = pd.Timestamp("2018-01-01", tz="UTC"), pd.Timestamp("2026-08-06", tz="UTC")
TP_USD, LOCK_AT, LOCK, TRAIL_GAP, STEP = 20.0, 12.0, 10.0, 2.0, 0.35
GRID_ATR, CAT_ATR, GRID_MIN, COST = 0.5, 3.6, 180, 0.20
MAX_MIN = 10 * 1440


def load_m1() -> pd.DataFrame:
    m = pd.read_parquet(M1)
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    srv = m.index.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    m["smin"] = srv.hour * 60 + srv.minute
    return m


def simulate(m: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    o, h, lo, c = (m[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    smin = m.smin.to_numpy()
    ts = m.index.to_numpy()
    start = np.searchsorted(ts, ev.t.to_numpy())
    out = []
    for (_, e), s in zip(ev.iterrows(), start):
        d, L, atr = e.side, e.level, e.atr
        trig = L + d * 0.5
        j = next((k for k in range(s, min(s + 5, len(m)))
                  if ((h[k] >= trig) if d > 0 else (lo[k] <= trig))), None)
        if j is None or j + 2 >= len(m):
            out.append(dict(fill=np.nan))
            continue
        k = j + 1
        if (lo[k] <= L) if d > 0 else (h[k] >= L):
            entry, kind, f = (min(L, o[k]) if d > 0 else max(L, o[k])), "limit", k
        else:
            entry, kind, f = o[k + 1], "chase", k + 1
        tp = entry + d * TP_USD
        stop = entry - d * CAT_ATR * atr
        locked, armed_checked = False, False
        res = None
        for q in range(f + 1, min(f + MAX_MIN, len(m))):
            if (lo[q] <= stop) if d > 0 else (h[q] >= stop):
                px = min(stop, o[q]) if d > 0 else max(stop, o[q])
                res = (q, px, "trail" if locked else "stop")
                break
            if (h[q] >= tp) if d > 0 else (lo[q] <= tp):
                res = (q, tp, "tp")
                break
            best = d * ((h[q] if d > 0 else lo[q]) - entry)
            if best >= LOCK_AT:
                steps = np.floor((best - TRAIL_GAP - LOCK) / STEP)
                new = entry + d * (LOCK + max(steps, 0) * STEP)
                stop = max(stop, new) if d > 0 else min(stop, new)
                locked = True
            if not armed_checked and q - f >= GRID_MIN and 150 <= smin[q] < 1380:
                armed_checked = True
                if d * (c[q] - entry) < 0:
                    g = entry - d * GRID_ATR * atr
                    if d * (c[q] - g) <= 0:
                        res = (q, c[q], "grid")
                        break
                    stop = max(stop, g) if d > 0 else min(stop, g)
        if res is None:
            q = min(f + MAX_MIN, len(m)) - 1
            res = (q, c[q], "cap")
        q, px, why = res
        usd = d * (px - entry) - COST
        out.append(dict(fill=entry, kind=kind, exit_time=m.index[q], how=why, usd=usd, r=usd / atr))
    return pd.concat([ev.reset_index(drop=True), pd.DataFrame(out)], axis=1)


def ea_trades() -> pd.DataFrame:
    """EA episodes from the 2018-2026 log with $/oz on the original position."""
    text = aas.LOG.read_bytes().decode("utf-16")
    text = text[text.index("from 2018.01.01"):]
    pos = cost = 0.0
    cur, eps = None, []
    pat = re.compile(r"(\d{4}\.\d\d\.\d\d \d\d:\d\d:\d\d)\s+deal #\d+ (buy|sell) ([\d.]+) XAUUSD at ([\d.]+)")
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 5 or p[3] != "Trades":
            continue
        mm = pat.match(p[4])
        if not mm:
            continue
        t, s, lot, px = mm.groups()
        q, px = float(lot) * (1 if s == "buy" else -1), float(px)
        if abs(pos) < 1e-9:
            cur = dict(open=aas.server_to_utc(pd.Timestamp(t.replace(".", "-", 2))),
                       side=1 if q > 0 else -1, lot0=abs(q), pnl=0.0, legs=0)
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
                cur["usd_oz"] = cur["pnl"] / (cur["lot0"] * 100)
                eps.append(cur)
    return pd.DataFrame(eps)


def summary(x: pd.DataFrame, col: str = "r") -> dict:
    r = x[col]
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    return dict(n=len(x), win=round(100 * (r > 0).mean(), 1), usd_oz=round(x.usd.mean(), 2),
                r=round(r.mean(), 3), pf=round(pos / neg, 2) if neg else np.inf,
                limit_fill=round(100 * (x.kind == "limit").mean(), 1))


def sequential(x: pd.DataFrame) -> pd.DataFrame:
    keep, free = [], pd.Timestamp.min.tz_localize("UTC")
    for i, r in x.sort_values("t").iterrows():
        if r.t >= free:
            keep.append(i)
            free = r.exit_time
    return x.loc[keep]


def main() -> None:
    ev = pd.read_parquet(OUT / "aero_knn_events_5m.parquet")[["t", "side", "level", "atr"]]
    ev = ev[(ev.t >= START) & (ev.t < END)].reset_index(drop=True)
    orders, _ = aas.parse_log()
    tv = ev.t.dt.tz_localize(None).to_numpy()
    ev["ea"] = False
    for t, side, px in orders.itertuples(index=False):
        bar = t.floor("5min").tz_localize(None).to_datetime64()
        near = np.flatnonzero((tv >= bar - np.timedelta64(5, "m")) & (tv <= bar + np.timedelta64(5, "m"))
                              & (ev.side.to_numpy() == side))
        if len(near):
            dist = np.abs(ev.level.to_numpy()[near] - px)
            if dist.min() < 1.5:
                ev.loc[near[dist.argmin()], "ea"] = True
    sim = simulate(load_m1(), ev).dropna(subset=["fill"])
    sim.to_parquet(OUT / "aero_daybreak_sim.parquet")

    eps = ea_trades()
    picks = sim[sim.ea].sort_values("t")
    j = pd.merge_asof(picks, eps.sort_values("open"), left_on="t", right_on="open", by="side",
                      tolerance=pd.Timedelta("10min"), direction="forward").dropna(subset=["open"])
    print(f"day-range breaks simulated: {len(sim)}; EA-ordered among them: {sim.ea.sum()}; "
          f"with an EA trade in the log: {len(j)}\n")
    print("simulator vs EA's own log, same trades:")
    print(f"   sim  win {100 * (j.usd > 0).mean():.1f}%  $/oz {j.usd.mean():+.2f}")
    print(f"   EA   win {100 * (j.usd_oz > 0).mean():.1f}%  $/oz {j.usd_oz.mean():+.2f}  "
          f"(outcome agreement {100 * ((j.usd > 0) == (j.usd_oz > 0)).mean():.1f}%)\n")

    rows = {"all day-range breaks": sim, "EA-ordered breaks": sim[sim.ea],
            "all breaks, one at a time": sequential(sim), "breaks the EA skipped": sim[~sim.ea]}
    print(pd.DataFrame({k: summary(v) for k, v in rows.items()}).T.to_string())
    print("\nby year, avg R (and $/oz):")
    yr = sim.t.dt.year
    tab = pd.DataFrame({
        "all R": sim.groupby(yr).r.mean(), "all $/oz": sim.groupby(yr).usd.mean(),
        "EA R": sim[sim.ea].groupby(yr[sim.ea]).r.mean(),
        "EA $/oz": sim[sim.ea].groupby(yr[sim.ea]).usd.mean(),
        "all n": sim.groupby(yr).size(), "EA n": sim[sim.ea].groupby(yr[sim.ea]).size()})
    print(tab.round(3).to_string())
    print("\nexit reasons:", {k: sim[k2].how.value_counts(normalize=True).round(3).to_dict()
                              for k, k2 in (("all", sim.index == sim.index), ("EA", sim.ea))})


if __name__ == "__main__":
    main()
