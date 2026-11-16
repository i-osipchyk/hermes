"""London range -> NY open on gold: event study (idea c706aa04). In-sample 2018-2022 only.

Range     high/low of XAUUSD 5m bars from 08:00 Europe/London to 08:30 America/New_York
          (local zones, so DST does not move either edge), weekdays, >= 40 bars.
NY open   the 08:30 ET bar (US data, COMEX). Flat at the open of the 16:45 ET bar.
Units     ATR(4h,20) known before entry; cost $0.20/oz round trip.

Part 1  magnet test: P(touch London high/low after 08:30 ET) vs the same distances borrowed
        from a random other day (shuffled-level control). If the edges are magnets, they are
        touched more often than generic levels at the same distance.
Part 2  direction rules, all exiting at the London edge as target:
          A_far   at 08:30 go toward the farther edge; stop = same distance (1:1)
          A_near  toward the nearer edge, 1:1 (the mirror; A_far + A_near ~ a random direction)
          B_fade  08:30-11:30 ET: a 5m bar trades beyond one edge and closes back inside ->
                  next open, toward the opposite edge; stop = sweep extreme + 0.05 ATR
          C_first direction of the 08:30-09:00 ET move; enter 09:00 toward that edge, 1:1;
                  skip if already beyond it
        Stop is checked before the target on the same bar (conservative, ADR-0004).

Run:  .venv/bin/python research/london_range/london_range_study.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BARS = ROOT / ".hermes_cache/bars/pepperstone/XAUUSD_5m.parquet"
OUT = ROOT / "research/output/london_range"
COST = 0.20
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
LDN, NY = "Europe/London", "America/New_York"


def wilder(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(alpha=1 / n, adjust=False).mean()


def load() -> pd.DataFrame:
    m = pd.read_parquet(BARS)
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    m = m[m.index < IS_END]
    h4 = m.resample("4h").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    pc = h4.close.shift()
    tr = pd.concat([h4.high - h4.low, (h4.high - pc).abs(), (h4.low - pc).abs()], axis=1).max(axis=1)
    atr = pd.Series(wilder(tr, 20).values, index=h4.index + pd.Timedelta("4h"))
    m["atr"] = atr.reindex(m.index, method="ffill").to_numpy()
    m["ny"] = m.index.tz_convert(NY)
    m["ldn"] = m.index.tz_convert(LDN)
    return m


def walk(d: int, entry: float, stop: float, tp: float, h, lo, o, c, f: int, last: int):
    """Exit price, reason for a trade entered at bar f's open; bars f..last-1, flat at last open."""
    for q in range(f, last):
        if (lo[q] <= stop) if d > 0 else (h[q] >= stop):
            return (min(stop, o[q]) if d > 0 else max(stop, o[q])), "stop"
        if (h[q] >= tp) if d > 0 else (lo[q] <= tp):
            return (max(tp, o[q]) if d > 0 else min(tp, o[q])) if q > f else tp, "target"
    return o[last], "time"


