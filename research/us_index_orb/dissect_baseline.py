"""Dissect the ORB baseline trades (idea 08e23fa6) -- in-sample 2018-2023 only.

Reads a stored run's trades and joins each to the state of its day, computed from the cached
5m bars with information available at the end of the opening range:

    or_ratio    opening-range width / median of the previous 20 opening ranges
    or_atr      opening-range width / mean cash-session range (09:30-16:00) of the previous 14 days
    gap_atr     (09:30 open - previous 16:00 close) / that ATR, signed
    gap_dir     +1 if the break is in the direction of the overnight gap, -1 against, 0 tiny gap
    nth         1 = first entry of the day, 2 = second (the reversal after a stop)
    entry_min   minutes after 09:30 at entry
    prior_trend sign of the previous day's cash-session close vs its 20-day mean (+1/-1),
                times the trade side -> +1 = with trend

and the trade's shape from the bars between entry and exit: MFE / MAE in R.

    .venv/bin/python research/us_index_orb/dissect_baseline.py <run-key> [<run-key> ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
OUT = Path("research/output")
RANGE_MIN = 15


def day_table(ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    b = pd.read_parquet(f".hermes_cache/bars/pepperstone/{ticker}_5m.parquet")
    b["t"] = pd.to_datetime(b.ts, unit="s", utc=True).dt.tz_convert(NY)
    b["d"] = b.t.dt.date
    b["m"] = b.t.dt.hour * 60 + b.t.dt.minute
    cash = b[(b.m >= 570) & (b.m < 960)]
    orr = b[(b.m >= 570) & (b.m < 570 + RANGE_MIN)]
    g = cash.groupby("d")
    days = pd.DataFrame({
        "open": g.open.first(), "close": g.close.last(),
        "hi": g.high.max(), "lo": g.low.min(), "nbars": g.size(),
    })
    o = orr.groupby("d")
    days["or_hi"], days["or_lo"] = o.high.max(), o.low.min()
    days = days[days.nbars >= 60]                       # full cash sessions only
    days["or_w"] = days.or_hi - days.or_lo
    days["rng"] = days.hi - days.lo
    days["atr"] = days.rng.shift(1).rolling(14).mean()
    days["or_ratio"] = days.or_w / days.or_w.shift(1).rolling(20).median()
    days["or_atr"] = days.or_w / days.atr
    days["gap_atr"] = (days.open - days.close.shift(1)) / days.atr
    days["trend"] = np.sign(days.close.shift(1) - days.close.shift(1).rolling(20).mean())
    return days, b.set_index("t")[["high", "low", "close"]]


def load_trades(key: str) -> tuple[str, pd.DataFrame]:
    r = json.load(open(f".hermes_cache/runs/{key}/result.json"))
    t = pd.DataFrame(r["trades"])
    ticker = t.symbol.iloc[0].split(":")[1]
    t["entry_time"] = pd.to_datetime(t.entry_time, utc=True).dt.tz_convert(NY)
    t["exit_time"] = pd.to_datetime(t.exit_time, utc=True).dt.tz_convert(NY)
    t["dir"] = np.where(t.side == "buy", 1, -1)
    t["risk"] = (t.entry_price - t.stop_loss).abs() * t["size"]
    t["R"] = t.net_pnl / t.risk
    t["d"] = t.entry_time.dt.date
    t["nth"] = t.groupby("d").cumcount() + 1
    t["entry_min"] = t.entry_time.dt.hour * 60 + t.entry_time.dt.minute - 570
    return ticker, t


def shape(t: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    mfe, mae = [], []
    for row in t.itertuples():
        w = bars.loc[row.entry_time:row.exit_time]
        per = abs(row.entry_price - row.stop_loss)
        if row.dir == 1:
            mfe.append((w.high.max() - row.entry_price) / per)
            mae.append((row.entry_price - w.low.min()) / per)
        else:
            mfe.append((row.entry_price - w.low.min()) / per)
            mae.append((w.high.max() - row.entry_price) / per)
    t["mfe"], t["mae"] = mfe, mae
    return t


def cut(t: pd.DataFrame, col: str, bins=None, q: int | None = None) -> pd.DataFrame:
    if q:
        key = pd.qcut(t[col], q, duplicates="drop")
    else:
        key = pd.cut(t[col], bins) if bins is not None else t[col]
    g = t.groupby(key, observed=True).R
    return pd.DataFrame({"n": g.size(), "mean_R": g.mean().round(3),
                         "win%": (g.apply(lambda x: (x > 0).mean()) * 100).round(1),
                         "sum_R": g.sum().round(1)})


def main(keys: list[str]) -> None:
    for key in keys:
        ticker, t = load_trades(key)
        days, bars = day_table(ticker)
        t = t.join(days[["or_ratio", "or_atr", "gap_atr", "trend", "or_w", "atr"]], on="d")
        t["gap_dir"] = np.where(t.gap_atr.abs() < 0.1, 0, np.sign(t.gap_atr) * t.dir)
        t["with_trend"] = t.trend * t.dir
        t["year"] = t.entry_time.dt.year
        t["weekday"] = t.entry_time.dt.dayofweek
        t["hold_min"] = (t.exit_time - t.entry_time).dt.total_seconds() / 60
        t = shape(t, bars)
        t.to_csv(OUT / f"us_index_orb_trades_{ticker}_{key[:8]}.csv", index=False)
        print(f"\n#################### {ticker} {key}  n={len(t)}  mean R={t.R.mean():.3f}  "
              f"sum R={t.R.sum():.0f}")
        for title, frame in [
            ("side", cut(t, "side")),
            ("nth entry of day", cut(t, "nth")),
            ("year", cut(t, "year")),
            ("opening range / median prev 20 (quintiles)", cut(t, "or_ratio", q=5)),
            ("opening range / ATR14 (quintiles)", cut(t, "or_atr", q=5)),
            ("ATR14 in points (quintiles; vol regime)", cut(t, "atr", q=5)),
            ("gap / ATR signed (quintiles)", cut(t, "gap_atr", q=5)),
            ("break vs gap direction", cut(t, "gap_dir")),
            ("with prior 20d trend (+1) / against (-1)", cut(t, "with_trend")),
            ("entry minutes after 09:30", cut(t, "entry_min", bins=[0, 30, 60, 120, 240, 400])),
            ("weekday", cut(t, "weekday")),
            ("exit reason", cut(t, "exit_reason")),
        ]:
            print(f"\n-- {title}\n{frame.to_string()}")
        print("\n-- shape: MFE/MAE in R")
        print(t.groupby(t.R > 0)[["mfe", "mae", "hold_min"]].describe().T.round(2).to_string())
        stopped = t[t.exit_reason == "stop_loss"]
        print(f"\nstopped trades: {len(stopped)}, of which MFE >= 1R first: "
              f"{(stopped.mfe >= 1).mean():.1%}, >= 2R: {(stopped.mfe >= 2).mean():.1%}")
        win = t[t.R > 0]
        print(f"winners exit R median {win.R.median():.2f}; winners with MFE >= 2x final R: "
              f"{(win.mfe >= 2 * win.R).mean():.1%}")
        top = t.R.sort_values(ascending=False)
        print(f"top 5% of trades = {top.head(len(t)//20).sum() / t.R.sum():.0%} of net R")


if __name__ == "__main__":
    main(sys.argv[1:])
