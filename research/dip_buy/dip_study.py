"""S&P 500 volatile-dip limit buy: event study + portfolio simulation (idea 6f4f38e2).

Rules (as supplied; constants hard-coded, only the limit depth is a knob):
  * universe: point-in-time S&P 500 (sp500_history.csv), yfinance daily OHLC (split/div
    adjusted). Names yfinance lacks (~300 fully delisted) are missing -> survivor bias.
  * signal at close t: member, close > SMA(200), close/close[t-1]-1 < -3%, 100*ATR(5)/close > 3.
  * day t+1: limit buy at close_t - DEPTH*ATR5_t. Filled if low <= limit, at min(open, limit)
    (a gap below the limit fills at the open, ADR-0004).
  * exits, first that triggers:
      - target (from the day after entry): high >= prev close + 0.5*ATR5(prev); filled at
        max(open, target);
      - close > previous day's high -> exit at the next open (no acting on a close at that
        same close);
      - held 10 trading days -> exit at the next open;
      - leaves the index -> exit at its last member close.
  * portfolio: SLOTS equal-weight slots (1/SLOTS of equity each); each morning orders go to
    the top `free slots` candidates by ATR5/close (no over-ordering).
  * costs: COST_BP per side.

Usage:
    .venv/bin/python research/dip_buy/dip_study.py --window is            # 2011-2022
    .venv/bin/python research/dip_buy/dip_study.py --window holdout       # 2023-> (count it!)
Outputs research/output/dip_buy/trades_<tag>.csv and prints the summary.
"""

from __future__ import annotations

import argparse
import csv
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BARS = ROOT / ".hermes_cache" / "bars" / "yfinance"
CAL = ROOT / "sp500_history.csv"
OUT = ROOT / "research" / "output" / "dip_buy"

WINDOWS = {
    "is": ("2011-01-01", "2022-12-31"),
    "holdout": ("2023-01-01", "2026-12-31"),
    "all": ("2011-01-01", "2026-12-31"),
}
DROP = -0.03
VOL_MIN = 3.0
TARGET_ATR = 0.5
MAX_HOLD = 10


@dataclass
class Cfg:
    depth: float = 0.9
    slots: int = 10
    max_fills_per_day: int | None = None   # cap on limit orders that fill on one day (by rank)
    cost_bp: float = 5.0
    entry: str = "limit"           # limit | open (market at next open, no limit)
    through: float = 0.0           # require low <= limit*(1-through) to count a fill
    vol_min: float = VOL_MIN
    drop: float = DROP
    target_atr: float = TARGET_ATR
    max_hold: int = MAX_HOLD
    atr_len: int = 5
    sma_len: int = 200
    size: float | None = None       # fraction of equity per position (default 1/slots)
    max_gross: float = 1.0          # cap on total notional / equity (leverage)
    swap_annual: float = 0.0        # financing on open notional, per calendar day
    stop_atr: float | None = None   # stop at entry - k*ATR5(signal day)
    stop_pct: float | None = None   # stop at entry*(1-pct)
    trend: bool = True
    exits: tuple[str, ...] = ("target", "close_gt_high", "time")
    extra: dict = field(default_factory=dict)


def load_calendar() -> tuple[list[pd.Timestamp], list[frozenset[str]]]:
    dates, members = [], []
    with CAL.open() as f:
        for row in csv.DictReader(f):
            dates.append(pd.Timestamp(row["date"]))
            members.append(frozenset(t.strip() for t in row["tickers"].split(",")))
    return dates, members