def main() -> None:
    m = load()
    o, h, lo, c = (m[k].to_numpy() for k in ("open", "high", "low", "close"))
    nymin = (m.ny.dt.hour * 60 + m.ny.dt.minute).to_numpy()
    ldnmin = (m.ldn.dt.hour * 60 + m.ldn.dt.minute).to_numpy()
    nyday = m.ny.dt.date.to_numpy()
    rows, trades = [], []
    idx = pd.Series(np.arange(len(m))).groupby(nyday)
    for day, ii in idx:
        ii = ii.to_numpy()
        if pd.Timestamp(day).weekday() >= 5:
            continue
        rng = ii[(ldnmin[ii] >= 8 * 60) & (nymin[ii] < 8 * 60 + 30) & (m.ldn.dt.date.to_numpy()[ii] == day)]
        start = ii[nymin[ii] == 8 * 60 + 30]
        end = ii[nymin[ii] == 16 * 60 + 45]
        if len(rng) < 40 or not len(start) or not len(end):
            continue
        f, last = start[0], end[0]
        H, L, a, p0 = h[rng].max(), lo[rng].min(), m.atr.iat[f], o[f]
        if not np.isfinite(a) or not (L < p0 < H):
            continue
        after_h, after_l = h[f:last].max(), lo[f:last].min()
        rows.append(dict(day=day, H=H, L=L, p0=p0, atr=a, up=(H - p0) / a, dn=(p0 - L) / a,
                         touch_h=after_h >= H, touch_l=after_l <= L,
                         mfe_up=(after_h - p0) / a, mfe_dn=(p0 - after_l) / a, f=f, last=last))

        def rec(rule, d, fi, entry, stop, tp, day=day, a=a, last=last):
            px, why = walk(d, entry, stop, tp, h, lo, o, c, fi, last)
            trades.append(dict(day=day, rule=rule, d=d, r=(d * (px - entry) - COST) / a,
                               R=(d * (px - entry) - COST) / abs(entry - stop), why=why,
                               tgt=abs(tp - entry) / a, hour=int(nymin[fi] // 60)))

        far = 1 if H - p0 > p0 - L else -1
        for rule, d in (("A_far", far), ("A_near", -far)):
            tp = H if d > 0 else L
            rec(rule, d, f, p0, p0 - (tp - p0), tp)
        # B: sweep then close back inside
        for q in range(f, last - 1):
            if nymin[q] >= 11 * 60 + 30:
                break
            if h[q] > H and c[q] < H:
                e = o[q + 1]
                if L < e < H:
                    rec("B_fade", -1, q + 1, e, h[q] + 0.05 * a, L)
                break
            if lo[q] < L and c[q] > L:
                e = o[q + 1]
                if L < e < H:
                    rec("B_fade", 1, q + 1, e, lo[q] - 0.05 * a, H)
                break
        # C: first half hour direction
        g = ii[nymin[ii] == 9 * 60]
        if len(g):
            g = g[0]
            e = o[g]
            d = 1 if e > p0 else -1
            tp = H if d > 0 else L
            if (tp - e) * d > 0:
                rec("C_first", d, g, e, e - (tp - e), tp)

    ev = pd.DataFrame(rows)
    tr = pd.DataFrame(trades)
    OUT.mkdir(parents=True, exist_ok=True)
    ev.to_csv(OUT / "events_is.csv", index=False)
    tr.to_csv(OUT / "trades_is.csv", index=False)

    # Part 1: magnet vs shuffled distances
    rng = np.random.default_rng(7)
    ctl_h, ctl_l = [], []
    for _ in range(20):
        perm = rng.permutation(len(ev))
        for k, row in enumerate(ev.itertuples()):
            j = ev.iloc[perm[k]]
            ctl_h.append(row.mfe_up >= j.up)
            ctl_l.append(row.mfe_dn >= j.dn)
    print(f"days {len(ev)}  median range {((ev.H-ev.L)/ev.atr).median():.2f} ATR, "
          f"p0 to high {ev.up.median():.2f}, to low {ev.dn.median():.2f} ATR")
    print(f"touch London high {ev.touch_h.mean():.3f} vs shuffled {np.mean(ctl_h):.3f}")
    print(f"touch London low  {ev.touch_l.mean():.3f} vs shuffled {np.mean(ctl_l):.3f}")
    print(f"touch either {(ev.touch_h|ev.touch_l).mean():.3f}  both {(ev.touch_h&ev.touch_l).mean():.3f}")
    near_up = ev.up < ev.dn
    print(f"touch nearer edge {np.where(near_up, ev.touch_h, ev.touch_l).mean():.3f}  "
          f"farther {np.where(near_up, ev.touch_l, ev.touch_h).mean():.3f}")

    # Part 2
    g = tr.groupby("rule")
    print("\nrule      n   win   meanATR  meanR   sumATR  tgtATR  target/stop/time")
    for rule, t in g:
        w = t.why.value_counts(normalize=True)
        print(f"{rule:8s}{len(t):4d} {np.mean(t.r>0):.3f} {t.r.mean():+.4f} {t.R.mean():+.4f} "
              f"{t.r.sum():+7.1f} {t.tgt.median():6.2f}  "
              f"{w.get('target',0):.2f}/{w.get('stop',0):.2f}/{w.get('time',0):.2f}")
    print("\nby year (meanATR):")
    tr["year"] = pd.to_datetime(tr.day).dt.year
    print(tr.pivot_table(index="rule", columns="year", values="r", aggfunc="mean").round(3))


if __name__ == "__main__":
    main()
