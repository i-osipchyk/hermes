"""Does the volatility-scaled EMA distance predict the colour of the NEXT 5m candle?

Not a strategy — a predictor study on BTCUSDT perpetual futures (Binance), 5m bars.

Definitions (every number is stated, none is tuned):
  pairs      fast/slow EMA of close: 4/16, 8/32, 16/64, 32/128, 64/256 (slow = 4x fast)
  distance   d = (EMA_fast - EMA_slow) / close
  vol        sigma = rolling std of 5m log returns over 288 bars (24h), known at bar close
  z          z = d / sigma          -> unitless "how many 5m-sigmas apart the EMAs are"
  target     colour of bar t+1: green = close > open, red = close < open (doji dropped)
  feature at bar t uses only bars <= t (EMAs are causal; sigma includes bar t's return).

Split: chronological, first 60% in-sample (IS), last 40% out-of-sample (OOS).
Every threshold / cell choice is fitted on IS and only *scored* on OOS.

Run:  .venv/bin/python research/ema_distance/ema_distance_candle_color.py
Output: research/output/ema_distance_candle_color/*.csv  (not committed)
"""

from __future__ import annotations

import itertools
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hermes import Symbol, Timeframe
from hermes.data import BinanceFuturesSource

START = datetime(2026, 3, 15, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)
FASTS = [4, 8, 16, 32, 64]
VOL_WINDOW = 288
WARMUP = 1024  # 4x the longest slow EMA; EMAs are seeded from the first close
IS_FRAC = 0.6
MIN_CELL = 300
OUT = Path(__file__).resolve().parents[1] / "output" / "ema_distance_candle_color"


def load() -> pd.DataFrame:
    src = BinanceFuturesSource()
    inst = src.get_instrument(Symbol("BTCUSDT", "binance-futures"))
    bars = src.history(inst, Timeframe.parse("5m"), START, END)
    df = pd.DataFrame(
        {"open": [b.open for b in bars], "close": [b.close for b in bars]},
        index=pd.DatetimeIndex([b.timestamp for b in bars], name="ts"),
    )
    return df


def build(df: pd.DataFrame) -> pd.DataFrame:
    sigma = np.log(df.close).diff().rolling(VOL_WINDOW).std()
    for f in FASTS:
        ef = df.close.ewm(span=f, adjust=False).mean()
        es = df.close.ewm(span=4 * f, adjust=False).mean()
        df[f"z{f}"] = (ef - es) / df.close / sigma
    nxt_open, nxt_close = df.open.shift(-1), df.close.shift(-1)
    df["ret_bps"] = (nxt_close / nxt_open - 1) * 1e4  # next bar's body return
    df["green"] = (nxt_close > nxt_open).astype(float)
    df.loc[nxt_close == nxt_open, "green"] = np.nan  # doji
    df.loc[nxt_close.isna(), "green"] = np.nan
    df = df.iloc[WARMUP:].dropna(subset=["green"] + [f"z{f}" for f in FASTS])
    return df


def auc(score: np.ndarray, y: np.ndarray) -> float:
    r = stats.rankdata(score)
    n1 = y.sum()
    n0 = len(y) - n1
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def binom_p(k: int, n: int, p0: float) -> float:
    return float(stats.binomtest(int(k), int(n), p0).pvalue) if n else float("nan")


def logit_fit(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 25) -> np.ndarray:
    Xb = np.c_[np.ones(len(X)), X]
    w = np.zeros(Xb.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xb @ w))
        g = Xb.T @ (y - p) - l2 * np.r_[0, w[1:]]
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + l2 * np.diag(np.r_[0, np.ones(len(w) - 1)])
        w += np.linalg.solve(H, g)
    return w


