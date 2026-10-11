"""Anchored, yearly walk-forward for gold_tokyo_breakout_v2 (idea 0945d084).

The CLI's walkforward axis always yields one window (rolling IS of is_frac x the whole period),
so this runs the library's WalkForward once per year instead:

    IS   2018-01-01 -> Y-01-01 (anchored, growing)      grid: width_ratio x stop_atr (3 x 3)
    OOS  Y-01-01 -> Y+1-01-01 (last: 2026-08-06)        Y = 2021 .. 2026

The fitted parameters per year show whether the optimum is stable; the stitched OOS curve
(each year scaled to the previous year's end equity, as WalkForward does) is the number to quote.
The grid is centred on the declared values (1.31, 1.0); it is not a search for a better point.

Run:  .venv/bin/python research/tokyo/tokyo_v2_walkforward.py [SYMBOL] [STRATEGY]
      (defaults XAUUSD, gold_tokyo_breakout_v2; STRATEGY is a file stem in strategies/misc/)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import dotenv

from hermes import Symbol
from hermes.backtest import BacktestResult, WalkForward

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "research/output"
GRID = {"width_ratio": [1.15, 1.31, 1.5], "stop_atr": [0.75, 1.0, 1.25]}
START = datetime(2018, 1, 1, tzinfo=UTC)
END = datetime(2026, 8, 6, tzinfo=UTC)


def load_strategy_module(name: str):
    path = ROOT / f"strategies/misc/{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    dotenv.load_dotenv(ROOT / ".env")
    sym = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
    name = sys.argv[2] if len(sys.argv) > 2 else "gold_tokyo_breakout_v2"
    mod = load_strategy_module(name)
    rows, windows = [], []
    for year in range(2021, 2027):
        oos_start = datetime(year, 1, 1, tzinfo=UTC)
        oos_end = min(datetime(year + 1, 1, 1, tzinfo=UTC), END)
        template = mod.build_backtest(symbol=Symbol(sym, "pepperstone"), start=START, end=oos_end)
        is_frac = (oos_start - START) / (oos_end - START)
        res = WalkForward(template=template, total_start=START, total_end=oos_end,
                          is_frac=is_frac, anchored=True, param_grid=GRID).run()
        w = res.windows[0]
        m = w.oos_result.metrics
        windows.append(w)
        rows.append(dict(year=year, width_ratio=w.best_params.get("width_ratio"),
                         stop_atr=w.best_params.get("stop_atr"), trades=m.num_trades,
                         ret=round(m.total_return, 4), sharpe=round(m.sharpe or 0.0, 3),
                         pf=round(m.profit_factor or 0.0, 3), max_dd=round(m.max_drawdown, 4)))
        print(rows[-1], flush=True)

    curve, trades, prev = [], [], windows[0].oos_result.equity_curve[0][1]
    for w in windows:
        c = w.oos_result.equity_curve
        scale = prev / c[0][1]
        curve += [(t, e * scale) for t, e in c]
        trades += w.oos_result.trades
        prev = curve[-1][1]
    st = BacktestResult.compute(curve, trades).metrics
    stitched = dict(trades=st.num_trades, ret=round(st.total_return, 4), cagr=round(st.cagr, 4),
                    sharpe=round(st.sharpe or 0.0, 3), pf=round(st.profit_factor or 0.0, 3),
                    max_dd=round(st.max_drawdown, 4), win=round(st.win_rate, 3))
    print("stitched OOS 2021-2026.08:", stitched)
    (OUT / f"tokyo_v2_walkforward_{name}_{sym}.json").write_text(
        json.dumps({"grid": GRID, "windows": rows, "stitched": stitched}, indent=2))


if __name__ == "__main__":
    main()
