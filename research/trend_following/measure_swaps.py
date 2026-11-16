"""Read the current Pepperstone swap terms for the xm_trend basket (read-only cTrader query).

Idea 771bd086: calibrates the FinancingModel. Prints swapLong/swapShort and the implied
annual rate on notional at today's price. Output: research/output/xm_swaps.csv
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import dotenv

dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True))

import pandas as pd  # noqa: E402

from hermes import Symbol, Timeframe  # noqa: E402
from hermes.data import PepperstoneSource  # noqa: E402

TICKERS = json.loads(Path("tickers/xm_trend.json").read_text())["tickers"]

if __name__ == "__main__":
    src = PepperstoneSource()
    conn = src._connection()
    rows = []
    for t in TICKERS:
        sid = conn.symbol_id(t)
        res = conn._call(lambda sid=sid: conn._client.send(
            "ProtoOASymbolByIdReq", ctidTraderAccountId=conn._account_id, symbolId=[sid]))
        s = res.symbol[0]
        inst = src.get_instrument(Symbol(t, "pepperstone"))
        end = datetime.now(UTC)
        px = src.history(inst, Timeframe.parse("1d"), end - timedelta(days=10), end)[-1].close
        row = dict(ticker=t, swapLong=s.swapLong, swapShort=s.swapShort,
                   calc=s.swapCalculationType, triple_day=s.swapRollover3Days,
                   digits=s.digits, price=px)
        rows.append(row)
        print(row, flush=True)
    pd.DataFrame(rows).to_csv("research/output/xm_swaps.csv", index=False)