def load_panel() -> dict[str, pd.DataFrame]:
    cache = OUT / "panel.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    cal_dates, cal_members = load_calendar()
    ever = frozenset().union(*cal_members)
    panel: dict[str, pd.DataFrame] = {}
    for p in sorted(BARS.glob("*_1d.parquet")):
        tkr = p.name[: -len("_1d.parquet")]
        if tkr not in ever:   # the cache also holds FX pairs etc.; their dates are not NYSE days
            continue
        d = pd.read_parquet(p)
        ny = pd.to_datetime(d.ts, unit="s", utc=True).dt.tz_convert("America/New_York")
        d.index = pd.DatetimeIndex(ny.dt.tz_localize(None).dt.normalize())
        d = d[~d.index.duplicated(keep="last")][["open", "high", "low", "close"]].astype(float)
        d = d[(d > 0).all(axis=1)]
        if len(d) < 220:
            continue
        c = d.close
        prev_c = c.shift(1)
        tr = pd.concat([d.high - d.low, (d.high - prev_c).abs(), (d.low - prev_c).abs()], axis=1).max(
            axis=1
        )
        d["atr5"] = tr.rolling(5).mean()
        d["sma200"] = c.rolling(200).mean()
        d["ret"] = c / prev_c - 1
        idx = [bisect_right(cal_dates, t) - 1 for t in d.index]
        d["member"] = [i >= 0 and tkr in cal_members[i] for i in idx]
        panel[tkr] = d
    OUT.mkdir(parents=True, exist_ok=True)
    pd.to_pickle(panel, cache)
    return panel


