"""aero_silver.py over the full 2018-2026.08 silver backtest, with gold as the reference.

Same scoring (the EA's 2026 gold trade geometry in ATR units, every 1m day-high/low break),
same labels (the EA's limit orders, last run in each log), reported overall and by year, with a
check that the silver edge does not come from moments when gold also broke out.

Run:  .venv/bin/python research/aero/aero_silver_full.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import aero_silver as asv  # noqa: E402

ROOT = asv.ROOT
RUNS = (("XAUUSD", 0.50, "20261009 copy.log", 1.5), ("XAGUSD", 0.05, "20261009 copy 3.log", 0.15))


def run(sym: str, trig: float, log: str, tol: float) -> pd.DataFrame:
    cache = asv.OUT / f"aero_silver_full_{sym}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    asv.START = pd.Timestamp("2018-02-01", tz="UTC")
    asv.END = pd.Timestamp("2026-08-06", tz="UTC")
    m = asv.load(sym)
    ev = asv.simulate(m, asv.events(m, trig), trig)
    od = asv.orders(asv.aas.LOGS / log, f"{sym},H1: testing of", sym)
    ev = asv.label(ev, od, tol).dropna(subset=["r"])
    ev.attrs["orders"] = len(od)
    ev.to_parquet(cache)
    return ev


def summary(ev: pd.DataFrame) -> pd.DataFrame:
    y = ev.t.dt.year
    g = ev.groupby(y)
    p = ev[ev.ea].groupby(y[ev.ea])
    t = pd.DataFrame({"breaks": g.size(), "all_R": g.r.mean(), "picks": p.size(),
                      "picks_win%": 100 * p.r.apply(lambda s: (s > 0).mean()), "picks_R": p.r.mean()})
    t["lift"] = t.picks_R - t.all_R
    return t


def main() -> None:
    res = {}
    rng = np.random.default_rng(0)
    for sym, trig, log, tol in RUNS:
        ev = run(sym, trig, log, tol)
        res[sym] = ev
        p = ev[ev.ea]
        draws = np.array([rng.choice(ev.r.to_numpy(), len(p), replace=False).mean() for _ in range(5000)])
        held = ev.age_h >= 2
        print(f"\n=== {sym} 2018-02..2026-08: breaks {len(ev)}, EA picks matched {len(p)}")
        print(f"all breaks  win {100 * (ev.r > 0).mean():.1f}%  mean R {ev.r.mean():+.3f}")
        print(f"held >= 2h  win {100 * (ev[held].r > 0).mean():.1f}%  mean R {ev[held].r.mean():+.3f}")
        print(f"EA picks    win {100 * (p.r > 0).mean():.1f}%  mean R {p.r.mean():+.3f}   "
              f"P(random >= picks) = {(draws >= p.r.mean()).mean():.4f}")
        print(summary(ev).round(3).to_string())
    g, s = res["XAUUSD"], res["XAGUSD"]
    gp = g[g.ea]
    for frame, w, col in ((gp, 60, "gold_pick_60"), (g, 15, "gold_break_15")):
        ft, fs = frame.t.dt.tz_convert(None).to_numpy(), frame.side.to_numpy()
        s[col] = [bool(np.any((np.abs(ft - t) <= np.timedelta64(w, "m")) & (fs == d)))
                  for t, d in zip(s.t.dt.tz_convert(None).to_numpy(), s.side.to_numpy())]
    print("\nsilver: is the edge borrowed from gold?")
    for col in ("gold_pick_60", "gold_break_15"):
        for v in (True, False):
            a = s[s[col] == v]
            p = a[a.ea]
            print(f"  {col}={v!s:5}: silver breaks n={len(a):6d} R {a.r.mean():+.3f} | "
                  f"EA picks n={len(p):4d} win {100 * (p.r > 0).mean():5.1f}% R {p.r.mean():+.3f}")


if __name__ == "__main__":
    main()
