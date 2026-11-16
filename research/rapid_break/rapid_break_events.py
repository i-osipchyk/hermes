"""Rapid breakout through M30/H1 fractal levels on gold -- event study (idea 16458973).

Claim: a $0.50 break of a confirmed M30/H1 fractal that arrives FAST continues more than a slow
one, and more when the fractal is also the running session/day extreme (confluence tier).

Bars     XAUUSD 1m (Pepperstone), UTC. Server time = New York + 7h (broker day starts 17:00 NY).
Levels   Fractals of width n on M30/H1 bars resampled from 1m: bar i's high strictly above the n
         bars before it and >= the n after; known when bar i+n closes (ADR-0002). Dropped after
         20 days unbroken. Event = the first 1m bar whose high (low) trades $0.50 through it;
         events outside 02:30-22:00 server time consume the level and are not traded.
Tier     (known before the break bar) the level is within TOL_ATR x ATR of the running extreme of
           day      the current broker day,
           session  the current UTC session (Asia 00-07, London 07-12, NY 12-21),
         else fractal-only. Diagnostics: previous session / previous day extreme.
Speed    v15 = distance of the 1m close 15 bars before the break bar from the level, in ATR
         (large = covered a lot of ground in 15 min = fast).  Also v5, v60.
Outcome  entry = max(open, trigger) on the break bar; drift_H = side x (close H minutes later -
         entry) / ATR, gross, for H in 30/60/120; NaN if the window has a gap or crosses
         22:50 server.  Stopped variant: stop 0.5 ATR from entry, checked from the next bar
         (stop-first), R units.  ATR = Wilder(20) on server-time 4h bars, closed bars only.
Control  for each event, 10 random 1m bars in the same month, same UTC hour, same side:
         their drift_H (entry at next open) is the drift a break has to beat.

Run:  .venv/bin/python research/rapid_break/rapid_break_events.py           # build + IS tables
      .venv/bin/python research/rapid_break/rapid_break_events.py --holdout  # also 2023-26.08
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BARS = ROOT / ".hermes_cache" / "bars" / "pepperstone" / "XAUUSD_1m.parquet"
OUT = ROOT / "research" / "output"
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
HOLD_END = pd.Timestamp("2026-08-06", tz="UTC")
BREAK_USD = 0.50
COST_USD = 0.20
TOL_ATR = 0.05
MAX_AGE_MIN = 20 * 24 * 60
HORIZONS = (30, 60, 120)
STOP_ATR = 0.5
CONFIGS = [("30min", 1), ("30min", 2), ("1h", 1), ("1h", 2)]
SESSIONS = ((0, 7, "asia"), (7, 12, "london"), (12, 21, "ny"))


def load() -> pd.DataFrame:
    m = pd.read_parquet(BARS)
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    m = m[["open", "high", "low", "close"]]
    m = m[m.index >= pd.Timestamp("2017-12-01", tz="UTC")]
    m["srv"] = m.index.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    return m


def atr_4h(m: pd.DataFrame) -> np.ndarray:
    h4 = m.resample("4h", on="srv").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    pc = h4.close.shift()
    tr = pd.concat([h4.high - h4.low, (h4.high - pc).abs(), (h4.low - pc).abs()], axis=1).max(axis=1)
    a = tr.ewm(alpha=1 / 20, adjust=False).mean()
    s = pd.Series(a.values, index=h4.index + pd.Timedelta("4h"))   # known at the bar's close
    pos = np.searchsorted(s.index.values, m.srv.to_numpy(), side="right") - 1
    return np.where(pos >= 0, s.values[np.clip(pos, 0, None)], np.nan)


def running_extremes(m: pd.DataFrame, key: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """High/low of the group `key` up to the bar BEFORE each bar (NaN on the group's first bar)."""
    g = pd.Series(key, index=m.index)
    hi = m.high.groupby(g.values).cummax().groupby(g.values).shift()
    lo = m.low.groupby(g.values).cummin().groupby(g.values).shift()
    return hi.to_numpy(), lo.to_numpy()


def previous_group_extremes(m: pd.DataFrame, key: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Final high/low of the previous group (completed), aligned to each bar."""
    df = pd.DataFrame({"k": key, "h": m.high.values, "l": m.low.values})
    agg = df.groupby("k", sort=False).agg(h=("h", "max"), l=("l", "min"))
    prev = agg.shift()
    return prev.h.reindex(key).to_numpy(), prev.l.reindex(key).to_numpy()


def fractals(m: pd.DataFrame, rule: str, n: int) -> pd.DataFrame:
    b = m.resample(rule).agg({"high": "max", "low": "min"}).dropna()
    H, L = b.high.to_numpy(), b.low.to_numpy()
    dur = pd.Timedelta(rule)
    hi = np.ones(len(b), bool)
    lo = np.ones(len(b), bool)
    for k in range(1, n + 1):
        hi &= (H > np.roll(H, k)) & (H >= np.roll(H, -k))
        lo &= (L < np.roll(L, k)) & (L <= np.roll(L, -k))
    hi[:n] = hi[-n:] = lo[:n] = lo[-n:] = False
    conf = b.index[np.clip(np.arange(len(b)) + n, 0, len(b) - 1)] + dur
    rows = [(conf[i], 1, H[i]) for i in np.flatnonzero(hi)] + \
           [(conf[i], -1, L[i]) for i in np.flatnonzero(lo)]
    return pd.DataFrame(rows, columns=["conf", "side", "level"]).sort_values("conf")


def build(m: pd.DataFrame, rule: str, n: int, ctx: dict) -> pd.DataFrame:
    o, h, lo, c = (m[k].to_numpy() for k in ("open", "high", "low", "close"))
    ts = m.index.tz_convert(None).to_numpy()
    tmin = (ts - ts[0]) // np.timedelta64(1, "m")
    smin = ctx["smin"]
    atr = ctx["atr"]
    N = len(m)
    rows = []
    for conf, side, level in fractals(m, rule, n).itertuples(index=False):
        s = np.searchsorted(ts, conf.to_datetime64())
        if s >= N:
            continue
        e = np.searchsorted(tmin, tmin[s] + MAX_AGE_MIN)
        trig = level + side * BREAK_USD
        hit = (h[s:e] >= trig) if side > 0 else (lo[s:e] <= trig)
        if not hit.any():
            continue
        t = s + int(hit.argmax())
        a = atr[t]
        if not (150 <= smin[t] < 22 * 60) or not a == a or t < 61:
            continue
        fill = max(o[t], trig) if side > 0 else min(o[t], trig)
        r = dict(t=m.index[t], side=side, level=level, atr=a, fill=fill,
                 age_h=(ts[t] - conf.to_datetime64()) / np.timedelta64(1, "h"),
                 overshoot=side * (fill - trig) / a)
        for k in (5, 15, 60):
            r[f"v{k}"] = side * (level - c[t - 1 - k]) / a if tmin[t] - tmin[t - 1 - k] <= k + 5 else np.nan
        for name in ("day", "sess"):
            rh, rl = ctx[name]
            ext = rh[t] if side > 0 else rl[t]
            r[f"{name}_ext"] = bool(ext == ext and -TOL_ATR * a <= side * (ext - level) <= BREAK_USD)
        for name in ("pday", "psess"):
            ph, pl = ctx[name]
            ext = ph[t] if side > 0 else pl[t]
            r[f"{name}_ext"] = bool(ext == ext and abs(ext - level) <= TOL_ATR * a)
        stop = fill - side * STOP_ATR * a
        for H in HORIZONS:
            j = t + H
            ok = j < N and tmin[j] - tmin[t] == H and smin[t] + H <= 22 * 60 + 50
            if not ok:
                r[f"d{H}"] = r[f"r{H}"] = np.nan
                continue
            r[f"d{H}"] = side * (c[j] - fill) / a
            adverse = lo[t + 1:j + 1] <= stop if side > 0 else h[t + 1:j + 1] >= stop
            r[f"r{H}"] = -1.0 if adverse.any() else side * (c[j] - fill) / (STOP_ATR * a)
        rows.append(r)
    ev = pd.DataFrame(rows)
    ev["tier"] = np.where(ev.day_ext, "day", np.where(ev.sess_ext, "session", "fractal"))
    return ev


def control(m: pd.DataFrame, ev: pd.DataFrame, ctx: dict, seed: int = 0) -> pd.DataFrame:
    """Same month, same UTC hour, same side, random 1m bars: their drift_H."""
    rng = np.random.default_rng(seed)
    o, c = m.open.to_numpy(), m.close.to_numpy()
    ts = m.index.tz_convert(None).to_numpy()
    tmin = (ts - ts[0]) // np.timedelta64(1, "m")
    smin, atr = ctx["smin"], ctx["atr"]
    key = (m.index.year * 10000 + m.index.month * 100 + m.index.hour).to_numpy()
    order = np.argsort(key, kind="stable")
    sk = key[order]
    out = {f"c{H}": [] for H in HORIZONS}
    ek = (ev.t.dt.year * 10000 + ev.t.dt.month * 100 + ev.t.dt.hour).to_numpy()
    for k, side in zip(ek, ev.side.to_numpy(), strict=True):
        lo_, hi_ = np.searchsorted(sk, k), np.searchsorted(sk, k, side="right")
        pick = order[rng.integers(lo_, hi_, 10)]
        for H in HORIZONS:
            vals = []
            for p in pick:
                j = p + 1 + H
                if j < len(m) and tmin[j] - tmin[p + 1] == H and smin[p] + H <= 22 * 60 + 50 \
                        and atr[p] == atr[p]:
                    vals.append(side * (c[j] - o[p + 1]) / atr[p])
            out[f"c{H}"].append(np.mean(vals) if vals else np.nan)
    return pd.DataFrame(out, index=ev.index)


def context(m: pd.DataFrame) -> dict:
    srv = m.srv
    sday = srv.dt.normalize().to_numpy()
    hr = m.index.hour
    sess = np.full(len(m), "", dtype=object)
    for a, b, name in SESSIONS:
        sess[(hr >= a) & (hr < b)] = name
    skey = np.char.add(m.index.strftime("%Y%m%d").to_numpy().astype(str), sess.astype(str))
    skey = np.where(sess == "", "off" + skey, skey)
    in_sess = sess != ""
    sh, sl = running_extremes(m, skey)
    ph, pl = previous_group_extremes(m[in_sess], skey[in_sess])
    psh = np.full(len(m), np.nan)
    psl = np.full(len(m), np.nan)
    psh[in_sess], psl[in_sess] = ph, pl
    return dict(
        smin=(srv.dt.hour * 60 + srv.dt.minute).to_numpy(),
        atr=atr_4h(m),
        day=running_extremes(m, sday),
        sess=(np.where(in_sess, sh, np.nan), np.where(in_sess, sl, np.nan)),
        pday=previous_group_extremes(m, sday),
        psess=(psh, psl),
    )


def table(ev: pd.DataFrame, by: list[str], H: int = 60) -> pd.DataFrame:
    g = ev.groupby(by, observed=True)
    t = g.agg(n=(f"d{H}", "count"), drift=(f"d{H}", "mean"), ctrl=(f"c{H}", "mean"),
              stopR=(f"r{H}", "mean"))
    t["excess"] = t.drift - t.ctrl
    t["se"] = g.apply(lambda x: (x[f"d{H}"] - x[f"c{H}"]).std() / np.sqrt(x[f"d{H}"].count()))
    t["netR"] = t.stopR - g.apply(lambda x: (COST_USD / (STOP_ATR * x.atr)).mean())
    return t.round(4)


def report(ev: pd.DataFrame, label: str) -> None:
    print(f"\n##### {label}: {len(ev)} events")
    for H in HORIZONS:
        print(f"\n-- H={H} min, by tier")
        print(table(ev, ["tier"], H).to_string())
    print("\n-- H=60, by tier x v15 quintile (quintile edges from IS)")
    print(table(ev, ["tier", "vq"], 60).to_string())
    print("\n-- H=60, v15 quintile (all tiers)")
    print(table(ev, ["vq"], 60).to_string())
    print("\n-- H=60, diagnostics: previous session / previous day extreme")
    print(table(ev, ["psess_ext"], 60).to_string())
    print(table(ev, ["pday_ext"], 60).to_string())


def main() -> None:
    holdout = "--holdout" in sys.argv
    m = load()
    ctx = context(m)
    for rule, n in CONFIGS:
        path = OUT / f"rapid_break_{rule}_{n}.parquet"
        if path.exists():
            ev = pd.read_parquet(path)
        else:
            ev = build(m, rule, n, ctx)
            ev = ev[(ev.t >= "2018-01-01") & (ev.t < HOLD_END)].reset_index(drop=True)
            ev = ev.join(control(m, ev, ctx))
            ev.to_parquet(path)
        is_ = ev.t < IS_END
        edges = ev.loc[is_, "v15"].quantile([0.2, 0.4, 0.6, 0.8]).to_numpy()
        ev["vq"] = np.digitize(ev.v15, edges)
        print(f"\n================ fractals {rule} n={n}  (v15 quintile edges {np.round(edges, 3)})")
        report(ev[is_], "IN-SAMPLE 2018-2022")
        if holdout:
            report(ev[~is_], "HOLDOUT 2023-01..2026-08-05")


if __name__ == "__main__":
    main()
