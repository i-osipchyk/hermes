"""Tokyo-range breakout on gold (and silver) — event study (idea 0945d084).

Range     high/low of 00:00-06:00 UTC 1m bars (>= 300 bars), Monday-Friday.
Break     the first minute in 06:00-20:00 UTC whose high (low) trades `trig` beyond the range
          high (low); each side is its own event (a day can give two).  trig = 0.02 x ATR.
Entry     stop fill at the trigger (or the minute's open if it gapped through).
Exits     all intraday, forced exit at the close of 20:59 UTC, evaluated minute by minute with the
          stop checked before the target (conservative):
            G1  EA-like: lock at +0.32 ATR -> stop +0.27, trail 0.054 behind in 0.0095 steps,
                TP +0.54, at 3h if under water stop -0.5 ATR, catastrophe -3.6 ATR
            G2a range: stop at the opposite range edge, target 1R  (R = entry-to-stop distance)
            G2b range: same stop, target 2R
            G3a bracket: stop 0.5 ATR, target 0.5 ATR      G3b: stop 0.5, target 1.0 ATR
            G4  hold: stop 1 ATR, exit at 20:59 UTC
Units     result in ATR(4h,20) after costs ($0.20/oz gold, $0.025/oz silver), so years compare.
Features  (for the filter step) range width / ATR, break hour, a1 = distance from the level 1 min
          before the trigger, room = distance to the previous day's extreme beyond the level,
          minutes since 06:00, whether the other side already broke today.

Run:  .venv/bin/python research/tokyo/tokyo_breakout_study.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aero"))
sys.argv = [sys.argv[0], "5m"]
import aero_knn_filter as akf  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = akf.OUT
BARS = ROOT / ".hermes_cache/bars/pepperstone"
COST = {"XAUUSD": 0.20, "XAGUSD": 0.025}
GEOS = ["G1", "G2a", "G2b", "G3a", "G3b", "G4"]
IS_END = pd.Timestamp("2023-01-01", tz="UTC")


def load(sym: str) -> pd.DataFrame:
    m = pd.read_parquet(BARS / f"{sym}_1m.parquet")
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    m = m[(m.index >= "2017-11-01") & (m.index < "2026-08-07")]
    h4 = m.resample("4h").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    s = pd.Series(akf.wilder(akf.true_range(h4), 20).values, index=h4.index + pd.Timedelta("4h"))
    m["atr"] = s.reindex(m.index, method="ffill").to_numpy()
    return m


def run_exit(geo: str, d: int, entry: float, a: float, rng_stop: float,
             h: np.ndarray, lo: np.ndarray, o: np.ndarray, c: np.ndarray, f: int, last: int) -> float:
    """Exit price for one trade, minutes f+1..last (last = the 20:59 UTC minute)."""
    if geo == "G1":
        tp, stop, checked = entry + d * 0.54 * a, entry - d * 3.6 * a, False
    elif geo in ("G2a", "G2b"):
        risk = abs(entry - rng_stop)
        stop, tp = rng_stop, entry + d * (1 if geo == "G2a" else 2) * risk
    elif geo == "G3a":
        stop, tp = entry - d * 0.5 * a, entry + d * 0.5 * a
    elif geo == "G3b":
        stop, tp = entry - d * 0.5 * a, entry + d * 1.0 * a
    else:
        stop, tp = entry - d * 1.0 * a, None
    for q in range(f + 1, last + 1):
        if (lo[q] <= stop) if d > 0 else (h[q] >= stop):
            return min(stop, o[q]) if d > 0 else max(stop, o[q])
        if tp is not None and ((h[q] >= tp) if d > 0 else (lo[q] <= tp)):
            return max(tp, o[q]) if d > 0 else min(tp, o[q])
        if geo == "G1":
            best = d * ((h[q] if d > 0 else lo[q]) - entry) / a
            if best >= 0.32:
                steps = max(np.floor((best - 0.054 - 0.27) / 0.0095), 0)
                new = entry + d * (0.27 + steps * 0.0095) * a
                stop = max(stop, new) if d > 0 else min(stop, new)
            if not checked and q - f >= 180:
                checked = True
                if d * (c[q] - entry) < 0:
                    g = entry - d * 0.5 * a
                    if d * (c[q] - g) <= 0:
                        return c[q]
                    stop = max(stop, g) if d > 0 else min(stop, g)
    return c[last]


def events(sym: str) -> pd.DataFrame:
    cache = OUT / f"tokyo_events_{sym}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    m = load(sym)
    o, h, lo, c, atr = (m[k].to_numpy(float) for k in ("open", "high", "low", "close", "atr"))
    ts = m.index
    day = ts.normalize()
    mins = (ts.hour * 60 + ts.minute).to_numpy()
    dd = m.groupby(day).agg(h=("high", "max"), l=("low", "min"))
    rows = []
    for dte, idx in pd.Series(np.arange(len(m)), index=day).groupby(level=0):
        if dte.weekday() >= 5:
            continue
        ii = idx.to_numpy()
        tok = ii[mins[ii] < 360]
        win = ii[(mins[ii] >= 360) & (mins[ii] < 1200)]
        lastm = ii[mins[ii] <= 1259]
        if len(tok) < 300 or len(win) < 60 or np.isnan(atr[win[0]]):
            continue
        last = lastm[-1]
        hi, lw = h[tok].max(), lo[tok].min()
        prev = dd.index[dd.index < dte]
        pdh, pdl = (dd.h[prev[-1]], dd.l[prev[-1]]) if len(prev) else (np.nan, np.nan)
        broke = {}
        for d, L, opp in ((1, hi, lw), (-1, lw, hi)):
            a = atr[win[0]]
            trig = L + d * 0.02 * a
            hit = win[(h[win] >= trig) if d > 0 else (lo[win] <= trig)]
            if not len(hit):
                continue
            j = hit[0]
            entry = max(o[j], trig) if d > 0 else min(o[j], trig)
            r = {"t": ts[j], "date": dte, "side": d, "level": L, "atr": a, "entry": entry,
                 "range_atr": (hi - lw) / a, "hour": mins[j] / 60, "since_06": mins[j] - 360,
                 "a1": d * (L - c[j - 1]) / a,
                 "room_pd": d * ((pdh if d > 0 else pdl) - L) / a,
                 "gap_through": int((o[j] - trig) * d > 0)}
            for g in GEOS:
                px = run_exit(g, d, entry, a, opp, h, lo, o, c, j, last)
                r[g] = (d * (px - entry) - COST[sym]) / a
            broke[d] = j
            rows.append(r)
        if len(broke) == 2:
            first = min(broke, key=broke.get)
            for r in rows[-2:]:
                r["second_side"] = int(r["side"] != first)
        elif rows and rows[-1]["date"] == dte:
            rows[-1]["second_side"] = 0
    ev = pd.DataFrame(rows)
    ev.to_parquet(cache)
    return ev


def table(ev: pd.DataFrame) -> pd.DataFrame:
    g = ev.groupby(ev.t.dt.year)
    out = pd.DataFrame({"n": g.size()})
    for geo in GEOS:
        out[geo] = g[geo].mean()
    return out


def main() -> None:
    for sym in ("XAUUSD", "XAGUSD"):
        ev = events(sym)
        is_ = ev.t < IS_END
        print(f"\n=== {sym}: {len(ev)} breaks on {ev.date.nunique()} days; both sides broke on "
              f"{100 * (ev.groupby('date').size() == 2).mean():.0f}% of break days; "
              f"range median {ev.range_atr.median():.2f} ATR")
        s = pd.DataFrame({"2018-22 mean R": ev[is_][GEOS].mean(), "2023-26 mean R": ev[~is_][GEOS].mean(),
                          "2018-22 win%": 100 * (ev[is_][GEOS] > 0).mean(),
                          "2023-26 win%": 100 * (ev[~is_][GEOS] > 0).mean()})
        print(s.round(3).T.to_string())
        print("by year (mean R per trade):")
        print(table(ev).round(3).to_string())


if __name__ == "__main__":
    main()
