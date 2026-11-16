"""Diagnosis (no trial): does the baseline's gross edge live in high-vol regimes? IS only.

Splits each leg's daily baseline (T1, 12m sign, equal vol) P&L by whether the leg's 60d vol
on the signal date is above its own trailing 3-year median (past data only).  Idea 771bd086.
"""
from __future__ import annotations

import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path[:0] = ["research/trend_following"]
from screen import IS_END, SLEEVES, closes, run, tsmom  # noqa: E402

if __name__ == "__main__":
    px = closes()
    px = px[px.index < IS_END].ffill(limit=3)
    s = tsmom(px, 252)
    vol = np.log(px).diff().rolling(60).std()
    hi = (vol > vol.rolling(756, min_periods=252).median()).shift(1)   # known before the bar
    rows = []
    for sl, cols in SLEEVES.items():
        for t in cols:
            g = run(px[[t]], s[[t]], sleeve_parity=False, costs=False)
            n = run(px[[t]], s[[t]], sleeve_parity=False)
            for regime, mask in (("high", hi[t] == True), ("low", hi[t] == False)):  # noqa: E712
                m = mask & (g.index >= "2007-01-01") & (g != 0)
                rows.append(dict(sleeve=sl, ticker=t, regime=regime, days=int(m.sum()),
                                 gross=g[m].sum(), net=n[m].sum(), sq=(g[m] ** 2).sum()))
    d = pd.DataFrame(rows)
    agg = d.groupby(["sleeve", "regime"]).agg(days=("days", "sum"), gross=("gross", "sum"),
                                             net=("net", "sum"), sq=("sq", "sum"))
    # per-leg-day Sharpe-like ratio: mean / sd of the pooled leg-day returns, annualised
    agg["gross_sr"] = (agg.gross / agg.days) / np.sqrt(agg.sq / agg.days) * np.sqrt(252)
    agg["net_sr"] = (agg.net / agg.days) / np.sqrt(agg.sq / agg.days) * np.sqrt(252)
    print(agg[["days", "gross_sr", "net_sr"]].round(2))
    tot = d.groupby("regime")[["days", "gross", "net", "sq"]].sum()
    tot["gross_sr"] = (tot.gross / tot.days) / np.sqrt(tot.sq / tot.days) * np.sqrt(252)
    tot["net_sr"] = (tot.net / tot.days) / np.sqrt(tot.sq / tot.days) * np.sqrt(252)
    print(tot[["days", "gross_sr", "net_sr"]].round(2))
