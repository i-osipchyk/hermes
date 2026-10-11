"""Export gold_tokyo_breakout_v2_static's per-day plans and trades for the live bot's tests.

    .venv/bin/python research/tokyo/static_export.py ../gold-tokyo-breakout/tests/fixtures

Writes ``static_day_plans.csv`` (one row per weekday the 08:55 bar was processed),
``static_trades.csv`` and ``bars.csv.gz`` (the bars those plans were made from). Same
configuration as the ledger run 92974ddaae48e824; the trade count is printed so the two can be
compared.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import dotenv

dotenv.load_dotenv(".env")

_spec = importlib.util.spec_from_file_location(
    "static", Path("strategies/misc/gold_tokyo_breakout_v2_static.py"))
static = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(static)


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    strategy = static.GoldTokyoBreakoutV2Static()
    result = static.build_backtest(strategy=strategy, params={"width_ratio": 1.0}).run()

    with open(out / "static_day_plans.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(strategy.day_plans[0]))
        w.writeheader()
        w.writerows(strategy.day_plans)

    fields = ["entry_time", "side", "entry_price", "stop_loss", "size", "exit_time",
              "exit_price", "exit_reason", "net_pnl"]
    with open(out / "static_trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for t in result.trades:
            w.writerow([t.entry_time.isoformat(), t.side.name, t.entry_price, t.stop_loss,
                        t.size, t.exit_time.isoformat(), t.exit_price, t.exit_reason, t.net_pnl])
    print(f"{len(strategy.day_plans)} day plans, {len(result.trades)} trades -> {out}")


def export_bars(out_dir: str) -> None:
    """``bars.csv.gz`` for the bot's replay test: the 5m bars of every weekday's Tokyo window and
    1h bars aggregated from the same 5m stream, both from the engine's Lead-in start, so ATR is
    seeded exactly as in the backtest. Columns: kind (t = Tokyo 5m, h = 1h), epoch minutes of
    the bar open, open/high/low/close in integer cents."""
    import gzip
    from datetime import UTC, datetime

    from hermes import Symbol, Timeframe
    from hermes.data import PepperstoneSource

    m5 = Timeframe.parse("5m")
    start, end = datetime(2018, 1, 1, tzinfo=UTC), datetime(2026, 8, 6, tzinfo=UTC)
    source = PepperstoneSource()
    inst = source.get_instrument(Symbol("XAUUSD", "pepperstone"))
    strategy = static.GoldTokyoBreakoutV2Static()
    bt = static.build_backtest(strategy=strategy, params={"width_ratio": 1.0})
    strategy.instrument = inst
    strategy.run_setup()
    lead = bt._lead_in_start(start, [(strategy.registered_indicators, m5, inst.session)])
    bars = [b for b in source.history(inst, m5, lead, end) if b.timestamp <= end]

    def cents(x: float) -> int:
        c = round(x * 100)
        assert abs(c - x * 100) < 1e-6, x
        return c

    rows, hour = [], None
    for b in bars:
        t = b.timestamp
        if t.weekday() < 5 and t.hour < 9:
            rows.append(("t", t, b.open, b.high, b.low, b.close))
        key = t.replace(minute=0)
        if hour is None or hour[1] != key:
            if hour is not None:
                rows.append(tuple(hour))
            hour = ["h", key, b.open, b.high, b.low, b.close]
        else:
            hour[3], hour[4], hour[5] = max(hour[3], b.high), min(hour[4], b.low), b.close
    rows.append(tuple(hour))
    rows.sort(key=lambda r: (r[1], r[0]))

    path = Path(out_dir) / "bars.csv.gz"
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "minute", "open", "high", "low", "close"])
        for kind, t, o, h, low, c in rows:
            w.writerow([kind, int(t.timestamp()) // 60, cents(o), cents(h), cents(low), cents(c)])
    print(f"lead-in start {lead}; {len(rows)} rows -> {path} ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main(sys.argv[1])
    export_bars(sys.argv[1])
