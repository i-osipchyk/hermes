"""London range bracket at the NY open (idea c706aa04, trials 2-3) + filter dissection.

Entry     market at the open of the 08:30 ET bar, price inside the London range
          (08:00 Europe/London to 08:30 America/New_York).
G_far     toward the farther edge: target = farther edge, stop = nearer edge  (RR > 1)
G_near    toward the nearer edge:  target = nearer edge,  stop = farther edge (RR < 1)
          Flat at the 16:45 ET bar open; stop checked before target on a bar (ADR-0004).
Result    net R (per unit risked) and net ATR(4h,20), cost $0.20/oz round trip.
Features  all known at 08:30 ET (see FEATURES below); bucketed for both geometries.

Run:  .venv/bin/python research/london_range/london_bracket_study.py [--holdout]
      Default is in-sample 2018-2022. --holdout scores 2023-2026.09 and prints only the
      rules named in HOLDOUT_RULES, so the holdout is touched once per declared candidate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BARS = ROOT / ".hermes_cache/bars/pepperstone/XAUUSD_5m.parquet"
OUT = ROOT / "research/output/london_range"
COST = 0.20
IS_START, IS_END = pd.Timestamp("2018-01-01", tz="UTC"), pd.Timestamp("2023-01-01", tz="UTC")
LDN, NY = "Europe/London", "America/New_York"
HOLDOUT_RULES: dict[str, str] = {}   # name -> pandas query over the trades frame; set before --holdout


def load(holdout: bool) -> pd.DataFrame:
    m = pd.read_parquet(BARS)
    m.index = pd.to_datetime(m.ts, unit="s", utc=True)
    lo, hi = (IS_END, pd.Timestamp("2026-10-01", tz="UTC")) if holdout else (IS_START, IS_END)
    m = m[(m.index >= lo - pd.Timedelta("150D")) & (m.index < hi)]
    h4 = m.resample("4h").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    pc = h4.close.shift()
    tr = pd.concat([h4.high - h4.low, (h4.high - pc).abs(), (h4.low - pc).abs()], axis=1).max(axis=1)
    atr = pd.Series(tr.ewm(alpha=1 / 20, adjust=False).mean().values, index=h4.index + pd.Timedelta("4h"))
    m["atr"] = atr.reindex(m.index, method="ffill").to_numpy()
    m.attrs["lo"] = lo
    return m


def walk(d, entry, stop, tp, h, lo, o, f, last):
    for q in range(f, last):
        if (lo[q] <= stop) if d > 0 else (h[q] >= stop):
            return (min(stop, o[q]) if d > 0 else max(stop, o[q])), "stop"
        if (h[q] >= tp) if d > 0 else (lo[q] <= tp):
            return (max(tp, o[q]) if d > 0 else min(tp, o[q])), "target"
    return o[last], "time"


def build(m: pd.DataFrame) -> pd.DataFrame:
    o, h, lo, c = (m[k].to_numpy() for k in ("open", "high", "low", "close"))
    ny, ldn = m.index.tz_convert(NY), m.index.tz_convert(LDN)
    nym = np.asarray(ny.hour * 60 + ny.minute)
    ldnm = np.asarray(ldn.hour * 60 + ldn.minute)
    nyd, ldd = np.asarray(ny.date), np.asarray(ldn.date)
    # NY trading day for the prior-day features: gold's day runs 18:00 ET -> 17:00 ET
    tday = np.asarray((ny + pd.Timedelta("6h")).date)
    prev: dict = {}
    rows = []
    groups = pd.Series(np.arange(len(m))).groupby(tday)
    days = list(groups)
    for k, (day, ii) in enumerate(days):
        ii = ii.to_numpy()
        if k:
            pi = days[k - 1][1].to_numpy()
            prev = dict(ph=h[pi].max(), pl=lo[pi].min(), po=o[pi[0]], pcl=c[pi[-1]])
        if pd.Timestamp(day).weekday() >= 5 or not prev:
            continue
        rng = ii[(ldnm[ii] >= 480) & (ldd[ii] == day) & (nyd[ii] == day) & (nym[ii] < 510)]
        start, end = ii[nym[ii] == 510], ii[nym[ii] == 1005]
        asia = ii[(nyd[ii] < day) | ((nym[ii] < 180) & (nyd[ii] == day))]  # 18:00-03:00 ET
        if len(rng) < 40 or not len(start) or not len(end) or len(asia) < 50:
            continue
        f, last = start[0], end[0]
        H, L, a, p0 = h[rng].max(), lo[rng].min(), m.atr.iat[f], o[f]
        if not np.isfinite(a) or not (L < p0 < H) or m.index[f] < m.attrs["lo"]:
            continue
        aH, aL = h[asia].max(), lo[asia].min()
        t_hi, t_lo = rng[np.argmax(h[rng])], rng[np.argmin(lo[rng])]
        up, dn = H - p0, p0 - L
        far = 1 if up > dn else -1
        base = dict(
            day=day, atr=a, width=(H - L) / a, pos=(p0 - L) / (H - L), rr_far=max(up, dn) / min(up, dn),
            ldn_ret=(p0 - o[rng[0]]) / a,                          # London session move so far
            asia_w=(aH - aL) / a,
            broke_asia=("both" if H > aH and L < aL else "high" if H > aH else "low" if L < aL else "none"),
            last_ext=1 if t_hi > t_lo else -1,                     # which London extreme is newer
            ext_age=(f - max(t_hi, t_lo)) * 5,                     # minutes since newest extreme
            prev_ret=(prev["pcl"] - prev["po"]) / a,
            vs_prev=("above" if p0 > prev["ph"] else "below" if p0 < prev["pl"] else "inside"),
            H_vs_ph=(H - prev["ph"]) / a, L_vs_pl=(prev["pl"] - L) / a,
            weekday=pd.Timestamp(day).weekday(),
            nfp=pd.Timestamp(day).weekday() == 4 and pd.Timestamp(day).day <= 7,
            pre=(h[f - 6:f].max() - lo[f - 6:f].min()) / a,       # last 30 min of London, range
            pre_ret=(p0 - o[f - 6]) / a,
        )
        for rule, d in (("G_far", far), ("G_near", -far)):
            tp, sl = (H, L) if d > 0 else (L, H)
            px, why = walk(d, p0, sl, tp, h, lo, o, f, last)
            risk = abs(p0 - sl)
            rows.append(dict(base, rule=rule, d=d, why=why, rr=abs(tp - p0) / risk,
                             R=(d * (px - p0) - COST) / risk, r=(d * (px - p0) - COST) / a,
                             gross_R=d * (px - p0) / risk,
                             # trade-relative features: + means "with the trade"
                             ldn_with=np.sign(d * base["ldn_ret"]), newest_tgt=int(base["last_ext"] == d),
                             pre_with=np.sign(d * base["pre_ret"]), prev_with=np.sign(d * base["prev_ret"])))
    return pd.DataFrame(rows)


def summary(t: pd.DataFrame, label: str) -> None:
    w = t.why.value_counts(normalize=True)
    print(f"{label:28s} n={len(t):4d} win={np.mean(t.R > 0):.3f} RR~{t.rr.median():4.2f} "
          f"grossR={t.gross_R.mean():+.3f} netR={t.R.mean():+.3f} netATR={t.r.mean():+.3f} "
          f"sumR={t.R.sum():+6.1f}  tgt/stop/time "
          f"{w.get('target', 0):.2f}/{w.get('stop', 0):.2f}/{w.get('time', 0):.2f}")


def dissect(t: pd.DataFrame) -> None:
    t = t.copy()
    t["wrel"] = t.groupby("rule").width.transform(
        lambda s: s / s.rolling(20, min_periods=15).median().shift())
    cuts = {
        "width ATR": lambda x: pd.qcut(x.width, 4),
        "wrel vs prev20": lambda x: pd.qcut(x.wrel, 4),
        "rr_far": lambda x: pd.qcut(x.rr_far, 4),
        "atr level (q)": lambda x: pd.qcut(x.atr.rank(pct=True), 4),
        "asia width": lambda x: pd.qcut(x.asia_w, 4),
        "London broke Asia": lambda x: x.broke_asia,
        "London move with trade": lambda x: x.ldn_with,
        "newest extreme = target": lambda x: x.newest_tgt,
        "ext_age min": lambda x: pd.cut(x.ext_age, [-1, 30, 90, 180, 600]),
        "last 30m with trade": lambda x: x.pre_with,
        "last 30m range ATR": lambda x: pd.qcut(x.pre, 4),
        "prev day with trade": lambda x: x.prev_with,
        "p0 vs prev day": lambda x: x.vs_prev,
        "weekday": lambda x: x.weekday,
        "NFP Friday": lambda x: x.nfp,
        "side": lambda x: x.d,
        "year": lambda x: pd.to_datetime(x.day).dt.year,
    }
    for name, fn in cuts.items():
        print(f"\n== {name}")
        for rule, g in t.groupby("rule"):
            s = g.assign(b=fn(g)).groupby("b", observed=True).agg(
                n=("R", "size"), win=("R", lambda x: (x > 0).mean()), netR=("R", "mean"),
                se=("R", lambda x: x.std() / np.sqrt(len(x))), grossR=("gross_R", "mean"))
            s["t"] = s.netR / s.se
            print(f"  {rule}\n" + s.drop(columns="se").round(3).to_string().replace("\n", "\n    "))


def main() -> None:
    holdout = "--holdout" in sys.argv
    t = build(load(holdout))
    OUT.mkdir(parents=True, exist_ok=True)
    if holdout:
        t.to_csv(OUT / "bracket_holdout.csv", index=False)
        for name, q in HOLDOUT_RULES.items():
            summary(t.query(q), f"HOLDOUT {name}")
        return
    t.to_csv(OUT / "bracket_is.csv", index=False)
    for rule, g in t.groupby("rule"):
        summary(g, rule)
    dissect(t)


if __name__ == "__main__":
    main()
