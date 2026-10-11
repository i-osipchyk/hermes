"""Silver session-range breakout variants, event study (idea e97af039).

Three faithful ways to carry the gold Tokyo breakout (0945d084) to XAGUSD, screened together:

  A  gold-led   gold's Tokyo range (00-09 UTC) and its first break per side in 09-20 UTC
                (0.02 ATR beyond the edge); enter SILVER at the next 5m bar's open in the
                break direction. Reported for all gold breaks and for wide gold days.
  C  confirmed  silver's own Tokyo break, kept only if gold broke the same side earlier
                that day (or on the same bar).
  B  London     silver's 07-12 UTC range broken in 12-20 UTC (COMEX hours).

Exit for all: silver stop 1 ATR(4h,20) of silver, flat at the close of the 20:45 UTC bar
(the engine's flat-by-20:50). Fill bar: stop assumed hit if touched (conservative).
Width filter: range / median of the previous 20 same-session ranges; "wide" = top fifth with
the cut-off taken from 2018-22 only. Each side of each day is its own event.
Costs (measured from Pepperstone cTrader ticks, see FINDINGS.md): silver spread $0.012 in
2018-22, $0.018 in 2023-25, no commission. 2026 is held out (spread ~$0.05).
R = (move - cost) / silver ATR.  Choose on 2018-22; 2023-25 is looked at once.

Run:  .venv/bin/python research/tokyo/silver_gold_led_study.py            # 2018-22 only
      .venv/bin/python research/tokyo/silver_gold_led_study.py "A wide"   # + 2023-25 for ONE
      variant, after choosing it on 2018-22 (prefix match on the variant label)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PICK = sys.argv[1] if len(sys.argv) > 1 else None   # read before tokyo_multi_market rewrites argv
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tokyo_multi_market as tm  # noqa: E402

OUT = tm.OUT
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
END = pd.Timestamp("2026-01-01", tz="UTC")
TRIG = 0.02
FLAT_MIN = 20 * 60 + 45


def cost(ts: pd.Timestamp) -> float:
    return 0.012 if ts < IS_END else 0.018


def sessions(m: pd.DataFrame, r0: int, r1: int, w1: int) -> dict:
    """Per weekday: range bars [r0, r1) min, window bars [r1, w1) min, last bar <= FLAT_MIN."""
    mins = (m.index.hour * 60 + m.index.minute).to_numpy()
    out = {}
    for dte, idx in pd.Series(np.arange(len(m)), index=m.index.normalize()).groupby(level=0):
        if dte.weekday() >= 5:
            continue
        ii = idx.to_numpy()
        rng, win = ii[(mins[ii] >= r0) & (mins[ii] < r1)], ii[(mins[ii] >= r1) & (mins[ii] < w1)]
        upto = ii[mins[ii] <= FLAT_MIN]
        if len(rng) < (r1 - r0) // 5 * 0.8 or len(win) < 12:
            continue
        out[dte] = (rng, win, upto[-1])
    return out


def with_rel20(days: dict, m: pd.DataFrame) -> pd.DataFrame:
    h, lo = m.high.to_numpy(), m.low.to_numpy()
    d = pd.DataFrame({dte: dict(hi=h[r].max(), lo=lo[r].min()) for dte, (r, _, _) in days.items()}).T
    d["rng"] = d.hi - d.lo
    d["rel20"] = d.rng / d.rng.shift(1).rolling(20, min_periods=15).median()
    return d


def first_breaks(days: dict, d: pd.DataFrame, m: pd.DataFrame) -> list[dict]:
    h, lo, atr = m.high.to_numpy(), m.low.to_numpy(), m.atr.to_numpy()
    rows = []
    for dte, (_, win, _) in days.items():
        a = atr[win[0]]
        if np.isnan(a):
            continue
        for side, lvl in ((1, d.at[dte, "hi"]), (-1, d.at[dte, "lo"])):
            trig = lvl + side * TRIG * a
            hit = win[(h[win] >= trig) if side > 0 else (lo[win] <= trig)]
            if len(hit):
                rows.append(dict(date=dte, side=side, j=hit[0], trig=trig, rel20=d.at[dte, "rel20"]))
    return rows


def silver_exit(side: int, entry: float, j: int, last: int, a: float, s: pd.DataFrame) -> float:
    o, h, lo, c = (s[k].to_numpy() for k in ("open", "high", "low", "close"))
    stop = entry - side * a
    for q in range(j, last + 1):
        if (lo[q] <= stop) if side > 0 else (h[q] >= stop):
            return stop if q == j else (min(stop, o[q]) if side > 0 else max(stop, o[q]))
    return c[last]


def r_of(side, entry, j, last, s) -> float:
    a = s.atr.iat[j]
    px = silver_exit(side, entry, j, last, a, s)
    return (side * (px - entry) - cost(s.index[j])) / a


def report(name: str, ev: pd.DataFrame) -> dict:
    ev = ev[ev.t < END]
    is_ = ev.t < IS_END
    yrs = ev.groupby(ev.t.dt.year).R.mean()
    return dict(variant=name, n_is=int(is_.sum()), R_is=ev[is_].R.mean(), gross_is=ev[is_].G.mean(),
                n_oos=int((~is_).sum()), R_oos=ev[~is_].R.mean(), gross_oos=ev[~is_].G.mean(),
                yrs_pos=f"{int((yrs > 0).sum())}/{len(yrs)}")


def main() -> None:
    g, s = tm.load("XAUUSD"), tm.load("XAGUSD")
    common = g.index.intersection(s.index)
    g, s = g.loc[common], s.loc[common]

    gd = sessions(g, 0, 540, 1200)
    sd = sessions(s, 0, 540, 1200)
    gr, sr = with_rel20(gd, g), with_rel20(sd, s)
    rows = []

    # A: gold-led, silver entered at the next bar's open
    for b in first_breaks(gd, gr, g):
        if b["date"] not in sd:
            continue
        j = b["j"] + 1
        last = sd[b["date"]][2]
        if j > last or np.isnan(s.atr.iat[j]):
            continue
        entry = s.open.iat[j]
        R = r_of(b["side"], entry, j, last, s)
        rows.append(dict(v="A", t=s.index[j], side=b["side"], rel20=b["rel20"], R=R,
                         G=R + cost(s.index[j]) / s.atr.iat[j]))

    # C: silver's own Tokyo break, confirmed by an earlier same-side gold break
    gb = {(b["date"], b["side"]): g.index[b["j"]] for b in first_breaks(gd, gr, g)}
    for b in first_breaks(sd, sr, s):
        gt = gb.get((b["date"], b["side"]))
        j, last = b["j"], sd[b["date"]][2]
        entry = max(s.open.iat[j], b["trig"]) if b["side"] > 0 else min(s.open.iat[j], b["trig"])
        R = r_of(b["side"], entry, j, last, s)
        rows.append(dict(v="C" if gt is not None and gt <= s.index[j] else "C_unconfirmed",
                         t=s.index[j], side=b["side"], rel20=b["rel20"], R=R,
                         G=R + cost(s.index[j]) / s.atr.iat[j]))

    # B: silver London range 07-12, broken 12-20
    ld = sessions(s, 420, 720, 1200)
    lr = with_rel20(ld, s)
    for b in first_breaks(ld, lr, s):
        j, last = b["j"], ld[b["date"]][2]
        entry = max(s.open.iat[j], b["trig"]) if b["side"] > 0 else min(s.open.iat[j], b["trig"])
        R = r_of(b["side"], entry, j, last, s)
        rows.append(dict(v="B", t=s.index[j], side=b["side"], rel20=b["rel20"], R=R,
                         G=R + cost(s.index[j]) / s.atr.iat[j]))

    ev = pd.DataFrame(rows)
    ev.to_parquet(OUT / "silver_gold_led_events.parquet")

    out = []
    for v in ("A", "C", "C_unconfirmed", "B"):
        e = ev[ev.v == v]
        out.append(report(f"{v} all", e))
        cut = e[e.t < IS_END].rel20.quantile(0.8)
        out.append(report(f"{v} wide (rel20 >= {cut:.2f})", e[e.rel20 >= cut]))
    pd.set_option("display.width", 200)
    t = pd.DataFrame(out).set_index("variant").round(3)
    print("mean R per event after measured costs; gross = before costs; 2018-22 only")
    print(t[["n_is", "R_is", "gross_is"]].to_string())
    pick = PICK
    if pick:
        print(f"\n2023-25, looked at once for {pick!r}:")
        print(t[t.index.str.startswith(pick)].to_string())


if __name__ == "__main__":
    main()