def simulate(panel: dict[str, pd.DataFrame], cfg: Cfg, start: str, end: str, seed: int | None = None):
    """Return (trades DataFrame, daily equity Series).

    seed != None -> random-entry control: on each signal day the candidates are replaced by
    the same number of random uptrend members (same entry type and exits).
    """
    rng = np.random.default_rng(seed)
    days = pd.DatetimeIndex(sorted(set().union(*[d.index for d in panel.values()])))
    days = days[(days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))]
    # per-ticker arrays aligned to `days` (NaN where no bar)
    cols = ["open", "high", "low", "close", "atr5", "sma200", "ret", "member"]
    A = {}
    for t, d in panel.items():
        if cfg.atr_len != 5 or cfg.sma_len != 200:
            d = d.copy()
            pc = d.close.shift(1)
            tr_ = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1)
            d["atr5"] = tr_.max(axis=1).rolling(cfg.atr_len).mean()
            d["sma200"] = d.close.rolling(cfg.sma_len).mean() if cfg.sma_len else 0.0
        r = d.reindex(days)
        A[t] = {k: r[k].to_numpy(dtype=float) for k in cols}
    tickers = list(A)
    O = np.vstack([A[t]["open"] for t in tickers])
    H = np.vstack([A[t]["high"] for t in tickers])
    L = np.vstack([A[t]["low"] for t in tickers])
    C = np.vstack([A[t]["close"] for t in tickers])
    ATR = np.vstack([A[t]["atr5"] for t in tickers])
    SMA = np.vstack([A[t]["sma200"] for t in tickers])
    RET = np.vstack([A[t]["ret"] for t in tickers])
    MEM = np.vstack([A[t]["member"] for t in tickers]) == 1.0
    volp = 100 * ATR / C
    uptrend = (C > SMA) if cfg.trend else np.isfinite(C)
    sig = MEM & uptrend & (RET < cfg.drop) & (volp > cfg.vol_min)
    pool = MEM & (C > SMA) & np.isfinite(ATR)
    cost = cfg.cost_bp / 1e4

    cash = 1.0
    pos: dict[int, dict] = {}   # ticker index -> position
    trades, equity = [], []
    n_days = len(days)
    for j in range(1, n_days):
        # --- mark equity at prior close for sizing
        gross_prev = sum(p["sh"] * _last(C, i, j - 1) for i, p in pos.items())
        eq_prev = cash + gross_prev
        cash -= gross_prev * cfg.swap_annual * (days[j] - days[j - 1]).days / 365
        # --- exits that fill at today's open (decided at yesterday's close)
        for i in list(pos):
            p = pos[i]
            if p.get("exit_open"):
                px = O[i, j] if np.isfinite(O[i, j]) else _last(C, i, j - 1)
                _close(pos, i, px, days[j], p["exit_open"], trades, cost)
                cash += p["sh"] * px * (1 - cost)
        # --- entries: candidates from yesterday's signals
        free = cfg.slots - len(pos)
        if free > 0:
            cand = np.flatnonzero(sig[:, j - 1])
            cand = [i for i in cand if i not in pos]
            if seed is not None and cand:
                ok = [i for i in np.flatnonzero(pool[:, j - 1]) if i not in pos]
                cand = list(rng.choice(ok, size=min(len(cand), len(ok)), replace=False)) if ok else []
            else:
                cand.sort(key=lambda i: -volp[i, j - 1])
            filled_today = 0
            for i in cand[:free]:
                if cfg.max_fills_per_day is not None and filled_today >= cfg.max_fills_per_day:
                    break
                if not np.isfinite(O[i, j]):
                    continue
                if cfg.entry == "limit":
                    lim = C[i, j - 1] - cfg.depth * ATR[i, j - 1]
                    if not (L[i, j] <= lim * (1 - cfg.through)):
                        continue
                    px = min(O[i, j], lim)
                else:
                    px = O[i, j]
                alloc = eq_prev * (cfg.size if cfg.size else 1 / cfg.slots)
                held_val = sum(q["sh"] * _last(C, k, j - 1) for k, q in pos.items())
                alloc = min(alloc, cfg.max_gross * eq_prev - held_val)
                if alloc <= 0:
                    continue
                sh = alloc * (1 - cost) / px
                cash -= alloc
                filled_today += 1
                stop = -np.inf
                if cfg.stop_atr is not None:
                    stop = px - cfg.stop_atr * ATR[i, j - 1]
                if cfg.stop_pct is not None:
                    stop = max(stop, px * (1 - cfg.stop_pct))
                pos[i] = dict(
                    stop=stop, sh=sh, entry=px, cost_basis=alloc, t=tickers[i], d0=days[j], j0=j,
                    sig_close=C[i, j - 1], atr_sig=ATR[i, j - 1], volp=volp[i, j - 1],
                    ret_sig=RET[i, j - 1], mae=0.0, mfe=0.0,
                )
        # --- intraday / close exits for today
        for i in list(pos):
            p = pos[i]
            if not np.isfinite(C[i, j]):
                continue
            p["mae"] = min(p["mae"], L[i, j] / p["entry"] - 1)
            p["mfe"] = max(p["mfe"], H[i, j] / p["entry"] - 1)
            held = j - p["j0"]
            if L[i, j] <= p["stop"]:   # live from the entry bar; stop-first on a clash
                px = min(O[i, j], p["stop"]) if held >= 1 else p["stop"]
                cash += p["sh"] * px * (1 - cost)
                _close(pos, i, px, days[j], "stop", trades, cost)
                continue
            if "target" in cfg.exits and held >= 1 and np.isfinite(ATR[i, j - 1]):
                tgt = C[i, j - 1] + cfg.target_atr * ATR[i, j - 1]
                if H[i, j] >= tgt:
                    px = max(O[i, j], tgt)
                    cash += p["sh"] * px * (1 - cost)
                    _close(pos, i, px, days[j], "target", trades, cost)
                    continue
            if "close_gt_high" in cfg.exits and C[i, j] > H[i, j - 1]:
                p["exit_open"] = "close_gt_high"
            elif "time" in cfg.exits and held >= cfg.max_hold:
                p["exit_open"] = "time"
            elif not MEM[i, j] or j + 1 >= n_days:
                cash += p["sh"] * C[i, j] * (1 - cost)
                _close(pos, i, C[i, j], days[j], "index_exit" if not MEM[i, j] else "end", trades,
                       cost)
        equity.append(cash + sum(p["sh"] * _last(C, i, j) for i, p in pos.items()))
    tr = pd.DataFrame(trades)
    eq = pd.Series(equity, index=days[1:])
    return tr, eq


