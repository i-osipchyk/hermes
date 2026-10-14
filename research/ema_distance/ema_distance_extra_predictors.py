"""Look for a second predictor of next-5m-candle colour, beyond the EMA-distance z's.

Protocol (fixed before looking at OOS):
  1. Build ~25 causal candidate features from bars only (all known at bar t's close).
  2. SCREEN on in-sample (first 60%) only: AUC vs next colour, plus AUC in each of 4 equal IS
     chunks. A feature is SELECTED iff |IS_auc - .5| >= 0.008 AND all 4 chunks agree on sign.
  3. VALIDATE on OOS (last 40%): the same AUC, untouched thresholds.
  4. INCREMENTAL: logistic(5 z) vs logistic(5 z + selected), fit IS, score OOS; then the same
     in a 14-day-block walk-forward with the 50c / 1.75c-fee tail rule.
Categorical time features (minute-of-hour, hour-of-day) are target-encoded from IS only.

Run:  .venv/bin/python research/ema_distance/ema_distance_extra_predictors.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import ema_distance_candle_color as m

from hermes import Symbol, Timeframe
from hermes.data import BinanceFuturesSource

FEE = 0.07 * 0.25
BE = 0.5 + FEE
THRESH, TAIL = 0.008, 0.10
Z = [f"z{f}" for f in m.FASTS]


def load_full() -> pd.DataFrame:
    src = BinanceFuturesSource()
    inst = src.get_instrument(Symbol("BTCUSDT", "binance-futures"))
    bars = src.history(inst, Timeframe.parse("5m"), m.START, m.END)
    return pd.DataFrame(
        {k: [getattr(b, k) for b in bars] for k in ("open", "high", "low", "close", "volume")},
        index=pd.DatetimeIndex([b.timestamp for b in bars], name="ts"))


def features(raw: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c, v = (raw[k] for k in ("open", "high", "low", "close", "volume"))
    r = np.log(c).diff()
    sig = r.rolling(m.VOL_WINDOW).std()
    rng = (h - l).replace(0, np.nan)
    f = pd.DataFrame(index=raw.index)
    # EMA z's (the baseline)
    for p in m.FASTS:
        f[f"z{p}"] = (c.ewm(span=p, adjust=False).mean() - c.ewm(span=4 * p, adjust=False).mean()) / c / sig
    # candle shape of the bar just closed
    f["clv"] = (c - l) / rng - 0.5
    f["body_ratio"] = (c - o) / rng
    f["upper_wick"] = (h - np.maximum(o, c)) / rng
    f["lower_wick"] = (np.minimum(o, c) - l) / rng
    f["range_z"] = (h - l) / c / sig
    # short-term returns, vol scaled
    for k in (1, 2, 3, 6, 12, 36):
        f[f"ret{k}"] = np.log(c / c.shift(k)) / (sig * np.sqrt(k))
    f["ret288"] = np.log(c / c.shift(288)) / (sig * np.sqrt(288))
    # volume
    lv = np.log(v.replace(0, np.nan))
    f["vol_z"] = (lv - lv.rolling(288).mean()) / lv.rolling(288).std()
    f["vol_signed"] = f.vol_z * np.sign(c - o)
    f["vol_trend"] = np.log(v.rolling(12).mean() / v.rolling(288).mean())
    # volatility regime
    f["vol_regime"] = np.log(sig / sig.rolling(2016).median())
    # position in range / anchors
    hi, lo = h.rolling(288).max(), l.rolling(288).min()
    f["pos24h"] = (c - lo) / (hi - lo) - 0.5
    f["dist_vwap24"] = (c / ((c * v).rolling(288).sum() / v.rolling(288).sum()) - 1) / sig
    dayopen = o.groupby(raw.index.normalize()).transform("first")
    f["dist_dayopen"] = (c / dayopen - 1) / sig
    f["dist_ema288"] = (c / c.ewm(span=288, adjust=False).mean() - 1) / sig
    # time (categorical, target-encoded later)
    f["minute"] = raw.index.minute
    f["hour"] = raw.index.hour
    f["dow"] = raw.index.dayofweek
    return f


CATS = ["minute", "hour", "dow"]


def encode(tr: pd.DataFrame, te: pd.DataFrame, col: str, y_tr: np.ndarray, k: float = 100) -> tuple:
    base = y_tr.mean()
    g = pd.DataFrame({"c": tr[col].values, "y": y_tr}).groupby("c").y.agg(["sum", "count"])
    mp = ((g["sum"] + k * base) / (g["count"] + k)).to_dict()
    return tr[col].map(mp).fillna(base).values, te[col].map(mp).fillna(base).values


def main() -> None:
    raw = load_full()
    f = features(raw)
    nxt_o, nxt_c = raw.open.shift(-1), raw.close.shift(-1)
    f["green"] = (nxt_c > nxt_o).astype(float)
    f.loc[(nxt_c == nxt_o) | nxt_c.isna(), "green"] = np.nan
    f = f.iloc[m.WARMUP + 2016:].replace([np.inf, -np.inf], np.nan).dropna()
    cut = int(len(f) * m.IS_FRAC)
    I, O = f.iloc[:cut], f.iloc[cut:]
    yI, yO = I.green.values, O.green.values
    print(f"bars={len(f)}  IS n={len(I)} ({I.index[0]:%m-%d}..{I.index[-1]:%m-%d})  "
          f"OOS n={len(O)} ({O.index[0]:%m-%d}..{O.index[-1]:%m-%d})")

    cands = [c for c in f.columns if c not in ("green", *Z, *CATS)]
    chunks = np.array_split(np.arange(len(I)), 4)
    rows = []
    for c in cands:
        a_is = m.auc(I[c].values, yI)
        ch = [m.auc(I[c].values[ix], yI[ix]) for ix in chunks]
        same = all((x > .5) == (a_is > .5) for x in ch)
        a_oos = m.auc(O[c].values, yO)
        rows.append({"feature": c, "IS_auc": a_is, "chunk_min": min(ch), "chunk_max": max(ch),
                     "selected": abs(a_is - .5) >= THRESH and same, "OOS_auc": a_oos,
                     "OOS_same_side": (a_oos > .5) == (a_is > .5)})
    # categorical time features: encode on IS, AUC of encoded value
    for c in CATS:
        eI, eO = encode(I, O, c, yI)
        a_is, a_oos = m.auc(eI, yI), m.auc(eO, yO)
        ch = [m.auc(eI[ix], yI[ix]) for ix in chunks]
        rows.append({"feature": c + "(enc)", "IS_auc": a_is, "chunk_min": min(ch), "chunk_max": max(ch),
                     "selected": abs(a_is - .5) >= THRESH and all(x > .5 for x in ch),
                     "OOS_auc": a_oos, "OOS_same_side": a_oos > .5})
    S = pd.DataFrame(rows).sort_values("IS_auc", key=lambda s: (s - .5).abs(), ascending=False)
    print(f"\n== screen: {len(S)} candidates; selected iff |IS_auc-.5|>={THRESH} & 4/4 chunks same side ==")
    print(S.round(4).to_string(index=False))
    sel = S[S.selected]
    print(f"\nselected={len(sel)}; of those OOS same side: {int(sel.OOS_same_side.sum())}; "
          f"OOS |auc-.5|>=.005: {int((sel.OOS_auc.sub(.5).abs() >= .005).sum())}")
    chosen = [x for x in sel.feature if not x.endswith("(enc)")]
    print("chosen continuous features:", chosen)
    if not chosen:
        return

    # ---- incremental, fixed split ----
    def fit_eval(cols: list[str]) -> tuple[float, float]:
        mu, sd = I[cols].mean().values, I[cols].std().values
        w = m.logit_fit((I[cols].values - mu) / sd, yI)
        return (m.auc(m.logit_pred(w, (I[cols].values - mu) / sd), yI),
                m.auc(m.logit_pred(w, (O[cols].values - mu) / sd), yO))
    print("\n== incremental, fixed split (fit IS, score OOS) ==")
    for name, cols in (("5 z", Z), ("chosen only", chosen), ("5 z + chosen", Z + chosen)):
        a, b = fit_eval(cols)
        print(f"{name:14s} IS auc={a:.4f}  OOS auc={b:.4f}")

    # ---- incremental, walk-forward ----
    day0 = f.index[0].normalize()
    starts = pd.date_range(day0 + pd.Timedelta(days=m.IS_FRAC and 60), f.index[-1], freq="14D")
    res = {k: [] for k in ("base", "plus")}
    for t0 in starts:
        t1 = t0 + pd.Timedelta(days=14)
        tr, te = f[f.index < t0].iloc[:-1], f[(f.index >= t0) & (f.index < t1)]
        if len(te) < 500:
            continue
        for key, cols in (("base", Z), ("plus", Z + chosen)):
            mu, sd = tr[cols].mean().values, tr[cols].std().values
            w = m.logit_fit((tr[cols].values - mu) / sd, tr.green.values)
            st = m.logit_pred(w, (tr[cols].values - mu) / sd)
            se = m.logit_pred(w, (te[cols].values - mu) / sd)
            lo, hi = np.quantile(st, [TAIL, 1 - TAIL])
            side = np.where(se <= lo, -1, np.where(se >= hi, 1, 0))
            res[key].append(pd.DataFrame({"fold": t0.strftime("%m-%d"), "s": se, "y": te.green.values,
                                          "side": side}, index=te.index))
    print("\n== walk-forward (14d blocks, expanding), tail rule at 50c, fee 1.75c ==")
    for key, lst in res.items():
        P = pd.concat(lst)
        t = P[P.side != 0]
        win = np.where(t.side == 1, t.y == 1, t.y == 0)
        per = pd.Series(win).groupby(t.fold.values).mean()
        aucs = [m.auc(g.s.values, g.y.values) for _, g in P.groupby("fold")]
        print(f"{key:5s} AUC={m.auc(P.s.values, P.y.values):.4f} "
              f"folds>.5={sum(a > .5 for a in aucs)}/{len(aucs)} "
              f"trades={len(t)} hit={win.mean():.4f} EV={((win.mean() - BE) * 100):+.2f}c "
              f"folds>BE={int((per > BE).sum())}/{len(per)} minfold={per.min():.4f}")


if __name__ == "__main__":
    main()
