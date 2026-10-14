"""Measure the US500 / NAS100 CFD spread around the US cash open from cTrader bid/ask ticks.

Idea 08e23fa6 (opening range breakout) enters at the open, the widest-spread minutes of the
day, so the cost model needs a measured number there and at mid-morning, not a guess.

Samples the second Wednesday of each quarter (plus a Monday/Friday per year for variety),
09:25-10:30 New York, and reports the median (bid-ask) per 5-minute bucket and period.
Read-only: ProtoOAGetTickDataReq and ProtoOASymbolByIdReq, no orders.

    .venv/bin/python research/us_index_orb/measure_open_spread.py
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import dotenv
import pandas as pd

dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True))

from hermes.data import PepperstoneSource  # noqa: E402

NY = ZoneInfo("America/New_York")
OUT = Path("research/output/us_index_orb_spreads.csv")
SYMBOLS = ["US500", "NAS100"]


def sample_days() -> list[date]:
    days = []
    for y in range(2018, 2027):
        for m in (1, 4, 7, 10):
            d = date(y, m, 8)
            while d.weekday() != 2:          # second Wednesday
                d += timedelta(days=1)
            if d < date(2026, 10, 1):
                days.append(d)
    return days


def ticks(conn, symbol_id: int, side: int, start: datetime, end: datetime) -> list[tuple[int, float]]:
    """All ticks of one side in [start, end), as (ms, price)."""
    out: list[tuple[int, float]] = []
    to_ms = int(end.timestamp() * 1000)
    from_ms = int(start.timestamp() * 1000)
    while True:
        res = conn._call(lambda to_ms=to_ms: conn._client.send(
            "ProtoOAGetTickDataReq", ctidTraderAccountId=conn._account_id, symbolId=symbol_id,
            type=side, fromTimestamp=from_ms, toTimestamp=to_ms))
        ts = px = 0
        page = []
        for i, t in enumerate(res.tickData):     # first absolute, the rest deltas (newest first)
            ts = t.timestamp if i == 0 else ts + t.timestamp
            px = t.tick if i == 0 else px + t.tick
            page.append((ts, px / 100_000))
        out.extend(page)
        if not res.hasMore or not page:
            break
        to_ms = min(p[0] for p in page) - 1
        _time.sleep(0.25)
    return sorted(out)


def main() -> None:
    src = PepperstoneSource()
    conn = src._connection()
    rows = []
    for sym in SYMBOLS:
        sid = conn.symbol_id(sym)
        spec = conn._call(lambda sid=sid: conn._client.send(
            "ProtoOASymbolByIdReq", ctidTraderAccountId=conn._account_id, symbolId=[sid])).symbol[0]
        print(sym, "digits", spec.digits, "lotSize", spec.lotSize,
              "commission", getattr(spec, "commission", None),
              "preciseTradingCommissionRate", getattr(spec, "preciseTradingCommissionRate", None))
        for d in sample_days():
            start = datetime(d.year, d.month, d.day, 9, 25, tzinfo=NY)
            end = datetime(d.year, d.month, d.day, 10, 30, tzinfo=NY)
            try:
                bid = ticks(conn, sid, 1, start, end)
                ask = ticks(conn, sid, 2, start, end)
            except Exception as exc:          # noqa: BLE001 -- report and move on
                print(sym, d, "failed:", exc)
                continue
            if not bid or not ask:
                print(sym, d, "no ticks")
                continue
            b = pd.DataFrame(bid, columns=["ms", "bid"]).drop_duplicates("ms", keep="last")
            a = pd.DataFrame(ask, columns=["ms", "ask"]).drop_duplicates("ms", keep="last")
            q = pd.merge_asof(b.sort_values("ms"), a.sort_values("ms"), on="ms",
                              direction="backward").dropna()
            q["spread"] = q.ask - q.bid
            q["t"] = pd.to_datetime(q.ms, unit="ms", utc=True).dt.tz_convert(NY)
            q["bucket"] = q.t.dt.floor("5min").dt.strftime("%H:%M")
            for bkt, g in q.groupby("bucket"):
                rows.append({"symbol": sym, "day": d, "bucket": bkt, "median": g.spread.median(),
                             "p90": g.spread.quantile(0.9), "n": len(g),
                             "mid": float((g.bid.median() + g.ask.median()) / 2)})
            print(sym, d, f"{len(q)} quotes, 09:30 bucket median",
                  q[q.bucket == "09:30"].spread.median())
    df = pd.DataFrame(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    df["year"] = pd.to_datetime(df.day).dt.year
    df["phase"] = df.bucket.map(lambda b: "09:30-09:35" if b == "09:30" else
                                "09:35-09:45" if b in ("09:35", "09:40") else
                                "pre" if b < "09:30" else "09:45-10:30")
    print(df.pivot_table(index=["symbol", "year"], columns="phase", values="median",
                         aggfunc="median").round(2).to_string())


if __name__ == "__main__":
    main()
