"""Cut a stored xm_tsmom run's trades by signal strength at entry (IS only; diagnosis).

Strength = |trailing 252d log return| / (60d daily vol * sqrt(252)), i.e. the 12m move in
units of its own annual vol -- a t-stat-like trend strength, read on the entry signal bar.
Idea 771bd086.  Usage: dissect_strength.py <run-key>
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "research/trend_following")
from basket_structure import closes  # noqa: E402
from dissect import load  # noqa: E402

if __name__ == "__main__":
    t, _ = load(sys.argv[1])
    px = closes()
    lr = np.log(px).diff()
    strength = (np.log(px).diff(252)).abs() / (lr.rolling(60).std() * np.sqrt(252))
    # signal bar = last daily bar strictly before the entry fill
    def at(row):
        s = strength[row.ticker].dropna()
        s = s[s.index < row.entry_time.normalize()]
        return s.iloc[-1] if len(s) else np.nan
    t["strength"] = t.apply(at, axis=1)
    t["bucket"] = pd.qcut(t.strength, 4, labels=["q1 weak", "q2", "q3", "q4 strong"])
    g = t.groupby("bucket", observed=True)
    print(pd.DataFrame({
        "n": g.size(), "str_med": g.strength.median().round(2),
        "gross_k": (g.gross_pnl.sum() / 1e3).round(0), "costs_k": (g.costs.sum() / 1e3).round(0),
        "net_k": (g.net_pnl.sum() / 1e3).round(0),
        "win%": (g.net_pnl.apply(lambda x: (x > 0).mean()) * 100).round(0),
        "hold_d": g.days.median(),
    }))
    print("\nby class x bucket (gross_k)")
    print((t.pivot_table(index="bucket", columns="cls", values="gross_pnl", aggfunc="sum",
                         observed=True) / 1e3).round(0))
    print("\nby class x side x bucket (n)")
    print(t.pivot_table(index="bucket", columns=["cls"], values="gross_pnl", aggfunc="size",
                        observed=True))
