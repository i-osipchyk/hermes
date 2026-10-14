"""Walk-forward version of ema_distance_candle_color.py.

Expanding training window, 14-day test blocks. For each block everything is refit on data
strictly BEFORE it (z standardisation, logistic weights, tail thresholds), then scored on the
block. Only the concatenated out-of-sample predictions are reported.

Embargo: the last bar of train is dropped from the fit side (its label is the first test bar's
colour only at the boundary; one bar is enough because features are causal and the target is
the next bar).

Polymarket check: trade the tails of the predicted P(green) (bottom/top TAIL fraction of the
TRAIN score distribution) at an assumed 50c token, taker fee 0.07*p*(1-p) = 1.75c/share,
no spread. Break-even hit rate = 51.75%.

Run:  .venv/bin/python research/ema_distance/ema_distance_walkforward.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import ema_distance_candle_color as m

MIN_TRAIN_DAYS = 60
TEST_DAYS = 14
TAIL = 0.10
PRICE = 0.50
FEE = 0.07 * PRICE * (1 - PRICE)  # per share, USDC
COLS = [f"z{f}" for f in m.FASTS]


def main() -> None:
    df = m.build(m.load())
    day0 = df.index[0].normalize()
    starts = pd.date_range(day0 + pd.Timedelta(days=MIN_TRAIN_DAYS), df.index[-1], freq=f"{TEST_DAYS}D")
    preds = []
    for t0 in starts:
        t1 = t0 + pd.Timedelta(days=TEST_DAYS)
        tr, te = df[df.index < t0].iloc[:-1], df[(df.index >= t0) & (df.index < t1)]
        if len(te) < 500:
            continue
        mu, sd = tr[COLS].mean().values, tr[COLS].std().values
        w = m.logit_fit((tr[COLS].values - mu) / sd, tr.green.values)
        s_tr = m.logit_pred(w, (tr[COLS].values - mu) / sd)
        lo, hi = np.quantile(s_tr, [TAIL, 1 - TAIL])
        s_te = m.logit_pred(w, (te[COLS].values - mu) / sd)
        preds.append(pd.DataFrame({
            "fold": t0.strftime("%m-%d"), "score": s_te, "lo": lo, "hi": hi,
            "green": te.green.values, "ret_bps": te.ret_bps.values,
            "btc": te.close.iloc[-1] / te.close.iloc[0] - 1,
            "z8": te.z8.values,
        }, index=te.index))
    P = pd.concat(preds)
    y = P.green.values
    print(f"folds={P.fold.nunique()}  OOS bars={len(P)}  {P.index[0]:%Y-%m-%d} -> {P.index[-1]:%Y-%m-%d}")
    print(f"base P(green)={y.mean():.4f}")
    print(f"\nWalk-forward OOS AUC (logistic, 5 z): {m.auc(P.score.values, y):.4f}")
    print(f"8/32 alone (sign-flipped z8) AUC:      {m.auc(-P.z8.values, y):.4f}")

    # tail rule: predicted red when score <= lo, green when >= hi
    side = np.where(P.score <= P.lo, -1, np.where(P.score >= P.hi, 1, 0))
    P["side"] = side
    P["win"] = np.where(side == 1, y == 1, np.where(side == -1, y == 0, np.nan))
    tr_ = P[P.side != 0]
    be = PRICE + FEE
    print(f"\n== tail rule (top/bottom {TAIL:.0%} of train scores), price {PRICE:.2f}, fee {FEE*100:.2f}c ==")
    print(f"trades={len(tr_)}  hit={tr_.win.mean():.4f}  break-even={be:.4f}")
    ev = tr_.win.mean() - PRICE - FEE
    print(f"EV/share (no spread) = {ev*100:+.2f}c  = {ev/PRICE:+.2%} of stake")
    for nm, g in (("predict RED", tr_[tr_.side == -1]), ("predict GREEN", tr_[tr_.side == 1])):
        print(f"  {nm}: n={len(g)} hit={g.win.mean():.4f}")
    # bootstrap CI by fold-block resampling (autocorrelation-safe-ish)
    rng = np.random.default_rng(0)
    folds = {k: g.win.values for k, g in tr_.groupby("fold")}
    keys = list(folds)
    bs = []
    for _ in range(5000):
        pick = rng.choice(keys, len(keys))
        bs.append(np.concatenate([folds[k] for k in pick]).mean())
    print(f"hit 95% CI (fold bootstrap): {np.percentile(bs, 2.5):.4f} .. {np.percentile(bs, 97.5):.4f}")

    print("\n== per fold ==")
    rows = []
    for k, g in P.groupby("fold", sort=False):
        t = g[g.side != 0]
        rows.append({"fold": k, "btc_ret": g.btc.iloc[0], "n": len(g), "auc": m.auc(g.score.values, g.green.values),
                     "tail_n": len(t), "tail_hit": t.win.mean() if len(t) else np.nan,
                     "ev_c": (t.win.mean() - be) * 100 if len(t) else np.nan})
    f = pd.DataFrame(rows)
    print(f.round(4).to_string(index=False))
    print(f"\nfolds with AUC>0.5: {(f.auc > .5).sum()}/{len(f)};  tail hit>break-even: {(f.tail_hit > be).sum()}/{len(f)}")

    # dose-response: OOS P(green) by score decile (cut on train-score edges is overkill; use pooled deciles)
    d = pd.qcut(P.score, 10, labels=False)
    print("\n== OOS P(green) by predicted-score decile ==")
    print(P.groupby(d).green.agg(["size", "mean"]).round(4).T.to_string())
    out = Path(__file__).resolve().parents[1] / "output" / "ema_distance_candle_color"
    out.mkdir(parents=True, exist_ok=True)
    P.to_csv(out / "walkforward_preds.csv")


if __name__ == "__main__":
    main()
