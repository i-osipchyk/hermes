"""Correlation structure of the xm_trend basket and the baseline's realised vol (IS only).

Idea 771bd086. Diagnosis, not a trial: scores no variant.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

IS_END = "2020-07-01"
TICKERS = json.loads(Path("tickers/xm_trend.json").read_text())["tickers"]
FX = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"]
IDX = TICKERS[:8]


def closes() -> pd.DataFrame:
    out = {}
    for t in TICKERS:
        d = pd.read_parquet(f".hermes_cache/bars/pepperstone/{t}_1d.parquet")
        # bars open at 17:00 NY (21/22h UTC); +4h lands each on its trading date
        when = pd.DatetimeIndex(pd.to_datetime(d.ts + 4 * 3600, unit="s", utc=True))
        s = pd.Series(d.close.values, index=when.normalize())
        out[t] = s[~s.index.duplicated()]
    return pd.DataFrame(out)


if __name__ == "__main__":
    px = closes()
    px = px[px.index < IS_END]
    r = np.log(px).diff()
    # express every FX leg as USD-vs-foreign so the sign is comparable
    for t in ("USDJPY", "USDCAD", "USDCHF"):
        r[t] = -r[t]
    r = r.rename(columns={"USDJPY": "JPYUSD", "USDCAD": "CADUSD", "USDCHF": "CHFUSD"})
    fx = ["EURUSD", "GBPUSD", "JPYUSD", "AUDUSD", "CADUSD", "CHFUSD", "NZDUSD"]
    rr = r["2015-07":]
    c = rr.corr()

    def avg(cols):
        m = c.loc[cols, cols].values
        return m[np.triu_indices(len(cols), 1)].mean()

    print("avg pairwise corr, 2015-07..IS end: FX", round(avg(fx), 2), " indices",
          round(avg(IDX), 2), " metals", round(avg(["XAUUSD", "XAGUSD", "XPTUSD"]), 2))
    if len(sys.argv) > 1:
        run = next(Path(".hermes_cache/runs").glob(f"{sys.argv[1]}*"))
        eq = pd.DataFrame(json.loads((run / "result.json").read_text())["equity_curve"])
        eq = pd.Series(eq.iloc[:, 1].values, index=pd.to_datetime(eq.iloc[:, 0], utc=True))
        d = eq.resample("D").last().dropna().pct_change().dropna()
        print("realised portfolio vol (ann):", round(d.std() * np.sqrt(252), 3),
              " 2015-07+:", round(d["2015-07":].std() * np.sqrt(252), 3))
