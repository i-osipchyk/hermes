"""Gold/silver ratio mean reversion, event study (idea f96bcab0).

Daily broker-day bars (17:00 New York) for XAUUSD and XAGUSD from the cached 1m data.
  ratio    r = ln(gold close) - ln(silver close)
  z        (r - rolling mean_L) / rolling sd_L, both over the previous L days INCLUDING today's
           close (the decision is taken after the close, the trade at the next day's open)
  entry    z >= +z_in: short gold / long silver (ratio expected to fall); z <= -z_in: the reverse
  exit     when z crosses 0 (decided at a close, executed at the next open); no stop
  sizing   dollar-neutral: equal notional per leg, P&L in % of ONE leg's notional

Costs per round trip, per leg: gold spread $0.15 (2018-22) / $0.17 (2023-25) + $0.07/oz commission;
silver spread $0.012 / $0.018, no commission (measured, research/tokyo/FINDINGS.md).
Swap: long leg pays FedFunds + 3%/yr, short leg receives FedFunds - 2%/yr -> the pair pays ~5%/yr
of one leg's notional per calendar day held.

Grid (declared up front): L in {20, 60, 120}, z_in in {1.5, 2.0, 2.5}. Choose by net Sharpe on
2018-22 among cells with >= 15 trades; 2023-25 is looked at once for the chosen cell; 2026 untouched.

Run:  .venv/bin/python research/gold_silver_ratio/ratio_reversion_study.py              # 2018-22
      .venv/bin/python research/gold_silver_ratio/ratio_reversion_study.py 60 2.0       # + 2023-25
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research/silver"))
import silver_breakout_screen as sbs  # noqa: E402

OUT = ROOT / "research/output"
IS_END = pd.Timestamp("2023-01-01", tz="UTC")
END = pd.Timestamp("2026-01-01", tz="UTC")
PAIR_CARRY = 0.05            # per year, of one leg's notional
GRID = [(L, z) for L in (20, 60, 120) for z in (1.5, 2.0, 2.5)]


def bars() -> pd.DataFrame:
    g, s = sbs.daily(sbs.load_1m("XAUUSD")), sbs.daily(sbs.load_1m("XAGUSD"))
    for x in (g, s):                                                   # label = broker date
        x.index = pd.DatetimeIndex((x.index.tz_convert("America/New_York")
                                    + pd.Timedelta(hours=7)).date).tz_localize("UTC")
    d = pd.concat({"g": g, "s": s}, axis=1).dropna()
    return d[d.index < END]


def costs(t: pd.Timestamp, g_px: float, s_px: float) -> float:
    """Round-trip cost of both legs as a fraction of one leg's notional."""
    is_ = t < IS_END
    g_cost = ((0.15 if is_ else 0.17) + 0.07) / g_px
    s_cost = (0.012 if is_ else 0.018) / s_px
    return g_cost + s_cost


def simulate(d: pd.DataFrame, L: int, z_in: float) -> tuple[pd.DataFrame, pd.Series]:
    r = np.log(d["g"]["close"]) - np.log(d["s"]["close"])
    z = (r - r.rolling(L).mean()) / r.rolling(L).std()
    go, so = d["g"]["open"].to_numpy(), d["s"]["open"].to_numpy()
    gc, sc = d["g"]["close"].to_numpy(), d["s"]["close"].to_numpy()
    ts, zz = d.index, z.to_numpy()
    pnl = np.zeros(len(d))                  # daily P&L, fraction of one leg's notional
    trades, pos, entry = [], 0, None
    for i in range(1, len(d)):
        # mark-to-market of the position held into day i (entered at an earlier open)
        if pos != 0:
            if entry["i"] == i:             # entered at today's open
                gr, sr = gc[i] / go[i] - 1, sc[i] / so[i] - 1
            else:
                gr, sr = gc[i] / gc[i - 1] - 1, sc[i] / sc[i - 1] - 1
            pnl[i] += pos * (gr - sr) - PAIR_CARRY / 365 * (ts[i] - ts[i - 1]).days
        # decisions at day i-1's close are executed at day i's open: handled at the top of i
        if i + 1 >= len(d) or np.isnan(zz[i]):
            continue
        if pos != 0 and np.sign(zz[i]) != np.sign(entry["z"]):
            # exit at the next open: P&L from today's close to tomorrow's open, plus costs
            j = i + 1
            pnl[j] += pos * ((go[j] / gc[i] - 1) - (so[j] / sc[i] - 1)) - costs(ts[j], go[j], so[j])
            trades.append(dict(t=ts[entry["i"]], exit=ts[j], side=pos, days=(ts[j] - ts[entry["i"]]).days,
                               ret=pnl[entry["i"]: j + 1].sum()))
            pos, entry = 0, None
            continue
        if pos == 0 and abs(zz[i]) >= z_in:
            pos = -1 if zz[i] > 0 else 1    # +1 = long gold / short silver (ratio expected up)
            entry = dict(i=i + 1, z=zz[i])
    daily = pd.Series(pnl, index=ts)
    return pd.DataFrame(trades), daily


def stats(tr: pd.DataFrame, daily: pd.Series, lo, hi) -> dict:
    t = tr[(tr.t >= lo) & (tr.t < hi)] if len(tr) else tr
    d = daily[(daily.index >= lo) & (daily.index < hi)]
    eq = (1 + d).cumprod()
    return dict(n=len(t), mean=t.ret.mean() if len(t) else np.nan,
                win=(t.ret > 0).mean() if len(t) else np.nan,
                days=t.days.median() if len(t) else np.nan,
                sharpe=d.mean() / d.std() * np.sqrt(252) if d.std() > 0 else np.nan,
                total=eq.iloc[-1] - 1, max_dd=(eq / eq.cummax() - 1).min())


def main() -> None:
    d = bars()
    start = d.index[0]
    rows = []
    for L, z_in in GRID:
        tr, daily = simulate(d, L, z_in)
        s = stats(tr, daily, start, IS_END)
        rows.append(dict(L=L, z_in=z_in, **{f"{k}_is": v for k, v in s.items()}))
    pd.set_option("display.width", 220)
    print("XAUUSD/XAGUSD ratio reversion, dollar-neutral, net of spreads + commission + swap; 2018-22")
    print(pd.DataFrame(rows).round(4).to_string(index=False))

    if len(sys.argv) == 3:
        L, z_in = int(sys.argv[1]), float(sys.argv[2])
        tr, daily = simulate(d, L, z_in)
        tr.to_parquet(OUT / f"gs_ratio_trades_{L}_{z_in}.parquet")
        daily.to_frame("pnl").to_parquet(OUT / f"gs_ratio_daily_{L}_{z_in}.parquet")
        s = stats(tr, daily, IS_END, END)
        print(f"\n2023-25, looked at once for L={L}, z_in={z_in}:")
        print({k: round(v, 4) for k, v in s.items()})
        t = tr[tr.t >= IS_END]
        print("trades:", t.assign(ret=t.ret.round(4)).to_string(index=False))


if __name__ == "__main__":
    main()
