"""One-at-a-time parameter sensitivity around the book rule (idea 6f4f38e2), IS 2011-22 only.

Every constant is moved alone with the rest at book values; the rule is not re-chosen from
this. Output: research/output/dip_buy/sensitivity.csv
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import dip_study as d  # noqa: E402

GRID = {
    "drop": [-0.02, -0.025, -0.03, -0.035, -0.04, -0.05],
    "vol_min": [2.0, 2.5, 3.0, 3.5, 4.0],
    "depth": [0.5, 0.7, 0.9, 1.1, 1.3],
    "target_atr": [0.25, 0.5, 0.75, 1.0, 1.5],
    "max_hold": [3, 5, 10, 15, 20],
    "atr_len": [3, 5, 7, 10, 14],
    "sma_len": [0, 100, 150, 200, 250],
    "slots": [5, 10, 15, 20],
}

if __name__ == "__main__":
    panel = d.load_panel()
    base = d.Cfg(cost_bp=5)
    rows = []
    for name, values in GRID.items():
        for v in values:
            cfg = replace(base, **{name: v})
            if name == "sma_len" and v == 0:
                cfg = replace(cfg, trend=False)
            tr, eq = d.simulate(panel, cfg, *d.WINDOWS["is"])
            out = d.summarize(tr, eq, f"{name}={v}")
            rows.append(dict(param=name, value=v, book=getattr(base, name) == v, **out))
    pd.DataFrame(rows).to_csv(d.OUT / "sensitivity.csv", index=False)
