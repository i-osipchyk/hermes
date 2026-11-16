"""Fetch Pepperstone daily bars for the cross-market trend basket and report depth / roll gaps.

Idea 771bd086. Output: research/output/trend_basket_depth.csv
"""
from __future__ import annotations

from datetime import UTC, datetime

import dotenv

dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True))

import pandas as pd  # noqa: E402

from hermes import Symbol, Timeframe  # noqa: E402
from hermes.data import PepperstoneSource  # noqa: E402

CANDIDATES = [
    "US500", "US2000", "GER40", "UK100", "JPN225", "AUS200", "HK50", "EUSTX50",
    "USTN10YR", "USTN5YR", "USTN2YR",
    "XAUUSD", "XAGUSD", "XPTUSD", "Copper",
    "SpotCrude", "SpotBrent", "NatGas",
    "Corn", "Wheat", "Soybeans", "Sugar", "Coffee", "Cocoa", "Cotton",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
]
D1 = Timeframe.parse("1d")

if __name__ == "__main__":
    src = PepperstoneSource()
    rows = []
    for t in CANDIDATES:
        try:
            inst = src.get_instrument(Symbol(t, "pepperstone"))
            bars = src.history(inst, D1, datetime(2005, 1, 1, tzinfo=UTC),
                               datetime(2026, 10, 13, tzinfo=UTC))
        except Exception as e:  # report and carry on
            print(t, "ERROR", e)
            continue
        c = pd.Series([b.close for b in bars], index=[b.timestamp for b in bars])
        o = pd.Series([b.open for b in bars], index=c.index)
        gap = (o / c.shift(1) - 1).abs()
        ret = c.pct_change().abs()
        rows.append(dict(
            ticker=t, n=len(bars), first=c.index[0].date(), last=c.index[-1].date(),
            tick=inst.tick_size, lot=inst.lot_size, quote=inst.quote_currency,
            max_gap=round(gap.max(), 4), gaps_over_3pct=int((gap > 0.03).sum()),
            med_absret=round(ret.median(), 4),
        ))
        print(rows[-1], flush=True)
    pd.DataFrame(rows).to_csv("research/output/trend_basket_depth.csv", index=False)