def logit_pred(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-(np.c_[np.ones(len(X)), X] @ w)))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    df = build(load())
    cut = int(len(df) * IS_FRAC)
    IS, OOS = df.iloc[:cut], df.iloc[cut:]
    base_is, base_oos = IS.green.mean(), OOS.green.mean()
    print(f"bars={len(df)}  {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}")
    print(f"IS  {IS.index[0]:%m-%d}..{IS.index[-1]:%m-%d} n={len(IS)} P(green)={base_is:.4f}")
    print(f"OOS {OOS.index[0]:%m-%d}..{OOS.index[-1]:%m-%d} n={len(OOS)} P(green)={base_oos:.4f}")
    # BTC drift over the two windows — the 'ranging vs uptrend' check
    for name, part in (("IS", IS), ("OOS", OOS)):
        print(f"  {name} price {part.close.iloc[0]:.0f} -> {part.close.iloc[-1]:.0f} "
              f"({part.close.iloc[-1] / part.close.iloc[0] - 1:+.1%})")

    # ---------- 1. single pair ----------
    rows = []
    terc = {}
    for f in FASTS:
        c = f"z{f}"
        q = IS[c].quantile([1 / 3, 2 / 3]).values  # thresholds fitted on IS only
        terc[f] = q
        row = {"pair": f"{f}/{4 * f}"}
        for name, part, _base in (("IS", IS, base_is), ("OOS", OOS, base_oos)):
            y = part.green.values
            z = part[c].values
            row[f"{name}_auc"] = auc(z, y)
            row[f"{name}_ic"] = stats.spearmanr(z, part.ret_bps)[0]
            # trend-follow sign rule: z>0 -> predict green
            hit = ((z > 0) == (y == 1)).mean()
            row[f"{name}_signhit"] = hit
            row[f"{name}_signhit_p"] = binom_p(((z > 0) == (y == 1)).sum(), len(y), 0.5)
        rows.append(row)
    single = pd.DataFrame(rows)
    single.to_csv(OUT / "single_pair.csv", index=False)
    print("\n== 1. single pair: does z rank next-candle colour? (AUC 0.5 = nothing) ==")
    print(single.round(4).to_string(index=False))

    # tercile bins: P(green) and mean next-bar bps, thresholds from IS
    print("\n== 1b. tercile regimes (IS thresholds): P(green) / mean next-bar bps ==")
    bin_rows = []
    for f in FASTS:
        c = f"z{f}"
        q = terc[f]
        for name, part in (("IS", IS), ("OOS", OOS)):
            st = np.digitize(part[c].values, q)  # 0 low / 1 mid / 2 high
            for s, lab in enumerate(("down", "flat", "up")):
                m = st == s
                bin_rows.append({
                    "pair": f"{f}/{4 * f}", "sample": name, "regime": lab, "n": int(m.sum()),
                    "p_green": part.green.values[m].mean(),
                    "bps": part.ret_bps.values[m].mean(),
                    "p": binom_p(part.green.values[m].sum(), m.sum(),
                                 base_is if name == "IS" else base_oos),
                })
    bins = pd.DataFrame(bin_rows)
    bins.to_csv(OUT / "single_pair_terciles.csv", index=False)
    print(bins.pivot_table(index=["pair", "regime"], columns="sample",
                           values=["p_green", "bps", "p"]).round(4).to_string())

    # ---------- 2. combined regimes ----------
    def states(part: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({f: np.digitize(part[f"z{f}"].values, terc[f]) for f in FASTS},
                            index=part.index)

    sIS, sOOS = states(IS), states(OOS)
    yI, yO = IS.green.values, OOS.green.values

    print("\n== 2a. pairwise regime combos (3x3 grid per pair-of-pairs) ==")
    cells = []
    for a, b in itertools.combinations(FASTS, 2):
        for ia, ib in itertools.product(range(3), repeat=2):
            mI = (sIS[a].values == ia) & (sIS[b].values == ib)
            mO = (sOOS[a].values == ia) & (sOOS[b].values == ib)
            if mI.sum() < MIN_CELL:
                continue
            pI = yI[mI].mean()
            cells.append({
                "combo": f"{a}/{4 * a}={('dn','fl','up')[ia]} & {b}/{4 * b}={('dn','fl','up')[ib]}",
                "nIS": int(mI.sum()), "pIS": pI,
                "p_IS": binom_p(yI[mI].sum(), mI.sum(), base_is),
                "nOOS": int(mO.sum()), "pOOS": yO[mO].mean() if mO.sum() else np.nan,
                "p_OOS_vs_base": binom_p(yO[mO].sum(), mO.sum(), base_oos),
                "side": "green" if pI > base_is else "red",
                "oos_agrees": bool((yO[mO].mean() > base_oos) == (pI > base_is)) if mO.sum() else False,
                "oos_bps": OOS.ret_bps.values[mO].mean() if mO.sum() else np.nan,
            })
    cells = pd.DataFrame(cells)
    cells.to_csv(OUT / "pair_combo_cells.csv", index=False)
    n_cells = len(cells)
    sel = cells[cells.p_IS < 0.05]
    print(f"cells tested={n_cells}; IS-significant (p<.05)={len(sel)} "
          f"(~{0.05 * n_cells:.1f} expected by chance)")
    print(f"of those, OOS direction agrees: {int(sel.oos_agrees.sum())}/{len(sel)}; "
          f"OOS p<.05 in same direction: "
          f"{int(((sel.p_OOS_vs_base < .05) & sel.oos_agrees).sum())}")
    print(sel.sort_values("p_IS").round(4).to_string(index=False))

    print("\n== 2b. all-5-pairs alignment (# of pairs in 'up' minus # in 'down') ==")
    aI = (sIS == 2).sum(axis=1).values - (sIS == 0).sum(axis=1).values
    aO = (sOOS == 2).sum(axis=1).values - (sOOS == 0).sum(axis=1).values
    al = []
    for k in range(-5, 6):
        mI, mO = aI == k, aO == k
        al.append({"align": k, "nIS": int(mI.sum()), "pIS": yI[mI].mean() if mI.sum() else np.nan,
                   "nOOS": int(mO.sum()), "pOOS": yO[mO].mean() if mO.sum() else np.nan,
                   "oos_bps": OOS.ret_bps.values[mO].mean() if mO.sum() else np.nan,
                   "p_OOS": binom_p(yO[mO].sum(), mO.sum(), base_oos)})
    al = pd.DataFrame(al)
    al.to_csv(OUT / "alignment.csv", index=False)
    print(al.round(4).to_string(index=False))
    print(f"AUC of alignment score  IS={auc(aI, yI):.4f}  OOS={auc(aO, yO):.4f}")

    print("\n== 2c. logistic on all five z (fit IS, score OOS) ==")
    cols = [f"z{f}" for f in FASTS]
    mu, sd = IS[cols].mean().values, IS[cols].std().values
    XI, XO = (IS[cols].values - mu) / sd, (OOS[cols].values - mu) / sd
    w = logit_fit(XI, yI)
    pI_, pO_ = logit_pred(w, XI), logit_pred(w, XO)
    print("coefs (std z):", dict(zip(["b0", *cols], w.round(4))))
    print(f"AUC  IS={auc(pI_, yI):.4f}  OOS={auc(pO_, yO):.4f}")
    acc = ((pO_ > 0.5) == (yO == 1)).mean()
    print(f"OOS accuracy @0.5={acc:.4f} vs base-rate-always-guess="
          f"{max(base_oos, 1 - base_oos):.4f}")
    # decile calibration OOS
    dec = pd.qcut(pO_, 10, labels=False, duplicates="drop")
    cal = pd.DataFrame({"dec": dec, "g": yO, "bps": OOS.ret_bps.values}).groupby("dec").agg(
        n=("g", "size"), p_green=("g", "mean"), bps=("bps", "mean"))
    print(cal.round(4).to_string())

    # ---------- 3. stability: monthly OOS-style AUC ----------
    print("\n== 3. AUC by calendar month (all data, z at each pair) ==")
    mon = df.groupby(df.index.to_period("M"))
    m_rows = []
    for per, g in mon:
        r = {"month": str(per), "n": len(g),
             "btc_ret": g.close.iloc[-1] / g.close.iloc[0] - 1}
        for f in FASTS:
            r[f"auc{f}"] = auc(g[f"z{f}"].values, g.green.values)
        m_rows.append(r)
    print(pd.DataFrame(m_rows).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
