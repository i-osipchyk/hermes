"""Earning a look at the holdout (ADR-0012, phase-2 plan step 5).

``evaluate_gate`` is the in-sample bar an idea must clear *before* the holdout is read;
the holdout is a single, recorded look, so it is not spent on something that is already
unconvincing. Every check is measured here: one that cannot be measured fails the gate
rather than passing it (CLAUDE.md: never present an unmeasured axis as clean).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .analysis import DeflationInputs, analyze

MIN_DSR = 0.9            # deflated Sharpe at or above this (owner's choice; calibrate)
MAX_NULL_P = 0.05        # the null test's p-value must be below this
PASSING_TIERS = ("credible", "deployable")

MIN_HOLDOUT_PSR = 0.8    # P(SR > 0) on the holdout, before deflating by earlier looks
MIN_HOLDOUT_TRADES = 10  # the trade-level inference floor; a 20% slice rarely holds 30


@dataclass(frozen=True)
class GateResult:
    passed: bool
    checks: dict[str, dict[str, Any]]      # name -> {ok, value, need, why}

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks}


def evaluate_gate(
    backtest: Any, result: Any, deflation: DeflationInputs | None, *,
    null_n: int = 200, seed: int = 42,
) -> GateResult:
    analysis = analyze(
        backtest, result, axes=("validation", "deflation", "null"), deflation=deflation,
        null_n=null_n, seed=seed, holdout_carved=True,
    )
    checks: dict[str, dict[str, Any]] = {}

    v = analysis["validation"]
    tier = v.data["sample_quality"]["tier"] if v.ran else None
    checks["tier"] = {"ok": tier in PASSING_TIERS, "value": tier,
                      "need": f"one of {PASSING_TIERS}",
                      "why": v.data["sample_quality"]["tier_reason"] if v.ran else v.error}

    d = analysis["deflation"]
    dsr = d.data.get("dsr") if d.ran else None
    checks["dsr"] = {"ok": dsr is not None and dsr >= MIN_DSR, "value": dsr,
                     "need": f">= {MIN_DSR}", "why": d.note}

    n = analysis["null"]
    p = n.data.get("p_value") if n.ran else None
    checks["null"] = {"ok": p is not None and p < MAX_NULL_P, "value": p,
                      "need": f"< {MAX_NULL_P}", "why": n.note}

    return GateResult(all(c["ok"] for c in checks.values()), checks)


def required_psr(prior_looks: int) -> float:
    """The holdout PSR an idea must reach. Each earlier look at the same instrument's holdout
    is another draw against the same data, so the bar rises: 0.80, 0.90, 0.933, ... (ADR-0012)."""
    return 1.0 - (1.0 - MIN_HOLDOUT_PSR) / (1 + prior_looks)


def holdout_verdict(in_sample_sharpe: float | None, result: Any, prior_looks: int) -> dict:
    """Did the frozen strategy hold up on the data it never saw?

    It must keep the in-sample Sharpe's sign, reach :func:`required_psr`, and have traded
    enough to measure. The numbers are returned whatever the outcome.
    """
    from ..backtest import validate

    n_trades = len(result.trades)
    sharpe = result.metrics.sharpe
    sv = validate(result, n_bootstrap=10, n_mc=10, seed=0)
    psr = sv.probabilistic_sharpe
    need = required_psr(prior_looks)
    checks = {
        "trades": {"ok": n_trades >= MIN_HOLDOUT_TRADES, "value": n_trades,
                   "need": f">= {MIN_HOLDOUT_TRADES}"},
        "sign": {"ok": bool(in_sample_sharpe and sharpe and in_sample_sharpe * sharpe > 0),
                 "value": sharpe, "need": f"same sign as in-sample ({in_sample_sharpe})"},
        "psr": {"ok": psr is not None and psr >= need, "value": psr,
                "need": f">= {need:.3f} ({prior_looks} earlier look(s))"},
    }
    return {
        "passed": all(c["ok"] for c in checks.values()), "checks": checks,
        "holdout": {"sharpe": sharpe, "psr": psr, "n_trades": n_trades},
        "required_psr": need, "prior_looks": prior_looks,
    }
