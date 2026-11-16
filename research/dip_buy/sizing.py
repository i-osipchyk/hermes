"""Position size / leverage vs portfolio volatility (idea 6f4f38e2), IS 2011-22.

Book rule unchanged; only how much capital each signal gets. Swap 6.35%/yr on open notional
(Pepperstone long share-CFD rate, 2026-10) and 5 bp a side.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import dip_study as d  # noqa: E402

CASES = [  # label, slots, size, max_gross
    ("book 10 x 10%", 10, 0.10, 1.0),
    ("10 x 15%, gross 1.5", 10, 0.15, 1.5),
    ("10 x 20%, gross 2", 10, 0.20, 2.0),
    ("10 x 30%, gross 3", 10, 0.30, 3.0),
    ("5 x 20%, gross 1", 5, 0.20, 1.0),
    ("5 x 40%, gross 2", 5, 0.40, 2.0),
    ("3 x 33%, gross 1", 3, 0.333, 1.0),
]

if __name__ == "__main__":
    window = d.WINDOWS[sys.argv[1] if len(sys.argv) > 1 else "is"]
    panel = d.load_panel()
    base = d.Cfg(cost_bp=5, swap_annual=0.0635)
    rows = []
    for label, slots, size, gross in CASES:
        tr, eq = d.simulate(panel, replace(base, slots=slots, size=size, max_gross=gross), *window)
        r = eq.pct_change().dropna()
        yrs = (eq.index[-1] - eq.index[0]).days / 365.25
        dd = eq / eq.cummax() - 1
        rows.append(dict(
            case=label, trades=len(tr), avg_trade=tr.ret.mean(),
            ann_vol=r.std() * np.sqrt(252), cagr=eq.iloc[-1] ** (1 / yrs) - 1,
            sharpe=r.mean() / r.std() * np.sqrt(252), maxdd=dd.min(), worst_day=r.min(),
            worst_month=eq.resample("ME").last().pct_change().min(),
        ))
    out = pd.DataFrame(rows)
    print(out.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
