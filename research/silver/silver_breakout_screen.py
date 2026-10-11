"""Silver trend-breakout screen at swing / daily scale (idea a203e796).

Standard, untuned systems on XAGUSD, both sides, one position at a time:

  F   1h fractal swing system: swing = 1h bar whose high (low) beats the 2 bars each side,
      known 2 bars after it (no look-ahead). Long when price trades through the latest
      confirmed swing high; stop at the latest confirmed swing low, trailed up to each newer
      confirmed swing low; exit only on the stop (multi-day holds allowed). Mirror for shorts;
      a stop at the same level as an opposite entry reverses.
  D1  Turtle System 1: stop entry at the 20-day high/low, exit on the 10-day opposite extreme,
      initial stop 2 x ATR(20) daily.
  D2  Turtle System 2: 55-day entry, 20-day exit, 2 x ATR(20) initial stop.

Daily bars are broker days (17:00 New York). Fills: stop orders at the level or the open if
it gapped through; on any bar touching both the stop and something else, the stop is assumed
first (ADR-0004 spirit).
Costs: spread $0.012 (2018-22) / $0.018 (2023-25) per round trip (measured, FINDINGS in
research/tokyo/), no commission; swap per night held: long pays FedFunds + 3%/yr, short
receives FedFunds - 2%/yr (pays when negative) -- calibrated to Pepperstone's current
-1.58 / +0.49 points/day on silver.
R = net P&L / initial risk (entry to initial stop).

Choose on 2018-22 by net mean R (>= 30 trades); 2023-25 looked at once for the chosen system:
    .venv/bin/python research/silver/silver_breakout_screen.py            # 2018-22
    .venv/bin/python research/silver/silver_breakout_screen.py D1         # + 2023-25 for D1
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BARS = ROOT / ".hermes_cache/bars/pepperstone"
OUT = ROOT / "research/output"
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
END = pd.Timestamp("2026-01-01", tz="UTC")
FED_FUNDS = {2018: 1.8, 2019: 2.2, 2020: 0.4, 2021: 0.1, 2022: 1.7, 2023: 5.0, 2024: 5.1, 2025: 4.2}


def load_1m(sym: str) -> pd.DataFrame:
    m = pd.read_parquet(BARS / f"{sym}_1m.parquet")
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    m = m[~m.index.duplicated()].sort_index()
    return m[m.index < END][["open", "high", "low", "close"]]


def agg(m: pd.DataFrame, key) -> pd.DataFrame:
    g = m.groupby(key)
    return pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(),
                         "close": g.close.last(), "t": g.apply(lambda x: x.index[0])}).set_index("t")


def daily(m: pd.DataFrame) -> pd.DataFrame:
    ny = m.index.tz_convert("America/New_York")
    key = (ny + pd.Timedelta(hours=7)).date          # 17:00 NY -> next broker day
    d = agg(m, key)
    return d[d.index.tz_convert("America/New_York").map(lambda t: (t + pd.Timedelta(hours=7)).weekday()) < 5]


def hourly(m: pd.DataFrame) -> pd.DataFrame:
    return agg(m, m.index.floor("1h"))


def spread(t: pd.Timestamp) -> float:
    return 0.012 if t < IS_END else 0.018


def swap(side: int, entry_t: pd.Timestamp, exit_t: pd.Timestamp, price: float) -> float:
    """Cash cost per oz of the nights held (positive = paid)."""
    nights = pd.date_range(entry_t.normalize(), exit_t.normalize(), freq="D", inclusive="left")
    total = 0.0
    for n in nights:
        ff = FED_FUNDS.get(n.year, 4.0) / 100
        rate = ff + 0.03 if side > 0 else -(ff - 0.02)
        total += rate / 365 * price
    return total


def trade(side, t0, e, stop0, t1, x) -> dict:
    gross = side * (x - e)
    net = gross - spread(t0) - swap(side, t0, t1, e)
    risk = abs(e - stop0)
    return dict(t=t0, side=side, R=net / risk, G=gross / risk, days=(t1 - t0).total_seconds() / 86400,
                ret=net / e)


def donchian(d: pd.DataFrame, n_in: int, n_out: int) -> list[dict]:
    o, h, lo, c = (d[k].to_numpy() for k in ("open", "high", "low", "close"))
    tr = np.maximum(h[1:] - lo[1:], np.maximum(abs(h[1:] - c[:-1]), abs(lo[1:] - c[:-1])))
    atr = pd.Series(np.r_[np.nan, tr]).ewm(alpha=1 / 20, adjust=False, min_periods=20).mean().to_numpy()
    hi_in = pd.Series(h).rolling(n_in).max().shift(1).to_numpy()
    lo_in = pd.Series(lo).rolling(n_in).min().shift(1).to_numpy()
    hi_out = pd.Series(h).rolling(n_out).max().shift(1).to_numpy()
    lo_out = pd.Series(lo).rolling(n_out).min().shift(1).to_numpy()
    ts, rows, pos = d.index, [], None
    for i in range(n_in + 1, len(d)):
        if pos is not None:
            side, t0, e, stop0, stop = pos
            lvl = max(stop, lo_out[i]) if side > 0 else min(stop, hi_out[i])
            if (lo[i] <= lvl) if side > 0 else (h[i] >= lvl):
                x = min(o[i], lvl) if side > 0 else max(o[i], lvl)
                rows.append(trade(side, t0, e, stop0, ts[i], x))
                pos = None
            continue
        a = atr[i - 1]
        if np.isnan(a):
            continue
        for side, lvl in ((1, hi_in[i]), (-1, lo_in[i])):
            if (h[i] >= lvl) if side > 0 else (lo[i] <= lvl):
                e = max(o[i], lvl) if side > 0 else min(o[i], lvl)
                stop = e - side * 2 * a
                if (lo[i] <= stop) if side > 0 else (h[i] >= stop):      # stopped on the entry day
                    rows.append(trade(side, ts[i], e, stop, ts[i], stop))
                else:
                    pos = (side, ts[i], e, stop, stop)
                break
    return rows


def fractal(hb: pd.DataFrame, n: int = 2) -> list[dict]:
    o, h, lo = (hb[k].to_numpy() for k in ("open", "high", "low"))
    ts, rows = hb.index, []
    sh = sl = None                  # latest confirmed swing high / low
    pos = None                      # (side, t0, entry, stop0, stop)
    for i in range(2 * n, len(hb)):
        k = i - n                   # candidate swing bar; its n right-hand bars end with bar i
        w = slice(k - n, k + n + 1)
        if h[k] == h[w].max() and (h[w] < h[k]).sum() == 2 * n:
            sh_new = h[k]
        else:
            sh_new = None
        if lo[k] == lo[w].min() and (lo[w] > lo[k]).sum() == 2 * n:
            sl_new = lo[k]
        else:
            sl_new = None
        # bar i trades against swings confirmed up to bar i-1; bar k's swing is registered below
        if pos is not None:
            side, t0, e, stop0, stop = pos
            if (lo[i] <= stop) if side > 0 else (h[i] >= stop):
                x = min(o[i], stop) if side > 0 else max(o[i], stop)
                rows.append(trade(side, t0, e, stop0, ts[i], x))
                pos = None
        if pos is None and sh is not None and sl is not None and sl < sh:
            for side, lvl, st in ((1, sh, sl), (-1, sl, sh)):
                if (h[i] >= lvl) if side > 0 else (lo[i] <= lvl):
                    e = max(o[i], lvl) if side > 0 else min(o[i], lvl)
                    if (st < e) if side > 0 else (st > e):
                        pos = (side, ts[i], e, st, st)
                    break
        # register swings confirmed by this bar's close for use from the next bar
        if sh_new is not None:
            sh = sh_new
        if sl_new is not None:
            sl = sl_new
            if pos is not None and pos[0] > 0 and sl > pos[4]:
                pos = (*pos[:4], sl)
        if sh_new is not None and pos is not None and pos[0] < 0 and sh_new < pos[4]:
            pos = (*pos[:4], sh_new)
    return rows


def summary(name: str, ev: pd.DataFrame, oos: bool) -> dict:
    is_ = ev.t < IS_END
    r = dict(system=name, n_is=int(is_.sum()), R_is=ev[is_].R.mean(), gross_is=ev[is_].G.mean(),
             pf_is=ev[is_].R.clip(lower=0).sum() / -ev[is_].R.clip(upper=0).sum(),
             days_is=ev[is_].days.median(),
             yrs_pos_is=f"{int((ev[is_].groupby(ev[is_].t.dt.year).R.sum() > 0).sum())}/5")
    if oos:
        o = ev[~is_]
        r |= dict(n_oos=len(o), R_oos=o.R.mean(), gross_oos=o.G.mean(),
                  pf_oos=o.R.clip(lower=0).sum() / -o.R.clip(upper=0).sum(),
                  by_year=o.groupby(o.t.dt.year).R.sum().round(2).to_dict())
    return r


def main() -> None:
    pick = sys.argv[1] if len(sys.argv) > 1 else None
    m = load_1m("XAGUSD")
    d, hb = daily(m), hourly(m)
    systems = {"F": fractal(hb), "D1": donchian(d, 20, 10), "D2": donchian(d, 55, 20)}
    out = []
    for name, rows in systems.items():
        ev = pd.DataFrame(rows)
        ev.to_parquet(OUT / f"silver_breakout_{name}.parquet")
        out.append(summary(name, ev, oos=(name == pick)))
        s = ev[ev.t < IS_END]
        out[-1]["long_R_is"] = s[s.side > 0].R.sum()
        out[-1]["short_R_is"] = s[s.side < 0].R.sum()
    pd.set_option("display.width", 220)
    print("XAGUSD, net R per trade after spread + swap; 2018-22" + (f"; 2023-25 for {pick}" if pick else ""))
    print(pd.DataFrame(out).set_index("system").round(3).to_string())


if __name__ == "__main__":
    main()
