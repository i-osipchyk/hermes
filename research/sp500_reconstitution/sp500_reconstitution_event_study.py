"""Event study: S&P 500 additions / deletions around the effective date.

Idea e116efd9. Windows are fixed here BEFORE looking at results -- no window is tuned.
Day 0 = first trading day on/after the snapshot date in sp500_history.csv (taken as the
effective date). Abnormal return = stock close-to-close minus SPY close-to-close.

Caveats the numbers cannot remove:
  * the dataset gives effective dates, not announcement dates; the pre-effective windows
    assume the event was knowable ~5 trading days ahead (true for scheduled changes, often
    false for M&A-driven ones) -> the [-5,-1] window can be mildly look-ahead;
  * removals whose ticker has no prices (acquired/delisted) drop out, so the deletion leg is
    the *tradable* deletions only; ticker re-use can contaminate old symbols.

Run:  .venv/bin/python research/sp500_reconstitution/sp500_reconstitution_event_study.py
"""

from __future__ import annotations

import csv
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src")]

from hermes import Symbol, Timeframe  # noqa: E402
from hermes.data import YFinanceSource  # noqa: E402

D1 = Timeframe.parse("1d")
FIRST_YEAR = 2005
WINDOWS = {"pre[-5,-1]": (-5, -1), "runup[-5,0]": (-5, 0), "post[+1,+5]": (1, 5), "post[+1,+10]": (1, 10)}
COST_BPS = 10.0  # per side
ERAS = {"2005-12": (2005, 2012), "2013-19": (2013, 2019), "2020-26": (2020, 2026)}


def events() -> list[tuple[str, str, str]]:
    rows = []
    for r in csv.DictReader(open(ROOT / "sp500_history.csv")):
        rows.append((r["date"], {t.strip() for t in r["tickers"].strip('"').split(",") if t.strip()}))
    rows.sort(key=lambda x: x[0])
    out = []
    for (_, a), (d1, b) in zip(rows, rows[1:]):
        out += [(d1, t, "add") for t in b - a] + [(d1, t, "remove") for t in a - b]
    return [e for e in out if int(e[0][:4]) >= FIRST_YEAR]


def closes(src: YFinanceSource, ticker: str, start: datetime, end: datetime) -> pd.Series | None:
    try:
        bars = src.history(src.get_instrument(Symbol(ticker, "yfinance")), D1, start, end)
    except (OSError, ValueError, KeyError, RuntimeError):  # network/provider failures only
        return None
    if len(bars) < 30:
        return None
    s = pd.Series({pd.Timestamp(b.timestamp).tz_convert("UTC").normalize().tz_localize(None): b.close for b in bars})
    return s[~s.index.duplicated()].sort_index()


def main() -> None:
    ev = events()
    src = YFinanceSource()
    start, end = datetime(2004, 6, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)
    tickers = sorted({t for _, t, _ in ev} | {"SPY"})
    print(f"{len(ev)} events, {len(tickers)} tickers; fetching (cached) ...", file=sys.stderr)
    with ThreadPoolExecutor(6) as ex:
        px = dict(zip(tickers, ex.map(lambda t: closes(src, t, start, end), tickers)))
    spy = px["SPY"]
    assert spy is not None
    idx = spy.index
    rows, missing = [], 0
    for d, t, kind in ev:
        s = px.get(t)
        if s is None:
            missing += 1
            continue
        pos = idx.searchsorted(pd.Timestamp(d))
        if pos - 6 < 0 or pos + 11 >= len(idx):
            continue
        r = {"date": d, "ticker": t, "kind": kind, "year": int(d[:4])}
        sr = s.reindex(idx).ffill(limit=2)
        ok = True
        for name, (a, b) in WINDOWS.items():
            # window [a,b]: return from close of day a-1 to close of day b
            p0, p1 = sr.iloc[pos + a - 1], sr.iloc[pos + b]
            m0, m1 = spy.iloc[pos + a - 1], spy.iloc[pos + b]
            if np.isnan(p0) or np.isnan(p1):
                ok = False
                break
            r[name] = (p1 / p0 - m1 / m0) * 1e4  # bps, excess vs SPY
        if ok:
            rows.append(r)
    df = pd.DataFrame(rows)
    print(f"usable events {len(df)}; no-price events dropped {missing}\n")
    df.to_csv(ROOT / "research" / "output" / "sp500_recon_events.csv", index=False)

    def report(sub: pd.DataFrame, label: str) -> None:
        for kind, sign in (("add", 1), ("remove", -1)):
            g = sub[sub.kind == kind]
            if g.empty:
                continue
            line = f"{label:8s} {kind:6s} n={len(g):4d} "
            for w in WINDOWS:
                x = sign * g[w] - 2 * COST_BPS  # signed: long adds / short deletes; round-trip cost
                t = x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 2 else float("nan")
                line += f"| {w} net {x.mean():7.1f}bp t={t:5.2f} hit={(x > 0).mean():.0%} "
            print(line)

    print("net of 2x10bp round-trip cost; short leg sign-flipped (positive = profit)")
    report(df, "ALL")
    for lab, (a, b) in ERAS.items():
        report(df[(df.year >= a) & (df.year <= b)], lab)


if __name__ == "__main__":
    main()
