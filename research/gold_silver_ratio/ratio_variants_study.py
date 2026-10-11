"""Gold/silver ratio: beta-hedged, z-stop and momentum variants (idea f96bcab0, round 2).

Run after the dollar-neutral reversion failed in every cell (ratio_reversion_study.py). Same data,
signal and costs; the protocol is unchanged: choose on 2018-22 only, look at 2023-25 once for the
single chosen cell, 2026 untouched.

Signal  z = (r - mean_L) / sd_L of r = ln(gold) - ln(silver), at a close; trades at the next open.
Legs    silver notional 1; gold notional h (h = 1 dollar-neutral, or beta).
        pos = +1 means long the ratio (long gold h, short silver 1); pos = -1 the reverse.
  V1  beta    reversion (z >= z_in -> pos -1), h = rolling 60-day OLS beta of silver daily log
              returns on gold's, known at the signal close, fixed for the trade
  V2  zstop   reversion, dollar-neutral, plus exit when |z| >= z_stop (3.0 or 3.5); after a
              stop no re-entry until |z| < z_in
  V3  mom     momentum, dollar-neutral: z >= z_in -> pos +1 (go with the stretch)
Exit (all)    z crosses 0.
Costs   round trip per leg (gold scaled by h): measured spreads + gold commission, as in
        ratio_reversion_study.costs; swap per leg per calendar day: long leg pays FF + 3%/yr,
        short leg receives FF - 2%/yr.
P&L     fraction of the silver leg's notional.

~45 cells in total have now been tried on 2018-22 (9 in round 1 + 36 here): the best of that many
noise configurations reaches an in-sample Sharpe of ~0.9, so a winner below that is not evidence.

Run:  .venv/bin/python research/gold_silver_ratio/ratio_variants_study.py                  # 2018-22
      .venv/bin/python research/gold_silver_ratio/ratio_variants_study.py V3 60 2.0 [z_stop]  # + 2023-25
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ratio_reversion_study as rs  # noqa: E402

FF = rs.sbs.FED_FUNDS
IS_END, END = rs.IS_END, rs.END


def leg_costs(t: pd.Timestamp, g_px: float, s_px: float, h: float) -> float:
    is_ = t < IS_END
    return h * ((0.15 if is_ else 0.17) + 0.07) / g_px + (0.012 if is_ else 0.018) / s_px


def carry(pos: int, h: float, t: pd.Timestamp, days: int) -> float:
    ff = FF.get(t.year, 4.0) / 100
    long_n, short_n = (h, 1.0) if pos > 0 else (1.0, h)        # pos +1: long gold h, short silver 1
    return (long_n * (ff + 0.03) - short_n * (ff - 0.02)) / 365 * days


def simulate(d: pd.DataFrame, variant: str, L: int, z_in: float, z_stop: float | None = None):
    r = np.log(d["g"]["close"]) - np.log(d["s"]["close"])
    z = ((r - r.rolling(L).mean()) / r.rolling(L).std()).to_numpy()
    lg, ls = np.log(d["g"]["close"]).diff(), np.log(d["s"]["close"]).diff()
    beta = (ls.rolling(60).cov(lg) / lg.rolling(60).var()).to_numpy()
    go, so = d["g"]["open"].to_numpy(), d["s"]["open"].to_numpy()
    gc, sc = d["g"]["close"].to_numpy(), d["s"]["close"].to_numpy()
    ts = d.index
    pnl = np.zeros(len(d))
    trades, pos, e, blocked = [], 0, None, False
    for i in range(1, len(d)):
        if pos != 0:
            h = e["h"]
            if e["i"] == i:
                gr, sr = gc[i] / go[i] - 1, sc[i] / so[i] - 1
            else:
                gr, sr = gc[i] / gc[i - 1] - 1, sc[i] / sc[i - 1] - 1
            pnl[i] += pos * (h * gr - sr) - carry(pos, h, ts[i], (ts[i] - ts[i - 1]).days)
        if i + 1 >= len(d) or np.isnan(z[i]):
            continue
        if blocked and abs(z[i]) < z_in:
            blocked = False
        if pos != 0:
            crossed = np.sign(z[i]) != np.sign(e["z"])
            stopped = z_stop is not None and abs(z[i]) >= z_stop
            if crossed or stopped:
                j, h = i + 1, e["h"]
                move = h * (go[j] / gc[i] - 1) - (so[j] / sc[i] - 1)
                pnl[j] += pos * move - leg_costs(ts[j], go[j], so[j], h)
                reason = "stop" if stopped and not crossed else "cross"
                trades.append(dict(t=ts[e["i"]], exit=ts[j], side=pos, h=round(h, 3), reason=reason,
                                   days=(ts[j] - ts[e["i"]]).days, ret=pnl[e["i"]: j + 1].sum()))
                pos, e = 0, None
                blocked = stopped and not crossed
                continue
        if pos == 0 and not blocked and abs(z[i]) >= z_in:
            revert = -1 if z[i] > 0 else 1
            pos = -revert if variant == "V3" else revert
            h = beta[i] if variant == "V1" else 1.0
            if variant == "V1" and (np.isnan(h) or h <= 0):
                pos = 0
                continue
            e = dict(i=i + 1, z=z[i], h=h)
    return pd.DataFrame(trades), pd.Series(pnl, index=ts)


def cells():
    for v in ("V1", "V3"):
        for L in (20, 60, 120):
            for z_in in (1.5, 2.0, 2.5):
                yield v, L, z_in, None
    for L in (20, 60, 120):
        for z_in in (1.5, 2.0, 2.5):
            for zs in (3.0, 3.5):
                yield "V2", L, z_in, zs


def main() -> None:
    d = rs.bars()
    start = d.index[0]
    rows = []
    for v, L, z_in, zs in cells():
        tr, daily = simulate(d, v, L, z_in, zs)
        s = rs.stats(tr, daily, start, IS_END)
        keep = ("n", "win", "days", "sharpe", "total", "max_dd")
        rows.append(dict(variant=v, L=L, z_in=z_in, z_stop=zs, **{k: s[k] for k in keep}))
    t = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print("2018-22, net of spreads, commission and per-leg swap")
    print(t.round(3).to_string(index=False))
    ok = t[t.n >= 15].sort_values("sharpe", ascending=False)
    print("\nbest cell with >= 15 trades:", ok.head(1).round(3).to_dict("records"))

    if len(sys.argv) >= 4:
        v, L, z_in = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
        zs = float(sys.argv[4]) if len(sys.argv) > 4 else None
        tr, daily = simulate(d, v, L, z_in, zs)
        tag = f"{v}_{L}_{z_in}_{zs}"
        tr.to_parquet(rs.OUT / f"gs_variant_trades_{tag}.parquet")
        s = rs.stats(tr, daily, IS_END, END)
        print(f"\n2023-25, looked at once for {tag}:", {k: round(x, 4) for k, x in s.items()})
        o = tr[tr.t >= IS_END]
        print(o.assign(ret=o.ret.round(4)).to_string(index=False))


if __name__ == "__main__":
    main()
