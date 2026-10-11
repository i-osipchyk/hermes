"""Acceptance of gold_tokyo_breakout_v2_static against the spec's run 2fe13c7c79f3ee20.

The variant only changes how the rules are executed (orders placed once after the 08:55 bar,
ATR frozen, stop shortened on narrow days, holiday flatten), so it should land close to the
reference. The thresholds were fixed on 2026-10-11, BEFORE the variant was run
(gold-tokyo-breakout docs/ARCHITECTURE.md §9.1). If it fails, don't tune it: the agreed
fallback is the 5-minute exact-parity design judged against 2fe13c7c itself.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

REFERENCE_KEY = "2fe13c7c79f3ee20"
REFERENCE_TRADES = 1121

# name -> (lowest accepted, highest accepted); None = unbounded. All inclusive.
THRESHOLDS: dict[str, tuple[float | None, float | None]] = {
    "trades": (round(REFERENCE_TRADES * 0.95), round(REFERENCE_TRADES * 1.05)),  # 1065-1177
    "sharpe": (0.95, None),
    "mean_r": (0.08, None),           # R = 1% of equity at entry
    "profit_factor": (1.18, None),
    "mc_worst_dd": (-0.52, None),     # Monte Carlo worst drawdown, fraction (reference -0.459)
    "overlap": (0.95, None),          # share of reference (day, side) trades the variant also took
}


@dataclass(frozen=True)
class Criterion:
    name: str
    value: float
    low: float | None
    high: float | None
    passed: bool


@dataclass(frozen=True)
class Verdict:
    criteria: list[Criterion]

    @property
    def accepted(self) -> bool:
        return all(c.passed for c in self.criteria)


def check(metrics: dict[str, float]) -> Verdict:
    """Judge the variant's metrics against every threshold."""
    criteria = []
    for name, (low, high) in THRESHOLDS.items():
        value = metrics[name]
        passed = (low is None or value >= low) and (high is None or value <= high)
        criteria.append(Criterion(name, value, low, high, passed))
    return Verdict(criteria)


def overlap(variant: list[tuple], reference: list[tuple]) -> float:
    """Share of the reference's (day, side) trades that the variant also took."""
    ref = set(reference)
    return len(ref & set(variant)) / len(ref)


def mc_worst_drawdown_r(rs: list[float], n: int = 1000, seed: int = 42) -> float:
    """Worst peak-to-trough drawdown, in R, of the cumulative R curve over ``n`` random
    orderings of the trades. Additive R, not compounded equity: the live kill rule is in R
    because coarse 0.01-lot sizing makes %-of-equity drawdowns depend on the account size."""
    rng = random.Random(seed)
    order = list(rs)
    worst = 0.0
    for _ in range(n):
        rng.shuffle(order)
        equity = peak = 0.0
        for r in order:
            equity += r
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
    return worst


def trade_rs(trades: list[dict], starting_equity: float, risk: float = 0.01) -> list[float]:
    """Each trade's net P&L in R, R = ``risk`` x equity when it was entered (trades never
    overlap, so that equity is the start plus everything closed before the entry)."""
    closed = sorted(trades, key=lambda t: t["exit_time"])
    rs = []
    for t in sorted(trades, key=lambda t: t["entry_time"]):
        equity = starting_equity + sum(
            c["net_pnl"] for c in closed if c["exit_time"] <= t["entry_time"])
        rs.append(t["net_pnl"] / (risk * equity))
    return rs


def day_sides(trades: list[dict]) -> list[tuple]:
    return [(t["entry_time"][:10], t["side"]) for t in trades]


def _mc_worst_pct(analysis: dict) -> float:
    """Monte Carlo worst drawdown from ``hermes analyze``'s trade-reorder axis."""
    for axis in analysis["axes"]:
        mc = (axis.get("data") or {}).get("monte_carlo")
        if mc:
            return mc["max_drawdown_worst"]
    raise ValueError("analysis.json has no Monte Carlo axis -- run `hermes analyze <key>`")


def main(variant_key: str, reference_key: str = REFERENCE_KEY) -> None:
    """Print the verdict. The Monte Carlo worst %-drawdown comes from ``hermes analyze``."""
    import json

    from hermes.research.ledger import load_run

    variant, _ = load_run(variant_key)
    reference, _ = load_run(reference_key)
    start = variant["equity_curve"][0][1]
    v_rs = trade_rs(variant["trades"], start)
    analysis = json.loads(
        (Path(".hermes_cache/runs") / variant_key / "analysis.json").read_text())
    metrics = {
        "trades": len(variant["trades"]),
        "sharpe": variant["metrics"]["sharpe"],
        "mean_r": sum(v_rs) / len(v_rs),
        "profit_factor": variant["metrics"]["profit_factor"],
        "mc_worst_dd": _mc_worst_pct(analysis),
        "overlap": overlap(day_sides(variant["trades"]), day_sides(reference["trades"])),
    }
    verdict = check(metrics)
    for c in verdict.criteria:
        print(f"{c.name:14} {c.value:10.4f}  [{c.low}, {c.high}]  {'pass' if c.passed else 'FAIL'}")
    print("ACCEPTED" if verdict.accepted else "REJECTED")
    print(f"MC worst drawdown in R (live kill rule): {mc_worst_drawdown_r(v_rs):.1f} R")


if __name__ == "__main__":
    import sys

    main(*sys.argv[1:])
