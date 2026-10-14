"""Coverage and selectivity of the Aero EA's observable rules, alone and in every combination.

Universe  Every 5m break (by $0.50) of either the running broker-day high/low or an unbroken
          M30 fractal swing (n=1), 02:30-23:00 server, 2018-01-01..2026-08-06, while the EA
          was flat.  A day-extreme break and a swing break at the same level in the same bar
          are one event.
Rules     R1 day     the level is the current broker day's running high/low
          R2 dom     the level is a dominant swing, M30 or H1 (see Run): beyond the previous
                     swing, and a lower swing formed after it
          R3 held    the level had held >= 2h (time since the bar that printed it)
          R4 pull    price pulled back >= 0.40 x ATR from the level while it held
          R10 fast5  last closed 5m bar >= 0.08 x ATR from the level
          R11 fast1  close 1 minute before the trigger minute >= 0.03 x ATR from the level
For each rule set (all of its rules true):
          coverage    = EA orders meeting it / EA orders in the universe
          selectivity = EA orders meeting it / breaks meeting it

Run:  .venv/bin/python research/aero/aero_rule_table.py [30min_1|1h_1|1h_2]   (swing definition,
      default 30min_1; after the 5m/M1 research scripts)
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
SWING = sys.argv[1] if len(sys.argv) > 1 else "30min_1"
sys.argv = [sys.argv[0], "5m"]
import aero_approach_speed as aas  # noqa: E402
import aero_knn_filter as akf  # noqa: E402
import aero_m1_microstructure as amm  # noqa: E402

OUT = akf.OUT
START, END = pd.Timestamp("2018-01-01", tz="UTC"), pd.Timestamp("2026-08-06", tz="UTC")
RULES = {
    "R1 day": "is_day",
    "R2 dom": "dominant",
    "R3 held>=2h": "held",
    "R4 pull>=0.4ATR": "pull",
    "R10 fast5m": "fast5",
    "R11 fast1m": "fast1",
}


def union_events() -> pd.DataFrame:
    keep = ["t", "side", "level", "atr"]
    day = pd.read_parquet(OUT / "aero_knn_events_5m.parquet")[keep]
    sw = pd.read_parquet(OUT / f"aero_swing_{SWING}.parquet")[keep + ["prev_higher", "has_later"]]
    sw["dominant"] = sw.prev_higher & sw.has_later
    day, sw = (x[(x.t >= START) & (x.t < END)].reset_index(drop=True) for x in (day, sw))
    day["dominant"] = False
    day["src"] = "day"
    sw["src"] = "swing"
    # fold swing events that duplicate a day-extreme break (same bar, side, level within $0.5)
    key = day.set_index(["t", "side"])
    extra = []
    for r in sw.itertuples(index=False):
        if (r.t, r.side) in key.index:
            hit = day.index[(day.t == r.t) & (day.side == r.side) & ((day.level - r.level).abs() < 0.5)]
            if len(hit):
                day.loc[hit, "dominant"] |= bool(r.dominant)
                continue
        extra.append(r._asdict())
    ev = pd.concat([day, pd.DataFrame(extra)], ignore_index=True).sort_values("t").reset_index(drop=True)
    return ev[keep + ["dominant"]]


def level_context(ev: pd.DataFrame) -> pd.DataFrame:
    """is_day, level age (h) and pullback depth (ATR), uniformly from 5m bars."""
    m = akf.load()
    m = m[m.index >= START - pd.Timedelta(days=3)]
    h, lo = m.high.to_numpy(), m.low.to_numpy()
    sday = m.sday.to_numpy()
    pos = m.index.get_indexer(ev.t)
    is_day, age, pull = [], [], []
    for p, d, L, a in zip(pos, ev.side, ev.level, ev.atr):
        day0 = p
        while day0 > 0 and sday[day0 - 1] == sday[p]:
            day0 -= 1
        ext = h[day0:p].max() if d > 0 else lo[day0:p].min()
        is_day.append(p > day0 and abs(ext - L) < 0.05)
        back = h[max(p - 2880, 0):p] if d > 0 else lo[max(p - 2880, 0):p]   # up to 10 days
        at = np.flatnonzero(back >= L - 0.05) if d > 0 else np.flatnonzero(back <= L + 0.05)
        s = (max(p - 2880, 0) + at[-1]) if len(at) else max(p - 2880, 0)
        age.append((p - s) / 12)
        seg_lo, seg_hi = lo[s:p], h[s:p]
        pull.append(((L - seg_lo.min()) if d > 0 else (seg_hi.max() - L)) / a if p > s else 0.0)
    return ev.assign(is_day=is_day, age_h=age, pull_atr=pull)


def label_and_filter(ev: pd.DataFrame) -> pd.DataFrame:
    orders, busy = aas.parse_log()
    ev["ea"] = False
    tv = ev.t.dt.tz_localize(None).to_numpy()
    for t, side, px in orders.itertuples(index=False):
        bar = t.floor("5min").tz_localize(None).to_datetime64()
        near = np.flatnonzero((tv >= bar - np.timedelta64(5, "m")) & (tv <= bar + np.timedelta64(5, "m"))
                              & (ev.side.to_numpy() == side))
        if len(near):
            dist = np.abs(ev.level.to_numpy()[near] - px)
            if dist.min() < 1.5:
                ev.loc[near[dist.argmin()], "ea"] = True
    starts = np.array([b[0].tz_localize(None) for b in busy], dtype="datetime64[ns]")
    ends = np.array([b[1].tz_localize(None) for b in busy], dtype="datetime64[ns]")
    tn = ev.t.dt.tz_localize(None).to_numpy()
    k = np.searchsorted(starts, tn, side="right") - 1
    inside = (k >= 0) & (tn > starts[np.clip(k, 0, None)]) & (tn < ends[np.clip(k, 0, None)])
    print(f"EA orders {len(orders)}; matched to a break in the universe {ev.ea.sum()}")
    return ev[~inside | ev.ea].reset_index(drop=True)


def main() -> None:
    ev = level_context(union_events())
    ev = label_and_filter(ev)
    ev = aas.add_features(ev)
    ev = amm.m1_features(ev)
    ev["held"] = ev.age_h >= 2
    ev["pull"] = ev.pull_atr >= 0.40
    ev["fast5"] = ev.gap_now >= 0.08
    ev["fast1"] = ev.a1 >= 0.03
    ev = ev.dropna(subset=["a1", "gap_now"]).reset_index(drop=True)
    n_ea = int(ev.ea.sum())
    print(f"universe {len(ev)} breaks (EA flat), EA orders in it {n_ea}\n")
    rows = []
    names = list(RULES)
    for r in range(1, len(names) + 1):
        for combo in itertools.combinations(names, r):
            mask = np.logical_and.reduce([ev[RULES[c]].to_numpy() for c in combo])
            hits = int(mask.sum())
            ea = int(ev.ea[mask].sum())
            rows.append(dict(rules=" + ".join(c.split()[0] for c in combo), k=r, breaks=hits,
                             ea_orders=ea, coverage=100 * ea / n_ea,
                             selectivity=100 * ea / hits if hits else np.nan))
    t = pd.DataFrame(rows)
    base = 100 * n_ea / len(ev)
    print(f"baseline selectivity (all breaks): {base:.2f}%")
    t.to_csv(OUT / f"aero_rule_table_{SWING}.csv", index=False)
    pd.set_option("display.max_rows", 100)
    print(t.round(2).to_string(index=False))
    ev.to_parquet(OUT / f"aero_rule_table_{SWING}.parquet")


if __name__ == "__main__":
    main()
