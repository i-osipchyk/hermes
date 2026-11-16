"""Dissect a stored xm_tsmom universe run (in-sample only): where does P&L come from?

Usage: .venv/bin/python research/trend_following/dissect.py <run-key>
Idea 771bd086. Writes research/output/xm_trades_<key>.csv
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

FX = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"}
METALS = {"XAUUSD", "XAGUSD", "XPTUSD"}


def load(key: str) -> tuple[pd.DataFrame, pd.Series]:
    run = next(Path(".hermes_cache/runs").glob(f"{key}*"))
    r = json.loads((run / "result.json").read_text())
    t = pd.DataFrame(r["trades"])
    t["ticker"] = t.symbol.str.split(":").str[1]
    t["cls"] = t.ticker.map(lambda x: "fx" if x in FX else "metal" if x in METALS else "index")
    for c in ("entry_time", "exit_time"):
        t[c] = pd.to_datetime(t[c], utc=True)
    t["days"] = (t.exit_time - t.entry_time).dt.days
    t["notional"] = (t["size"] * t.entry_price).abs()
    eq = pd.DataFrame(r["equity_curve"])
    eq = pd.Series(eq.iloc[:, 1].values, index=pd.to_datetime(eq.iloc[:, 0], utc=True))
    return t, eq


if __name__ == "__main__":
    key = sys.argv[1]
    t, eq = load(key)
    t.to_csv(f"research/output/xm_trades_{key}.csv", index=False)
    pd.set_option("display.width", 200)
    print("gross", round(t.gross_pnl.sum()), "costs", round(t.costs.sum()),
          "net", round(t.net_pnl.sum()))
    print("\nby class / side")
    print(t.groupby(["cls", "side"])[["gross_pnl", "costs", "net_pnl"]].sum().round(0))
    print("\nby exit year x class (net)")
    print(t.pivot_table(index=t.exit_time.dt.year, columns="cls", values="net_pnl",
                        aggfunc="sum").round(0))
    yearly = eq.resample("YE").last()
    print("\nequity yearly return %")
    print((yearly.pct_change() * 100).round(1).to_string())
    print("\nholding days", t.days.describe().round(0).to_dict())
    w = t.sort_values("net_pnl")
    print("\nworst 8\n", w.head(8)[["ticker", "side", "entry_time", "exit_time", "notional",
                                       "net_pnl"]])
    print("\nbest 8\n", w.tail(8)[["ticker", "side", "entry_time", "exit_time", "notional",
                                      "net_pnl"]])
