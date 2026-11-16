"""Vectorised screen of cross-market TSMOM designs, IN-SAMPLE ONLY (<= 2020-07-01).

Idea 771bd086. Every design scored here is a trial and is counted in the run notes.
Positions are set on the first daily bar of each month from closed data and held through
the month (next-bar P&L, no look-ahead); notional is a fraction of equity sized to a vol
budget. Costs: half-spread per unit turnover + financing on |notional| per calendar day
(same rates as strategies/cross_market_trend/xm_tsmom.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path[:0] = ["research/trend_following", "strategies/cross_market_trend", "."]
from basket_structure import closes  # noqa: E402
from xm_tsmom import ADMIN_FEE, FX, FX_MARKUP, SPREAD, USD_RATE  # noqa: E402

IS_END = "2020-07-01"
IDX = ["US500", "US2000", "GER40", "UK100", "JPN225", "AUS200", "HK50", "EUSTX50"]
MET = ["XAUUSD", "XAGUSD", "XPTUSD"]
SLEEVES = {"index": IDX, "metal": MET, "fx": sorted(FX)}


def usd_rate(index: pd.DatetimeIndex) -> pd.Series:
    s = pd.Series({pd.Timestamp(d, tz="UTC"): r for d, r in USD_RATE}).sort_index()
    return s.reindex(index.union(s.index)).ffill().reindex(index)


def run(px: pd.DataFrame, signal: pd.DataFrame, *, sleeve_parity: bool, leg_vol=0.025,
        sleeve_vol=0.06, costs=True, long_only: set[str] = frozenset()) -> pd.Series:
    r = px.pct_change()
    vol = np.log(px).diff().rolling(60).std() * np.sqrt(252)
    month_start = px.index.to_series().dt.to_period("M").ne(
        px.index.to_series().dt.to_period("M").shift())
    sig = signal.where(month_start).ffill()
    for t in long_only:
        sig[t] = sig[t].clip(lower=0)
    if sleeve_parity:
        w = pd.DataFrame(0.0, index=px.index, columns=px.columns)
        for cols in SLEEVES.values():
            live = sig[cols].notna() & vol[cols].notna()
            n = live.sum(axis=1).replace(0, np.nan)
            w[cols] = (sleeve_vol / np.sqrt(n)).values[:, None] * live
        target = w / vol
    else:
        target = leg_vol / vol
    target = target.where(month_start).ffill()
    pos = (sig * target).shift(1)                       # set on signal bar, earn from next
    pnl = (pos * r).sum(axis=1)
    if costs:
        days = px.index.to_series().diff().dt.days.fillna(1).clip(upper=4)
        rate = usd_rate(px.index)
        fin = pd.DataFrame(0.0, index=px.index, columns=px.columns)
        for t in px.columns:
            p = pos[t]
            if t in FX:
                fr = pd.Series(FX_MARKUP, index=px.index)
                fin[t] = p.abs() * fr
            else:
                fin[t] = np.where(p > 0, p * (rate + ADMIN_FEE), -p * (ADMIN_FEE - rate))
        fin = fin.mul(days / 365, axis=0)
        half = pd.Series({t: SPREAD[t] / 2 for t in px.columns}) / px
        turn = pos.diff().abs() * half
        pnl = pnl - fin.sum(axis=1) - turn.sum(axis=1).fillna(0)
    return pnl.fillna(0)


def stats(pnl: pd.Series, name: str) -> dict:
    p = pnl[pnl.index >= "2007-01-01"]
    ann = p.mean() * 252
    vol = p.std() * np.sqrt(252)
    eq = (1 + p).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    yrs = p.groupby(p.index.year).sum()
    return dict(design=name, ret=round(ann * 100, 1), vol=round(vol * 100, 1),
                sharpe=round(ann / vol, 2) if vol else 0, maxdd=round(dd * 100, 1),
                pos_years=f"{(yrs > 0).sum()}/{len(yrs)}")


def tsmom(px, n):
    return np.sign(np.log(px).diff(n))


if __name__ == "__main__":
    px = closes()
    px = px[px.index < IS_END].ffill(limit=3)
    s12 = tsmom(px, 252)
    blend = (tsmom(px, 63) + tsmom(px, 126) + tsmom(px, 252)) / 3
    rows = []
    designs = {
        "T1 baseline equal-leg 12m (replica)": dict(signal=s12, sleeve_parity=False),
        "T2 sleeve-parity 12m": dict(signal=s12, sleeve_parity=True),
        "T3 sleeve-parity 3/6/12m blend": dict(signal=blend, sleeve_parity=True),
        "T4 sleeve-parity 12m, indices long/flat": dict(signal=s12, sleeve_parity=True,
                                                        long_only=set(IDX)),
    }
    for name, kw in designs.items():
        rows.append({**stats(run(px, costs=False, **kw), name + " GROSS")})
        rows.append({**stats(run(px, **kw), name + " NET")})
    pd.set_option("display.width", 200)
    print(pd.DataFrame(rows).to_string(index=False))
    # sleeve-level gross/net for the sleeve-parity 12m design
    print("\nT2 per sleeve, 2007+ (gross / net Sharpe)")
    for sl, cols in SLEEVES.items():
        sub = px[cols]
        g = run(sub, s12[cols], sleeve_parity=False, costs=False)
        n = run(sub, s12[cols], sleeve_parity=False)
        print(f"  {sl:6s} gross {stats(g, '')['sharpe']:>5}  net {stats(n, '')['sharpe']:>5}")
    Path("research/output").mkdir(exist_ok=True)
