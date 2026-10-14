"""Random-direction control for the ORB (idea 08e23fa6) -- in-sample trades only.

Does the *direction* of the opening-range break carry information, or would any trade with
the same timing and stop geometry on the same days have made the money (volatility + drift)?

For each real trade: same entry bar and price, same stop distance, same flat time (15:55 or
the stored exit if it was the flatten), but a coin-flip side. Simulated on the 5m bars with the
engine's conventions: the entry bar can stop out (stop-first), a stop is hit when a bar's range
touches it, otherwise exit at the real trade's flatten price -- no target. The spread is charged
as a fraction of risk, the same for both. 500 repetitions -> distribution of mean R.

    .venv/bin/python research/us_index_orb/random_direction_control.py <trades.csv> ...
"""

from __future__ import annotations

import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
SPREAD = {"US500": 0.4, "NAS100": 1.0}
N_REP = 500


def main(paths: list[str]) -> None:
    for path in paths:
        t = pd.read_csv(path, parse_dates=["entry_time", "exit_time"])
        ticker = "US500" if "US500" in path else "NAS100"
        b = pd.read_parquet(f".hermes_cache/bars/pepperstone/{ticker}_5m.parquet")
        b.index = pd.to_datetime(b.ts, unit="s", utc=True)
        t["entry_time"] = pd.to_datetime(t.entry_time, utc=True)
        t["exit_time"] = pd.to_datetime(t.exit_time, utc=True)
        # flatten time = the stored exit for signal exits, else 15:55 NY that day
        flat = []
        for r in t.itertuples():
            if r.exit_reason == "signal":
                flat.append(r.exit_time)
            else:
                d = r.entry_time.tz_convert(NY).date()
                flat.append(pd.Timestamp(f"{d} 15:55", tz=NY).tz_convert("UTC"))
        t["flat"] = flat
        # per trade, outcome in R for long and for short (deterministic given side)
        out = np.zeros((len(t), 2))
        for i, r in enumerate(t.itertuples()):
            per = abs(r.entry_price - r.stop_loss)
            w = b.loc[r.entry_time:r.flat]
            if w.empty:
                out[i] = np.nan
                continue
            exit_px = w.open.iloc[-1] if w.index[-1] == r.flat else w.close.iloc[-1]
            for j, side in enumerate((1, -1)):
                stop = r.entry_price - side * per
                hit = (w.low <= stop) if side == 1 else (w.high >= stop)
                px = stop if hit.iloc[:-1].any() or (len(w) == 1 and hit.iloc[0]) else exit_px
                out[i, j] = side * (px - r.entry_price) / per - SPREAD[ticker] / per
        ok = ~np.isnan(out).any(axis=1)
        out, real_side = out[ok], np.where(t.side[ok] == "buy", 0, 1)
        real = out[np.arange(len(out)), real_side].mean()
        rng = np.random.default_rng(7)
        reps = np.array([out[np.arange(len(out)), rng.integers(0, 2, len(out))].mean()
                         for _ in range(N_REP)])
        always_long = out[:, 0].mean()
        print(f"{ticker} {path.split('_')[-1][:8]} n={len(out)}  real-rule mean R (re-simulated) "
              f"{real:.3f}  | random side: p50 {np.median(reps):.3f} p95 {np.quantile(reps, .95):.3f} "
              f"max {reps.max():.3f} -> real beats {np.mean(reps < real):.1%} of draws | "
              f"always-long {always_long:.3f}, always-short {out[:, 1].mean():.3f}")


if __name__ == "__main__":
    main(sys.argv[1:])