def _last(C, i, j):
    k = j
    while k >= 0 and not np.isfinite(C[i, k]):
        k -= 1
    return C[i, k] if k >= 0 else 0.0


def _close(pos, i, px, day, why, trades, cost):
    p = pos.pop(i)
    proceeds = p["sh"] * px * (1 - cost)
    trades.append(dict(
        ticker=p["t"], entry_date=p["d0"].date(), exit_date=day.date(), entry=p["entry"], exit=px,
        ret=proceeds / p["cost_basis"] - 1, why=why, volp=p["volp"], ret_sig=p["ret_sig"],
        depth_atr=(p["sig_close"] - p["entry"]) / max(p["atr_sig"], 1e-9), mae=p["mae"], mfe=p["mfe"],
    ))


def summarize(tr: pd.DataFrame, eq: pd.Series, label: str) -> dict:
    r = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else float("nan")
    n = len(tr)
    m = tr.ret.mean() if n else float("nan")
    t = m / (tr.ret.std() / np.sqrt(n)) if n > 1 else float("nan")
    out = dict(label=label, trades=n, avg_trade=m, t=t, win=(tr.ret > 0).mean() if n else np.nan,
               cagr=cagr, sharpe=sharpe, maxdd=dd, expo=None)
    print(f"{label:<34} n={n:5d} avg={m:+.3%} t={t:5.2f} win={out['win']:.1%} "
          f"CAGR={cagr:+.1%} Sharpe={sharpe:.2f} MaxDD={dd:.1%}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=list(WINDOWS), default="is")
    ap.add_argument("--depth", type=float, default=0.9)
    ap.add_argument("--slots", type=int, default=10)
    ap.add_argument("--max-fills-per-day", type=int, default=None)
    ap.add_argument("--cost-bp", type=float, default=5.0)
    ap.add_argument("--entry", choices=["limit", "open"], default="limit")
    ap.add_argument("--through", type=float, default=0.0)
    ap.add_argument("--vol-min", type=float, default=VOL_MIN)
    ap.add_argument("--stop-atr", type=float, default=None)
    ap.add_argument("--stop-pct", type=float, default=None)
    ap.add_argument("--random", type=int, default=0, help="number of random-entry seeds")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    if a.window == "holdout":
        print("*** HOLDOUT TOUCH -- count it in the trial log ***", file=sys.stderr)
    start, end = WINDOWS[a.window]
    panel = load_panel()
    print(f"panel: {len(panel)} tickers  window {start}..{end}")
    cfg = Cfg(depth=a.depth, slots=a.slots, max_fills_per_day=a.max_fills_per_day, cost_bp=a.cost_bp, entry=a.entry, through=a.through,
              vol_min=a.vol_min, stop_atr=a.stop_atr, stop_pct=a.stop_pct)
    tr, eq = simulate(panel, cfg, start, end)
    tag = a.tag or f"{a.window}_d{a.depth}_s{a.slots}_c{a.cost_bp:g}_{a.entry}"
    OUT.mkdir(parents=True, exist_ok=True)
    tr.to_csv(OUT / f"trades_{tag}.csv", index=False)
    eq.to_csv(OUT / f"equity_{tag}.csv")
    summarize(tr, eq, tag)
    if len(tr):
        print(tr.groupby("why").ret.agg(["count", "mean"]).to_string())
        tr["year"] = pd.to_datetime(tr.entry_date).dt.year
        yr = tr.groupby("year").ret.agg(["count", "mean", "sum"])
        yr["eq_ret"] = eq.groupby(eq.index.year).apply(lambda s: s.iloc[-1] / s.iloc[0] - 1)
        print(yr.to_string(float_format=lambda x: f"{x:+.3f}"))
    for s in range(a.random):
        rtr, req = simulate(panel, cfg, start, end, seed=s)
        summarize(rtr, req, f"random seed {s}")


if __name__ == "__main__":
    main()
