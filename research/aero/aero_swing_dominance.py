"""Does the Aero EA trade breaks of "dominant" swings?

User observation (2026-10-09): the EA often breaks a swing that is higher than the previous
swing and than all later swings.  For a swing that is still unbroken, every later swing is
lower by construction, so the observation has two testable parts:
  A  prev_higher -- the broken swing high is above the swing high before it (mirror: lows);
  B  has_later   -- at least one swing formed after it before the break (not always true).

Swings   Fractals of width n on M30 and H1 bars built from 5m (bar i's high strictly above
         the n bars before it and >= the n after; known when bar i+n closes).
Events   Every 5m bar that trades $0.50 through an unbroken swing, 02:30-23:00 server time.
         One event per swing (it is then broken); swings older than 20 days are dropped.
Label    The EA placed a limit at that level in the same or adjacent 5m bar (704 orders from
         the 2018-2026.08 tester log), restricted to moments the EA was free to act.
Outcome  The aero_replica_atr exits (aero_knn_filter.simulate), net R after $0.20/oz.

Run:  .venv/bin/python research/aero/aero_swing_dominance.py
      (needs research/output/aero_knn_events_5m.parquet from `aero_knn_filter.py 5m`)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.argv = [sys.argv[0], "5m"]          # aero_knn_filter reads its bar size from argv
import aero_approach_speed as aas  # noqa: E402
import aero_knn_filter as akf  # noqa: E402

OUT = akf.OUT
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
MAX_AGE = np.timedelta64(20, "D")
CONFIGS = [("30min", 1), ("30min", 2), ("1h", 1), ("1h", 2)]


def swings(b: pd.DataFrame, n: int) -> list[tuple]:
    """(confirm_time, kind, price, seq) for each fractal, kind +1 high / -1 low."""
    H, L, idx = b.high.to_numpy(), b.low.to_numpy(), b.index
    dur = idx[1] - idx[0]
    out = []
    for i in range(n, len(b) - n):
        conf = idx[i + n] + dur
        if all(H[i] > H[i - k] for k in range(1, n + 1)) and all(H[i] >= H[i + k] for k in range(1, n + 1)):
            out.append((conf.tz_convert(None).to_datetime64(), 1, H[i]))
        if all(L[i] < L[i - k] for k in range(1, n + 1)) and all(L[i] <= L[i + k] for k in range(1, n + 1)):
            out.append((conf.tz_convert(None).to_datetime64(), -1, L[i]))
    return sorted(out, key=lambda x: x[0])


def swing_events(m: pd.DataFrame, rule: str, n: int, atr: np.ndarray) -> pd.DataFrame:
    b = m.resample(rule).agg({"high": "max", "low": "min"}).dropna()
    sw = swings(b, n)
    o, h, lo, c = (m[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = m.index.tz_convert(None).to_numpy()
    mins = (m.srv.dt.hour * 60 + m.srv.dt.minute).to_numpy()
    active = {1: [], -1: []}          # [price, prev_same_kind_price, n_later, confirm_time]
    last = {1: None, -1: None}
    rows, k = [], 0
    for t in range(1, len(m)):
        while k < len(sw) and sw[k][0] <= idx[t]:
            conf, kind, px = sw[k]
            for a in active[kind]:
                a[2] += 1                  # a later swing of the same kind formed
            active[kind].append([px, last[kind], 0, conf])
            last[kind] = px
            k += 1
        tradable = 150 <= mins[t] < 23 * 60 and atr[t] == atr[t]
        for d in (1, -1):
            keep = []
            for a in active[d]:
                px = a[0]
                trig = px + d * akf.BREAK_USD
                if (h[t] >= trig) if d > 0 else (lo[t] <= trig):
                    if tradable:
                        fill = max(o[t], trig) if d > 0 else min(o[t], trig)
                        res = akf.simulate(t, d, px, fill, atr[t], o, h, lo, c, len(m))
                        if res is not None:
                            rows.append(dict(
                                t=m.index[t], side=d, level=px, atr=atr[t],
                                prev_higher=a[1] is not None and d * (px - a[1]) > 0,
                                has_later=a[2] > 0, n_later=a[2],
                                age_h=(idx[t] - a[3]) / np.timedelta64(1, "h"),
                                exit_time=m.index[res[0]],
                                net_r=(res[1] - akf.COST_USD) / atr[t]))
                elif idx[t] - a[3] < MAX_AGE:
                    keep.append(a)         # not broken by $0.50 yet; drop swings older than MAX_AGE
            active[d] = keep
    return pd.DataFrame(rows)


def label(ev: pd.DataFrame, orders: pd.DataFrame) -> np.ndarray:
    ea = np.zeros(len(ev), bool)
    key = ev.t.to_numpy()
    for t, side, px in orders.itertuples(index=False):
        bar = np.datetime64(t.floor("5min").tz_localize(None))
        lo_, hi_ = bar - np.timedelta64(5, "m"), bar + np.timedelta64(5, "m")
        cand = np.flatnonzero((key.astype("datetime64[ns]") >= lo_) & (key.astype("datetime64[ns]") <= hi_)
                              & (ev.side.to_numpy() == side) & (np.abs(ev.level.to_numpy() - px) < 1.5))
        if len(cand):
            ea[cand[0]] = True
    return ea


def main() -> None:
    m = akf.load()
    m = m[m.index >= pd.Timestamp("2017-12-01", tz="UTC")]
    h4 = m.resample("4h", on="srv").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    a = akf.wilder(akf.true_range(h4), 20)
    s = pd.Series(a.values, index=h4.index + pd.Timedelta("4h"))
    pos = np.searchsorted(s.index.values, m.srv.to_numpy(), side="right") - 1
    atr = np.where(pos >= 0, s.values[np.clip(pos, 0, None)], np.nan)

    orders, busy = aas.parse_log()
    starts = np.array([b[0].tz_localize(None) for b in busy], dtype="datetime64[ns]")
    ends = np.array([b[1].tz_localize(None) for b in busy], dtype="datetime64[ns]")
    day = pd.read_parquet(akf.OUT / "aero_knn_events_5m.parquet")
    day_lab = label(day.assign(t=day.t.dt.tz_localize(None)), orders)
    print(f"EA orders {len(orders)}; day-range break match {day_lab.sum()}")

    covered = {}
    for rule, n in CONFIGS:
        ev = swing_events(m, rule, n, atr)
        ev = ev[(ev.t >= "2018-01-01") & (ev.t < "2026-08-06")].reset_index(drop=True)
        ev["ea"] = label(ev.assign(t=ev.t.dt.tz_localize(None)), orders)
        tv = ev.t.dt.tz_localize(None).to_numpy()
        k = np.searchsorted(starts, tv, side="right") - 1
        inside = (k >= 0) & (tv > starts[np.clip(k, 0, None)]) & (tv < ends[np.clip(k, 0, None)])
        ev = ev[~inside | ev.ea].reset_index(drop=True)
        covered[(rule, n)] = ev
        print(f"\n=== swings {rule} n={n}: {len(ev)} breaks while EA free, EA matched {ev.ea.sum()} "
              f"of {len(orders)} orders ===")
        tab = ev.groupby(["prev_higher", "has_later"]).agg(
            breaks=("ea", "size"), ea_take=("ea", "mean"), ea_n=("ea", "sum"))
        tab["share_of_ea"] = tab.ea_n / ev.ea.sum()
        tab["share_of_breaks"] = tab.breaks / len(ev)
        is_ = ev.t < IS_END
        tab["R_IS"] = ev[is_].groupby(["prev_higher", "has_later"]).net_r.mean()
        tab["R_OOS"] = ev[~is_].groupby(["prev_higher", "has_later"]).net_r.mean()
        print(tab.round(3).to_string())
        ev.to_parquet(OUT / f"aero_swing_{rule}_{n}.parquet")


if __name__ == "__main__":
    main()
