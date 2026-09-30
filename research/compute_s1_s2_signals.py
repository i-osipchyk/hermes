"""Compute the S1 (margin expansion) / S2 (revenue re-accel or fast-growth)
signals for every row in research/output/ema_crossover_trades.csv, once
against the latest annual (10-K) filing period and once against the latest
quarterly (10-Q) filing period — kept separate rather than collapsed into
whichever was filed more recently. No API calls: reads the two period
identifiers already stored per row (fundamentals_annual_year/_quarter,
fundamentals_quarterly_year/_quarter) and looks them up in the per-ticker
filing history already cached to .cache/pit/_massive_financials/ from the
enrich phase.

    .venv/bin/python research/compute_s1_s2_signals.py

Reads research/output/ema_crossover_trades.csv and writes
research/output/ema_crossover_trades_signals.csv: the same rows plus
s1_annual, s1_annual_detail, s2_annual, s2_annual_detail,
s1_quarterly, s1_quarterly_detail, s2_quarterly, s2_quarterly_detail.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hermes.ai.enrichers import screen_period

IN_PATH = Path(__file__).parent / "output" / "ema_crossover_trades.csv"
OUT_PATH = Path(__file__).parent / "output" / "ema_crossover_trades_signals.csv"

_NO_PERIOD = {
    "s1": None,
    "s2": None,
    "s1_detail": "no fundamentals period",
    "s2_detail": "no fundamentals period",
}


def _screen(symbol: str, year: str, quarter: str) -> dict:
    if not year or not quarter:
        return _NO_PERIOD
    return screen_period(symbol, int(year), quarter)


def main() -> None:
    if not IN_PATH.exists():
        print(f"ERROR: {IN_PATH} not found — run build_ema_crossover_dataset.py enrich first.")
        sys.exit(1)

    with IN_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    out_fields = fieldnames + [
        "s1_annual", "s1_annual_detail", "s2_annual", "s2_annual_detail",
        "s1_quarterly", "s1_quarterly_detail", "s2_quarterly", "s2_quarterly_detail",
    ]

    with OUT_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()

        for i, row in enumerate(rows, 1):
            annual = _screen(
                row["symbol"], row["fundamentals_annual_year"], row["fundamentals_annual_quarter"]
            )
            quarterly = _screen(
                row["symbol"], row["fundamentals_quarterly_year"], row["fundamentals_quarterly_quarter"]
            )

            out = dict(row)
            out["s1_annual"] = annual["s1"]
            out["s1_annual_detail"] = annual["s1_detail"]
            out["s2_annual"] = annual["s2"]
            out["s2_annual_detail"] = annual["s2_detail"]
            out["s1_quarterly"] = quarterly["s1"]
            out["s1_quarterly_detail"] = quarterly["s1_detail"]
            out["s2_quarterly"] = quarterly["s2"]
            out["s2_quarterly_detail"] = quarterly["s2_detail"]
            writer.writerow(out)

            if i % 2000 == 0 or i == len(rows):
                print(f"  … {i}/{len(rows)} screened")

    print(f"Done. Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
