"""Does the Aero EA pick day-range breaks by how fast price approached the level?

Universe  The 5m day-range break events of aero_knn_filter.py (run it with `5m` first),
          restricted to moments the EA could have acted: not inside one of its own trade
          episodes or pending orders (from the 2018-2026.08 MT5 log).
Label     EA-picked = the EA placed a limit at that level within the same 5m bar (all 704
          orders, filled or not -- the order is the selection, the fill is chance).
Features  Approach speed, all known at the trigger (closed bars, plus the breaking 5m bar's
          open-to-trigger move, which has happened by the time the trigger prints):
            m30_1/2/4    distance from the level 1/2/4 closed M30 bars ago, in ATR(4h,20)
                         (large = price came from far away quickly)
            gap_now      distance from the level at the last closed 5m bar
            intrabar     breaking 5m bar's open -> trigger, in ATR
            m30_range    last closed M30 bar's high-low, in ATR
            since_far    5m bars since a close was >= 0.5 ATR from the level (approach time)
            eff_1h       net/gross 5m path over the last hour (straightness)
Questions (1) Do EA picks differ on these features (AUC vs skipped)?
          (2) Does any feature predict net R, and does the sign hold IS (2018-22) and OOS
              (2023-26)?

Run:  .venv/bin/python research/aero/aero_approach_speed.py
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "research" / "output"
EVENTS = OUT / "aero_knn_events_5m.parquet"
BARS = ROOT / ".hermes_cache" / "bars" / "pepperstone" / "XAUUSD_5m.parquet"
LOGS = Path(__file__).resolve().parent / "logs"
LOG = LOGS / "20261009 copy.log"
FEATS = ["m30_1", "m30_2", "m30_4", "gap_now", "intrabar", "m30_range", "since_far", "eff_1h"]
IS_END = pd.Timestamp("2023-01-01", tz="UTC")


def server_to_utc(ts: pd.Timestamp) -> pd.Timestamp:
    """MT5 server = UTC+2, UTC+3 during EU DST."""
    y = ts.year
    a = max(pd.date_range(f"{y}-03-01", f"{y}-03-31", freq="W-SUN")) + pd.Timedelta(hours=3)
    b = max(pd.date_range(f"{y}-10-01", f"{y}-10-31", freq="W-SUN")) + pd.Timedelta(hours=3)
    return (ts - pd.Timedelta(hours=3 if a <= ts < b else 2)).tz_localize("UTC")


def parse_log() -> tuple[pd.DataFrame, list[tuple[pd.Timestamp, pd.Timestamp]]]:
    """EA limit orders (side, level, time) and the intervals it was busy."""
    text = LOG.read_bytes().decode("utf-16")
    text = text[text.index("from 2018.01.01"):]
    orders, busy, open_since, pos = [], [], None, 0.0
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 5:
            continue
        m = re.match(r"(\d{4}\.\d\d\.\d\d \d\d:\d\d:\d\d)\s+(.*)", p[4])
        if not m:
            continue
        ts = pd.Timestamp(m.group(1).replace(".", "-", 2))
        msg = m.group(2)
        o = re.match(r"(buy|sell) limit [\d.]+ XAUUSD at ([\d.]+) sl:", msg)
        if p[3] == "Trade" and o:
            orders.append((server_to_utc(ts), 1 if o.group(1) == "buy" else -1, float(o.group(2))))
            if open_since is None:
                open_since = ts
        d = re.match(r"deal #\d+ (buy|sell) ([\d.]+) XAUUSD", msg)
        if p[3] == "Trades" and d:
            q = float(d.group(2)) * (1 if d.group(1) == "buy" else -1)
            was_flat = abs(pos) < 1e-9
            pos += q
            if was_flat and open_since is None:
                open_since = ts
            if abs(pos) < 1e-9 and open_since is not None:
                busy.append((server_to_utc(open_since), server_to_utc(ts)))
                open_since = None
        c = re.match(r"order canceled \[#\d+ (buy|sell) limit", msg)
        if p[3] == "Trade" and c and abs(pos) < 1e-9 and open_since is not None:
            busy.append((server_to_utc(open_since), server_to_utc(ts)))
            open_since = None
    return pd.DataFrame(orders, columns=["t", "side", "px"]), busy


def add_features(ev: pd.DataFrame) -> pd.DataFrame:
    m = pd.read_parquet(BARS)
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    o, h, lo, c = (m[k].to_numpy() for k in ("open", "high", "low", "close"))
    pos = m.index.get_indexer(ev.t)
    m30 = m.resample("30min").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    m30_end = m30.index + pd.Timedelta("30min")
    j = np.searchsorted(m30_end.values, ev.t.values, side="right") - 1   # last closed M30
    d, lvl, atr = ev.side.to_numpy(), ev.level.to_numpy(), ev.atr.to_numpy()
    mc, mh, ml = m30.close.to_numpy(), m30.high.to_numpy(), m30.low.to_numpy()
    f = {}
    for k in (1, 2, 4):
        f[f"m30_{k}"] = d * (lvl - mc[np.clip(j - k + 1, 0, None)]) / atr
    f["m30_range"] = (mh[j] - ml[j]) / atr
    f["gap_now"] = d * (lvl - c[pos - 1]) / atr
    trig = lvl + d * 0.5
    f["intrabar"] = d * (trig - o[pos]) / atr
    since, eff = np.empty(len(ev)), np.empty(len(ev))
    for i, (p, dd, L, a) in enumerate(zip(pos, d, lvl, atr)):
        w = c[max(p - 288, 0):p][::-1]
        far = np.flatnonzero(dd * (L - w) >= 0.5 * a)
        since[i] = far[0] + 1 if len(far) else 288
        seg = c[max(p - 13, 0):p]
        gross = np.abs(np.diff(seg)).sum()
        eff[i] = abs(seg[-1] - seg[0]) / gross if gross else 0.0
    f["since_far"], f["eff_1h"] = since, eff
    return ev.assign(**f)


def auc(x: np.ndarray, y: np.ndarray) -> float:
    """P(score of a positive > score of a negative), ties half."""
    r = pd.Series(x).rank().to_numpy()
    n1 = y.sum()
    n0 = len(y) - n1
    return (r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def main() -> None:
    ev = pd.read_parquet(EVENTS).sort_values("t").reset_index(drop=True)
    orders, busy = parse_log()
    ev = ev[(ev.t >= "2018-01-01") & (ev.t < "2026-08-06")].reset_index(drop=True)

    # label: an EA limit at this level placed inside this 5m bar
    ev["ea"] = False
    for t, side, px in orders.itertuples(index=False):
        bar = t.floor("5min")
        hit = ev.index[(ev.t == bar) & (ev.side == side) & ((ev.level - px).abs() < 1.5)]
        if len(hit):
            ev.loc[hit[0], "ea"] = True
    # drop events while the EA was already in a trade / had a working order
    starts = np.array([b[0] for b in busy], dtype="datetime64[ns]")
    ends = np.array([b[1] for b in busy], dtype="datetime64[ns]")
    tv = ev.t.dt.tz_localize(None).to_numpy()
    k = np.searchsorted(starts, tv, side="right") - 1
    inside = (k >= 0) & (tv < ends[np.clip(k, 0, None)]) & (tv > starts[np.clip(k, 0, None)])
    ev = ev[~inside | ev.ea].reset_index(drop=True)
    ev = add_features(ev)
    print(f"EA orders {len(orders)}; matched to a 5m break {ev.ea.sum()}; "
          f"universe (EA free to act) {len(ev)} breaks, take rate {ev.ea.mean():.3f}\n")

    y = ev.ea.to_numpy()
    rows = []
    for f in FEATS:
        x = ev[f].to_numpy()
        rows.append(dict(feature=f, ea_median=np.median(x[y]), skipped_median=np.median(x[~y]),
                         auc_ea=auc(x, y)))
    print("(1) EA picks vs skipped (AUC 0.5 = no difference):")
    print(pd.DataFrame(rows).round(3).to_string(index=False))

    print("\n(2) feature quintile -> mean net R, IS 2018-22 | OOS 2023-26 (quintile edges from IS):")
    is_ = ev.t < IS_END
    for f in FEATS:
        edges = np.unique(np.quantile(ev.loc[is_, f], [0, .2, .4, .6, .8, 1]))
        edges[0], edges[-1] = -np.inf, np.inf
        q = pd.cut(ev[f], edges, labels=False)
        tab = pd.DataFrame({"IS": ev[is_].groupby(q[is_]).net_r.mean(),
                            "OOS": ev[~is_].groupby(q[~is_]).net_r.mean(),
                            "EA_take": ev.groupby(q).ea.mean()}).round(3)
        print(f"\n{f}  (IS edges {np.round(edges[1:-1], 2).tolist()})")
        print(tab.T.to_string())
    ev.to_parquet(OUT / "aero_approach_speed.parquet")


if __name__ == "__main__":
    main()
