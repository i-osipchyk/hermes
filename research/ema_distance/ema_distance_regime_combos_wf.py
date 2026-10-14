"""Walk-forward test of regime COMBINATIONS of the five volatility-scaled EMA distances.

Same folds as ema_distance_walkforward.py (expanding train, 14-day blocks). Per fold, on TRAIN only:
  * each pair's z gets tercile thresholds -> state dn/fl/up;
  * every pair-of-pairs 3x3 cell with n >= MIN_CELL gets a shrunk P(green);
  * score A "cells"  = mean P(green) over the 10 cells the bar falls in (one per pair-of-pairs);
  * score B "align"  = (#pairs up) - (#pairs down), integer -5..+5, mapped to its train P(green);
  * score C "logit"  = benchmark from ema_distance_walkforward.py.
Tail rule: trade bottom/top TAIL of each score's TRAIN distribution at 50c, taker fee 1.75c.
Also reports "selected cells": only cells with train binomial p < 0.01, bar must match >=1 and
all matched selected cells must agree on side.

Run:  .venv/bin/python research/ema_distance/ema_distance_regime_combos_wf.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import ema_distance_candle_color as m

MIN_TRAIN_DAYS, TEST_DAYS, TAIL = 60, 14, 0.10
MIN_CELL, SHRINK = 300, 200  # SHRINK pseudo-counts toward the train base rate
FEE = 0.07 * 0.5 * 0.5
BE = 0.5 + FEE
PAIRS = list(itertools.combinations(m.FASTS, 2))
COLS = [f"z{f}" for f in m.FASTS]


def states(part: pd.DataFrame, terc: dict) -> np.ndarray:
    return np.column_stack([np.digitize(part[f"z{f}"].values, terc[f]) for f in m.FASTS])


def cell_table(S: np.ndarray, y: np.ndarray, base: float) -> dict:
    tbl = {}
    for (i, _a), (j, _b) in itertools.combinations(enumerate(m.FASTS), 2):
        for sa, sb in itertools.product(range(3), repeat=2):
            mask = (S[:, i] == sa) & (S[:, j] == sb)
            n = int(mask.sum())
            if n < MIN_CELL:
                continue
            k = y[mask].sum()
            tbl[(i, j, sa, sb)] = ((k + SHRINK * base) / (n + SHRINK), m.binom_p(k, n, base))
    return tbl


def cell_scores(S: np.ndarray, tbl: dict, base: float):
    n = len(S)
    avg = np.zeros(n)
    cnt = np.zeros(n)
    sel_pos, sel_neg = np.zeros(n), np.zeros(n)
    for (i, j, sa, sb), (p, pv) in tbl.items():
        mask = (S[:, i] == sa) & (S[:, j] == sb)
        avg[mask] += p
        cnt[mask] += 1
        if pv < 0.01:
            (sel_pos if p > base else sel_neg)[mask] += 1
    avg = np.where(cnt > 0, avg / np.maximum(cnt, 1), base)
    # selected-cells signal: +1 all matched selected cells say green, -1 all say red, else 0
    sel = np.where((sel_pos > 0) & (sel_neg == 0), 1, np.where((sel_neg > 0) & (sel_pos == 0), -1, 0))
    return avg, sel


def evaluate(name: str, side: np.ndarray, y: np.ndarray, folds: np.ndarray) -> dict:
    t = side != 0
    win = np.where(side == 1, y == 1, y == 0)[t]
    fk = folds[t]
    per = pd.Series(win).groupby(fk).mean()
    return {"rule": name, "trades": int(t.sum()), "hit": win.mean(),
            "ev_c": (win.mean() - BE) * 100,
            "folds>BE": f"{int((per > BE).sum())}/{len(per)}",
            "min_fold_hit": per.min()}


def main() -> None:
    df = m.build(m.load())
    day0 = df.index[0].normalize()
    starts = pd.date_range(day0 + pd.Timedelta(days=MIN_TRAIN_DAYS), df.index[-1], freq=f"{TEST_DAYS}D")
    rec = []
    for t0 in starts:
        t1 = t0 + pd.Timedelta(days=TEST_DAYS)
        tr, te = df[df.index < t0].iloc[:-1], df[(df.index >= t0) & (df.index < t1)]
        if len(te) < 500:
            continue
        yt, ye = tr.green.values, te.green.values
        base = yt.mean()
        terc = {f: tr[f"z{f}"].quantile([1 / 3, 2 / 3]).values for f in m.FASTS}
        St, Se = states(tr, terc), states(te, terc)

        # A: cell-average score
        tbl = cell_table(St, yt, base)
        At, _ = cell_scores(St, tbl, base)
        Ae, sel = cell_scores(Se, tbl, base)
        lo, hi = np.quantile(At, [TAIL, 1 - TAIL])
        sideA = np.where(Ae <= lo, -1, np.where(Ae >= hi, 1, 0))

        # B: alignment -> train P(green)
        al_t = (St == 2).sum(1) - (St == 0).sum(1)
        al_e = (Se == 2).sum(1) - (Se == 0).sum(1)
        pmap = {k: yt[al_t == k].mean() if (al_t == k).sum() >= MIN_CELL else base for k in range(-5, 6)}
        Bt, Be = np.array([pmap[k] for k in al_t]), np.array([pmap[k] for k in al_e])
        loB, hiB = np.quantile(Bt, [TAIL, 1 - TAIL])
        sideB = np.where(Be <= loB, -1, np.where(Be >= hiB, 1, 0))

        # C: logistic benchmark
        mu, sd = tr[COLS].mean().values, tr[COLS].std().values
        w = m.logit_fit((tr[COLS].values - mu) / sd, yt)
        Ct = m.logit_pred(w, (tr[COLS].values - mu) / sd)
        Ce = m.logit_pred(w, (te[COLS].values - mu) / sd)
        loC, hiC = np.quantile(Ct, [TAIL, 1 - TAIL])
        sideC = np.where(Ce <= loC, -1, np.where(Ce >= hiC, 1, 0))

        rec.append(pd.DataFrame({
            "fold": t0.strftime("%m-%d"), "y": ye, "A": Ae, "B": Be, "C": Ce,
            "sideA": sideA, "sideB": sideB, "sideC": sideC, "sideSel": sel,
            "nsel": len(tbl)}, index=te.index))
    P = pd.concat(rec)
    y, folds = P.y.values, P.fold.values
    print(f"folds={P.fold.nunique()} OOS bars={len(P)} base P(green)={y.mean():.4f}  "
          f"break-even={BE:.4f} (50c + {FEE*100:.2f}c fee)")
    print(f"\nOOS AUC (walk-forward):  cells={m.auc(P.A.values, y):.4f}  "
          f"align={m.auc(P.B.values, y):.4f}  logit={m.auc(P.C.values, y):.4f}")

    rows = [evaluate("A cell-average tails", P.sideA.values, y, folds),
            evaluate("B alignment tails", P.sideB.values, y, folds),
            evaluate("C logistic tails", P.sideC.values, y, folds),
            evaluate("S selected cells (p<.01) agree", P.sideSel.values, y, folds)]
    # agreement: A and C on same side
    both = np.where(P.sideA.values == P.sideC.values, P.sideA.values, 0)
    rows.append(evaluate("A & C same side", both, y, folds))
    print("\n== tail rules, 50c token, taker fee ==")
    print(pd.DataFrame(rows).round(4).to_string(index=False))

    print("\n== per fold AUC ==")
    pf = P.groupby("fold", sort=False).apply(lambda g: pd.Series({
        "A": m.auc(g.A.values, g.y.values), "B": m.auc(g.B.values, g.y.values),
        "C": m.auc(g.C.values, g.y.values)}), include_groups=False)
    print(pf.round(4).to_string())
    print("\ncorr(A,C) =", round(np.corrcoef(P.A, P.C)[0, 1], 3))


if __name__ == "__main__":
    main()
