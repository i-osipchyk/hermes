"""Value x momentum diagnosis (idea f62f0c77), in-sample only.

Each quarter, form the top quintile by a value yield (bp, ibp_org) and by 12-1 momentum,
and measure each sleeve's next-quarter excess return over the equal-weight universe
(close of the rebalance day to the next; dividend-adjusted bars). Reports:

* the correlation of the sleeves' quarterly excess returns -- the idea's falsifier is
  rho > -0.2 for value vs momentum;
* mean / volatility / information ratio of each sleeve and of 50/50 combinations;
* the worst momentum quarters and what value did in them (the crash-hedge mechanism).

Long-short (Q1 - Q5) versions are shown too, since the book's evidence is long-short.
Window: rebalances up to 2021-09-30. The holdout is not read.

    .venv/bin/python research/value/value_momentum.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dissect import IS_LAST_REBALANCE, OUT, closes

SLEEVES = ("bp", "ibp_org", "mom")


def main() -> None:
    panel = pd.read_csv(OUT / "panel.csv")
    panel = panel[panel["why"].isna() & (panel["date"] <= IS_LAST_REBALANCE)].copy()
    dates = sorted(panel["date"].unique())
    nxt = dict(zip(dates, [*dates[1:], "2021-12-31"], strict=True))
    px = closes(sorted(panel["ticker"].unique()))

    rets = []
    for d in dates:
        window = px.loc[d:nxt[d]]
        if window.empty or window.index[0] != d:
            continue
        first, last = window.iloc[0], window.ffill().iloc[-1]
        rets.append((last / first - 1).rename(d))
    fwd = pd.DataFrame(rets).stack().rename("ret").reset_index()
    fwd.columns = ["date", "ticker", "ret"]
    panel = panel.merge(fwd, on=["date", "ticker"]).dropna(subset=["ret"])
    panel["xs"] = panel["ret"] - panel.groupby("date")["ret"].transform("mean")

    q1, ls = {}, {}
    for m in SLEEVES:
        s = panel.dropna(subset=[m])
        rank = s.groupby("date")[m].rank(pct=True)
        q1[m] = s[rank > 0.8].groupby("date")["xs"].mean()
        ls[m] = q1[m] - s[rank <= 0.2].groupby("date")["xs"].mean()
    for name, sl in (("top quintile minus EW", q1), ("long-short Q1-Q5", ls)):
        df = pd.DataFrame(sl) * 100
        df["50/50 bp+mom"] = (df["bp"] + df["mom"]) / 2
        df["50/50 ibp_org+mom"] = (df["ibp_org"] + df["mom"]) / 2
        print(f"\n=== {name}, %/quarter, n={len(df)} ===")
        print("correlation:")
        print(df[list(SLEEVES)].corr().round(2).to_string())
        stats = pd.DataFrame({"mean": df.mean(), "vol": df.std(),
                              "IR (ann.)": df.mean() / df.std() * 2,
                              "t": df.mean() / df.std() * np.sqrt(len(df)),
                              "worst qtr": df.min()})
        print(stats.round(2).to_string())
        if name.startswith("top"):
            worst = df.nsmallest(6, "mom")[["mom", "bp", "ibp_org", "50/50 bp+mom"]]
            print("six worst momentum quarters (date = rebalance at start of quarter):")
            print(worst.round(2).to_string())
            df.to_csv(OUT / "value_momentum_quarterly.csv")


if __name__ == "__main__":
    main()
