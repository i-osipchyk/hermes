"""Aero-style entry/exits on the rapid_break fractal events, with the EA's fixed dollars replaced
by a multiple of a lower-timeframe ATR (idea 16458973, follow-up 2026-10-14).

Entry    (Aero) limit at the broken level: filled if the minute after the trigger minute trades
         at the level (at the level, or the open if better); otherwise a market chase at the open
         of the following minute.
Exits    from the minute after the fill, each minute, in this order (Aero order):
           stop      through the stop -> exit at the stop, or the open if gapped
           target    entry +/- TP
           trail     once best excursion >= 0.6 TP, stop locks entry +/- 0.5 TP, then follows
                     0.1 TP behind in 0.0175 TP steps    (Aero: $12 / $10 / $2 / $0.35 for $20)
           3h rule   from the first in-session minute >= 180 min after entry, a stop at
                     entry -/+ 0.5 ATR(4h,20) for the rest of the trade; if already beyond it,
                     exit at that close.  (A one-off check at minute 180 let ~10% of trades reach
                     the catastrophe stop, which the EA never hits.)
           catastrophe stop entry -/+ 3.6 ATR(4h,20); 10-day cap
TP       = k x ATR(LTF,14) known at the trigger (closed LTF bars only).  k is CALIBRATED, not fitted:
         median TP over 2018-22 events = $20 (Aero's default).  'usd20' = the EA's fixed $20.
Control  per event, 3 random minutes in the same month, same UTC hour, same side, market entry at
         the next open, identical exits -- what the geometry earns without the break.
Costs    $0.20/oz.  R = net $/oz / ATR(4h,20).

Run:  .venv/bin/python research/rapid_break/rapid_break_aero_exits.py [--holdout]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import rapid_break_events as rbe  # noqa: E402

OUT = rbe.OUT
COST, GRID_ATR, CAT_ATR, GRID_MIN, MAX_MIN = 0.20, 0.5, 3.6, 180, 10 * 1440
LOCK_AT, LOCK, GAP, STEP = 0.6, 0.5, 0.1, 0.0175
TFS = ("15min", "1h")
EVENTS = ("1h_2", "30min_1")
W = {"fractal": 1.0, "session": 1.5, "day": 2.0}


def atr_ltf(m: pd.DataFrame, rule: str, n: int = 14) -> np.ndarray:
    b = m.resample(rule).agg({"high": "max", "low": "min", "close": "last"}).dropna()
    pc = b.close.shift()
    tr = pd.concat([b.high - b.low, (b.high - pc).abs(), (b.low - pc).abs()], axis=1).max(axis=1)
    a = tr.ewm(alpha=1 / n, adjust=False).mean()
    end = (b.index + pd.Timedelta(rule)).tz_convert(None).to_numpy()
    pos = np.searchsorted(end, m.index.tz_convert(None).to_numpy(), side="right") - 1
    return np.where(pos >= 0, a.to_numpy()[np.clip(pos, 0, None)], np.nan)


class Sim:
    def __init__(self, m: pd.DataFrame, smin: np.ndarray):
        self.o, self.h, self.lo, self.c = (m[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        self.smin, self.n = smin, len(m)

    def exits(self, f: int, d: int, entry: float, tp_usd: float, atr: float) -> tuple[float, str, int]:
        o, h, lo, c = self.o, self.h, self.lo, self.c
        tp = entry + d * tp_usd
        stop = entry - d * CAT_ATR * atr
        lock_at, lock, gap, step = (x * tp_usd for x in (LOCK_AT, LOCK, GAP, STEP))
        locked = checked = False
        for q in range(f + 1, min(f + MAX_MIN, self.n)):
            if (lo[q] <= stop) if d > 0 else (h[q] >= stop):
                px = min(stop, o[q]) if d > 0 else max(stop, o[q])
                return d * (px - entry), "trail" if locked else "stop", q
            if (h[q] >= tp) if d > 0 else (lo[q] <= tp):
                return d * (tp - entry), "tp", q
            best = d * ((h[q] if d > 0 else lo[q]) - entry)
            if best >= lock_at:
                new = entry + d * (lock + max(np.floor((best - gap - lock) / step), 0) * step)
                stop = max(stop, new) if d > 0 else min(stop, new)
                locked = True
            if not checked and q - f >= GRID_MIN and 150 <= self.smin[q] < 1380:
                # armed for the rest of the trade (the EA's wide stop never fires, so the
                # -0.5 ATR exit cannot be a one-off check at minute 180)
                checked = True
                g = entry - d * GRID_ATR * atr
                if d * (c[q] - g) <= 0:
                    return d * (c[q] - entry), "grid", q
                stop = max(stop, g) if d > 0 else min(stop, g)
        q = min(f + MAX_MIN, self.n) - 1
        return d * (self.c[q] - entry), "cap", q

    def break_trade(self, t: int, d: int, level: float, tp_usd: float, atr: float):
        k = t + 1
        if k + 1 >= self.n:
            return None
        if (self.lo[k] <= level) if d > 0 else (self.h[k] >= level):
            entry, f = (min(level, self.o[k]) if d > 0 else max(level, self.o[k])), k
        else:
            entry, f = self.o[k + 1], k + 1
        return entry, f, *self.exits(f, d, entry, tp_usd, atr)


def run(name: str, m: pd.DataFrame, ctx: dict, ltf: dict, holdout: bool) -> pd.DataFrame:
    ev = pd.read_parquet(OUT / f"rapid_break_{name}.parquet")
    if not holdout:
        ev = ev[ev.t < rbe.IS_END]
    ev = ev.reset_index(drop=True)
    sim = Sim(m, ctx["smin"])
    ts = m.index.tz_convert(None).to_numpy()
    tix = np.searchsorted(ts, ev.t.dt.tz_convert(None).to_numpy())
    is_ = (ev.t < rbe.IS_END).to_numpy()
    tps = {"usd20": np.full(len(ev), 20.0)}
    for rule in TFS:
        a = ltf[rule][tix]
        k = 20.0 / np.nanmedian(a[is_])
        tps[f"atr{rule}"] = k * a
        print(f"  {name}: TP = {k:.2f} x ATR({rule},14)  (IS median ATR ${np.nanmedian(a[is_]):.2f})")
    rng = np.random.default_rng(0)
    key = (m.index.year * 10000 + m.index.month * 100 + m.index.hour).to_numpy()
    order = np.argsort(key, kind="stable")
    sk = key[order]
    ek = (ev.t.dt.year * 10000 + ev.t.dt.month * 100 + ev.t.dt.hour).to_numpy()
    smin = ctx["smin"]
    rows = []
    for i, e in enumerate(ev.itertuples(index=False)):
        t = tix[i]
        lo_, hi_ = np.searchsorted(sk, ek[i]), np.searchsorted(sk, ek[i], side="right")
        ctrl_bars = [p for p in order[rng.integers(lo_, hi_, 6)] if 150 <= smin[p] < 1320][:3]
        for tpname, tpv in tps.items():
            tp = tpv[i]
            if not tp == tp:
                continue
            res = sim.break_trade(t, e.side, e.level, tp, e.atr)
            if res is None:
                continue
            entry, f, usd, how, q = res
            cr = [sim.exits(p + 1, e.side, sim.o[p + 1], tp, e.atr)[0] for p in ctrl_bars if p + 2 < sim.n]
            rows.append(dict(idx=i, t=e.t, tq=t, exitq=q, tp=tpname, tp_usd=tp, side=e.side, tier=e.tier,
                             atr=e.atr, limit=f == t + 1, how=how, usd=usd - COST,
                             ctrl_usd=np.mean(cr) - COST if cr else np.nan,
                             minutes=q - f))
    out = pd.DataFrame(rows)
    out["r"] = out.usd / out.atr
    out["ctrl_r"] = out.ctrl_usd / out.atr
    out["ny"] = out.t.dt.hour.between(12, 17)
    out["period"] = np.where(out.t < rbe.IS_END, "IS", "HOLD")
    out.to_parquet(OUT / f"rapid_break_aero_{name}{'_all' if holdout else ''}.parquet")
    return out


def summarize(x: pd.DataFrame) -> pd.Series:
    r = x.r
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    return pd.Series(dict(n=len(x), win=100 * (x.usd > 0).mean(), usd=x.usd.mean(), r=r.mean(),
                          ctrl_r=x.ctrl_r.mean(), excess=(r - x.ctrl_r).mean(),
                          t=(r - x.ctrl_r).mean() / (r - x.ctrl_r).std() * np.sqrt(len(x)),
                          pf=pos / neg if neg else np.inf,
                          ctrl_win=100 * (x.ctrl_usd > 0).mean(),
                          stop_hits=(x.how == "stop").sum()))


def sequential(x: pd.DataFrame) -> pd.DataFrame:
    """One position at a time: skip events that trigger while the previous trade is open."""
    keep, free = [], -1
    for i, r in x.sort_values("t").iterrows():
        if r.tq >= free:
            keep.append(i)
            free = r.exitq
    return x.loc[keep]


def candidates(out: pd.DataFrame) -> None:
    """Pre-declared on IS (2026-10-14): A tier-weighted all events, B day+NY, C = B with $20."""
    rows = []
    for p in ("IS", "HOLD"):
        x = out[out.period == p]
        a = x[x.tp == "atr1h"]
        b = x[(x.tp == "atr1h") & (x.tier == "day") & x.ny]
        c = x[(x.tp == "usd20") & (x.tier == "day") & x.ny]
        for name, sub, w in (("A tiers 1/1.5/2, ATR1h", a, a.tier.map(W)),
                             ("B day+NY, ATR1h", b, None), ("C day+NY, $20", c, None),
                             ("B one-at-a-time", sequential(b), None)):
            s_ = summarize(sub)
            if w is not None:
                s_["r"] = (w * sub.r).sum() / w.sum()
                s_["excess"] = (w * (sub.r - sub.ctrl_r)).sum() / w.sum()
            s_["r_2x"] = s_["r"] - (0.20 / sub.atr).mean()
            yrs = sub.groupby(sub.t.dt.year).r.sum()
            rows.append({"period": p, "cand": name, **s_.to_dict(),
                         "pos_years": f"{(yrs > 0).sum()}/{len(yrs)}"})
    print(pd.DataFrame(rows).round(3).to_string(index=False))


def main() -> None:
    holdout = "--holdout" in sys.argv
    m = rbe.load()
    ctx = rbe.context(m)
    ltf = {rule: atr_ltf(m, rule) for rule in TFS}
    for name in EVENTS[:1] if holdout else EVENTS:
        out = run(name, m, ctx, ltf, holdout)
        if holdout:
            candidates(out)
            continue
        for p in (["IS"] + (["HOLD"] if holdout else [])):
            x = out[out.period == p]
            print(f"\n=== {name} {p}: by TP model")
            print(x.groupby("tp").apply(summarize).round(3).to_string())
            print(f"\n=== {name} {p}: by TP model x tier")
            print(x.groupby(["tp", "tier"]).apply(summarize).round(3).to_string())
            print(f"\n=== {name} {p}: day tier, NY vs not")
            print(x[x.tier == "day"].groupby(["tp", "ny"]).apply(summarize).round(3).to_string())
            print(f"\n=== {name} {p}: exit reasons")
            print(x.groupby("tp").how.value_counts(normalize=True).unstack().round(3).to_string())


if __name__ == "__main__":
    main()
