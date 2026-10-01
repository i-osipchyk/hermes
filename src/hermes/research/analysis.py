"""Execute the analyse-results rubric as computations, not prose.

The ``hermes-analyze-results`` skill asks seven questions of a backtest — is the P&L
concentrated, does it survive costs, does it hold out of sample, is it distinguishable
from luck, was it the strategy or the market, did the AI gate do anything. Every one of
those is computable from primitives Hermes already ships. Before ADR-0010 the answers
were *described* rather than run, because the reviewer had no way to run them.

This module runs them and emits machine-readable evidence: an :class:`Analysis` whose
``to_dict()`` is what the CLI prints, the UI renders, and the headless reviewer reads.
The division of labour matters — **Hermes computes the numbers, the reviewer interprets
them.** An LLM should never be the thing that calculates whether an edge survives costs.

Each axis is independent and individually skippable, because they differ enormously in
price: ``trades`` is free, ``costs`` is three backtests, ``baseline`` is *n* of them.
An axis that raises records the error and the rest still run — a partial analysis is
worth far more than an exception.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Cheapest first. `baseline` and `walkforward` are deliberately out of the default set.
DEFAULT_AXES: tuple[str, ...] = (
    "trades", "costs", "params", "oos", "validation", "regime", "gate",
)
ALL_AXES: tuple[str, ...] = (*DEFAULT_AXES, "walkforward", "baseline")

# Axes that can be computed from a *stored* run (a RestoredResult), because they only
# read its serialised dict. Everything else needs the live BacktestResult — `validate`
# wants real ``Trade`` objects and `regime_analysis` wants ``benchmark_equity``, neither
# of which survives JSON. Asking for those from the ledger alone must trigger a re-run,
# not an AttributeError.
RESTORABLE_AXES: frozenset[str] = frozenset({"trades", "gate"})


@dataclass(slots=True)
class AxisResult:
    """One rubric axis: what it computed, and a one-line reading of it."""

    axis: str
    ran: bool
    note: str                                    # plain-language reading, for a human or an LLM
    data: dict[str, Any] = field(default_factory=dict)   # the numbers
    error: str | None = None                     # why it didn't run

    def __post_init__(self) -> None:
        # `note` is contractually ONE line: it is printed in an aligned column and
        # read back by the reviewer. Upstream strings (e.g. SampleQuality.warning)
        # contain newlines, which would break both.
        self.note = " ".join(self.note.split())

    def to_dict(self) -> dict:
        return {"axis": self.axis, "ran": self.ran, "note": self.note,
                "data": self.data, "error": self.error}


@dataclass(slots=True)
class Analysis:
    """The evidence for one run, axis by axis."""

    axes: list[AxisResult] = field(default_factory=list)
    run_key: str | None = None
    strategy: str | None = None

    def __getitem__(self, axis: str) -> AxisResult:
        for a in self.axes:
            if a.axis == axis:
                return a
        raise KeyError(axis)

    @property
    def ran(self) -> list[str]:
        return [a.axis for a in self.axes if a.ran]

    @property
    def failed(self) -> list[str]:
        return [a.axis for a in self.axes if not a.ran]

    @property
    def notes(self) -> list[str]:
        """The one-liners, in rubric order — the summary a reviewer starts from."""
        return [f"{a.axis}: {a.note}" for a in self.axes]

    def to_dict(self) -> dict:
        return {
            "run_key": self.run_key,
            "strategy": self.strategy,
            "axes": [a.to_dict() for a in self.axes],
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _r(x: Any, places: int = 4) -> Any:
    """Round for display without turning None or a non-number into one."""
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, (int, float)):
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
        return round(float(x), places)
    return x


def _metrics(result: Any) -> dict:
    m = result.metrics
    return {
        "total_return": _r(m.total_return),
        "cagr": _r(m.cagr),
        "sharpe": _r(m.sharpe),
        "sortino": _r(m.sortino),
        "max_drawdown": _r(m.max_drawdown),
        "win_rate": _r(m.win_rate),
        "profit_factor": _r(m.profit_factor),
        "num_trades": m.num_trades,
        "num_params": m.num_params,
        "parameter_adjusted_sharpe": _r(m.parameter_adjusted_sharpe),
    }


# ---------------------------------------------------------------------------
# run coverage — before you can judge an edge, check the run happened
# ---------------------------------------------------------------------------

def run_coverage(result: dict) -> dict:
    """Did the engine actually step bars, and did anything happen when it did?"""
    curve = result.get("equity_curve") or []
    return {
        "steps": len(curve),
        "first_step": curve[0][0] if curve else None,
        "last_step": curve[-1][0] if curve else None,
        "trades": len(result.get("trades") or []),
        "vetoed_signals": len(result.get("vetoed_signals") or []),
    }


def diagnose_zero_trades(result: dict) -> str:
    """Why a run produced no trades.

    This is the most common failure in strategy development and the three causes need
    completely different fixes, so guessing at all three ("check the warmup, the entry
    condition, and the date window") is close to useless. The distinction is already
    derivable from the stored result — an empty equity curve means no bars were ever
    stepped, a populated one means the engine ran and the logic never fired — so say
    which it is.
    """
    c = run_coverage(result)
    if c["steps"] == 0:
        return (
            "the engine stepped ZERO bars — no data reached the strategy. Check the "
            "symbol spelling for this source, the date window, and whether the source "
            "can supply the Lead-in the declared Indicators need."
        )
    window = f"{str(c['first_step'])[:10]} → {str(c['last_step'])[:10]}"
    if c["vetoed_signals"]:
        return (
            f"{c['steps']} bars stepped over {window} and {c['vetoed_signals']} signals "
            f"formed, but every one was vetoed — the entry logic works; the AI gate (or "
            f"its confidence threshold) is rejecting everything."
        )
    return (
        f"{c['steps']} bars stepped over {window}, so the data is fine and the engine "
        f"ran — the entry condition simply never became true. Look at the thresholds, "
        f"not the window: loosen them and check an Indicator's value on a few bars."
    )


# ---------------------------------------------------------------------------
# the axes
# ---------------------------------------------------------------------------

def axis_trades(result: Any) -> AxisResult:
    """Rubric 1 — is the P&L an edge, or two lucky trades?

    Nothing in Hermes computed this before, yet it is the cheapest and most often
    decisive check: a strategy whose entire return comes from the single best trade
    has no edge, whatever the Sharpe says.
    """
    trades = result.to_dict()["trades"]
    n = len(trades)
    if n == 0:
        return AxisResult("trades", False, "no trades to analyse",
                          error="the run produced zero trades")

    pnls = sorted((t.get("net_pnl") or 0.0 for t in trades), reverse=True)
    total = sum(pnls)
    winners = [p for p in pnls if p > 0]
    gross_win = sum(winners)

    # Share of all *winning* P&L held by the top trade / top 5 — robust when the
    # net total is near zero, where a share-of-net is meaningless.
    top1 = (pnls[0] / gross_win) if gross_win > 0 else None
    top5 = (sum(pnls[:5]) / gross_win) if gross_win > 0 else None

    sides = Counter(t.get("side") for t in trades)
    reasons = Counter(t.get("exit_reason") for t in trades)
    years = Counter(str(t["entry_time"])[:4] for t in trades if t.get("entry_time"))
    busiest_year, busiest_n = (years.most_common(1)[0] if years else (None, 0))

    data = {
        "num_trades": n,
        "net_pnl": _r(total, 2),
        "top_trade_share_of_wins": _r(top1),
        "top_5_share_of_wins": _r(top5),
        "sides": dict(sides),
        "exit_reasons": dict(reasons),
        "trades_per_year": dict(sorted(years.items())),
        "concentration_in_busiest_year": _r(busiest_n / n) if n else None,
    }

    flags = []
    if top1 is not None and top1 > 0.5:
        flags.append(f"the single best trade is {top1:.0%} of all winning P&L")
    elif top5 is not None and top5 > 0.8 and n > 10:
        flags.append(f"the top 5 trades are {top5:.0%} of all winning P&L")
    if len(sides) == 1:
        flags.append(f"every trade is {next(iter(sides))} — untested in the other direction")
    if years and busiest_n / n > 0.6 and len(years) > 1:
        flags.append(f"{busiest_n / n:.0%} of trades fall in {busiest_year}")
    note = "; ".join(flags) if flags else f"{n} trades, P&L not concentrated in a few outliers"
    return AxisResult("trades", True, note, data)


def axis_costs(backtest: Any, multipliers: tuple[float, ...] = (0.0, 1.0, 2.0)) -> AxisResult:
    """Rubric 3 — does the edge survive realistic and pessimistic friction?"""
    from ..backtest import cost_sensitivity

    results = cost_sensitivity(backtest, multipliers=multipliers)
    data = {str(m): _metrics(r) for m, r in sorted(results.items())}

    base = results.get(1.0)
    free = results.get(0.0)
    heavy = results.get(2.0)
    flags = []
    if base is not None and (base.metrics.total_return or 0) <= 0:
        flags.append("no edge at realistic (1x) costs")
    if (
        free is not None and base is not None
        and (free.metrics.total_return or 0) > 0 >= (base.metrics.total_return or 0)
    ):
        flags.append("profitable only with costs zeroed — this is a cost artifact, not an edge")
    if heavy is not None and (heavy.metrics.total_return or 0) <= 0:
        flags.append("does not survive doubled costs")
    note = "; ".join(flags) if flags else "edge survives 1x and 2x costs"
    return AxisResult("costs", True, note, data)


def axis_params(backtest: Any, *, metric: str = "sharpe", steps: int = 4,
                relative: float = 0.25) -> AxisResult:
    """Robustness — is the reported result a plateau or a spike?

    The other half of robustness testing alongside ``costs``. A backtest reports one
    point in parameter space; if the metric collapses one notch away, that point was
    found rather than earned and the live result will be the neighbourhood average.
    Parameters are swept one at a time around their configured values — this is a
    stability check, not an optimisation, and conflating the two is how a search gets
    mistaken for an edge.
    """
    from ..backtest import param_sensitivity

    ps = param_sensitivity(backtest, metric=metric, steps=steps, relative=relative)
    if not ps.curves:
        return AxisResult("params", False, "the strategy declares no Parameters to sweep",
                          error="no declared Parameters")

    data = ps.to_dict()
    fragile = ps.fragile
    unmeasured = [c.name for c in ps.curves if c.is_plateau is None]
    if fragile:
        worst = min(
            (c for c in ps.curves if c.name in fragile),
            key=lambda c: c.degradation if c.degradation is not None else 0.0,
        )
        note = (f"fragile: {', '.join(fragile)} — nudging {worst.name} off "
                f"{worst.base_value} drops {metric} to "
                f"{worst.degradation:.0%} of its reported value; the result is a spike, "
                f"not a plateau")
    elif unmeasured and len(unmeasured) == len(ps.curves):
        note = f"could not judge stability ({metric} undefined around the chosen values)"
    else:
        note = (f"stable: {metric} holds across the neighbourhood of every swept "
                f"parameter ({len(ps.curves)} checked)")
    return AxisResult("params", True, note, data)


def axis_oos(backtest: Any, is_frac: float = 0.7) -> AxisResult:
    """Rubric 7 (cheap form) — a single in-sample / out-of-sample split.

    No parameter fitting happens here: the same parameters are simply scored on both
    halves. It catches a result that only exists in one era. For fitted parameters use
    the ``walkforward`` axis.
    """
    from ..backtest import split_isoos

    is_result, oos_result = split_isoos(backtest, is_frac=is_frac)
    data = {"is_frac": is_frac, "in_sample": _metrics(is_result), "out_of_sample": _metrics(oos_result)}

    is_sh, oos_sh = is_result.metrics.sharpe, oos_result.metrics.sharpe
    if oos_result.metrics.num_trades == 0:
        note = "no trades out of sample — the split leaves nothing to judge"
    elif is_sh is None or oos_sh is None:
        note = "split ran but Sharpe is undefined on one half"
    elif is_sh <= 0:
        # Nothing to hold up: there was no in-sample edge to carry forward.
        note = f"no in-sample edge to begin with (Sharpe {is_sh:.2f}; {oos_sh:.2f} out of sample)"
    elif oos_sh <= 0:
        note = f"in-sample Sharpe {is_sh:.2f} does not carry out of sample ({oos_sh:.2f})"
    elif oos_sh < is_sh * 0.5:
        note = f"out-of-sample Sharpe {oos_sh:.2f} is less than half the in-sample {is_sh:.2f}"
    else:
        note = f"holds up: Sharpe {is_sh:.2f} in sample, {oos_sh:.2f} out"
    return AxisResult("oos", True, note, data)


def axis_walkforward(backtest: Any, *, is_frac: float = 0.6,
                     param_grid: dict | None = None) -> AxisResult:
    """Rubric 7 (full form) — fit parameters in-sample, score them out-of-sample.

    Expensive: one backtest per grid point per window. The stitched OOS curve is the
    only number worth quoting.
    """
    from ..backtest import WalkForward

    wf = WalkForward(
        template=backtest,
        total_start=backtest.start,
        total_end=backtest.end,
        is_frac=is_frac,
        param_grid=param_grid,
    )
    res = wf.run()
    data = {
        "anchored": res.anchored,
        "num_windows": len(res.windows),
        "stitched_out_of_sample": _metrics(res.oos_result),
        "windows": [
            {
                "is_start": w.is_start.isoformat(), "is_end": w.is_end.isoformat(),
                "oos_start": w.oos_start.isoformat(), "oos_end": w.oos_end.isoformat(),
                "best_params": w.best_params,
                "oos": _metrics(w.oos_result),
            }
            for w in res.windows
        ],
    }
    chosen = [w.best_params for w in res.windows if w.best_params]
    unstable = len({tuple(sorted(p.items())) for p in chosen}) > 1 if chosen else False
    sh = res.oos_result.metrics.sharpe
    bits = [f"stitched OOS Sharpe {sh:.2f}" if sh is not None else "stitched OOS Sharpe undefined"]
    if unstable:
        bits.append("the best parameters differ between windows — the optimum is not stable")
    return AxisResult("walkforward", True, "; ".join(bits), data)


def axis_validation(result: Any, *, seed: int = 42) -> AxisResult:
    """Rubric 4 — is the result distinguishable from luck, and is the sample big enough?"""
    from ..backtest import validate

    sv = validate(result, seed=seed)
    data = sv.to_dict()

    flags = []
    q = sv.sample_quality
    if q.warning:
        flags.append(q.warning)
    if sv.probabilistic_sharpe is not None and sv.probabilistic_sharpe < 0.95:
        flags.append(f"probabilistic Sharpe {sv.probabilistic_sharpe:.0%} — P(SR>0) below 95%")
    if sv.deflated_sharpe is not None and sv.deflated_sharpe < 0.95:
        flags.append(f"deflated Sharpe {sv.deflated_sharpe:.0%} once the search is accounted for")
    ci = sv.ci.sharpe
    if ci is not None and getattr(ci, "low", None) is not None and ci.low <= 0:
        flags.append(f"Sharpe confidence interval includes zero (low {ci.low:.2f})")
    note = "; ".join(flags) if flags else "survives the statistical checks"
    return AxisResult("validation", True, note, data)


def axis_regime(result: Any, *, external_benchmark: list | None = None) -> AxisResult:
    """Rubric 6 — was it the strategy, or being long in a bull market?"""
    from ..backtest import regime_analysis

    analysis = regime_analysis(result, external_benchmark=external_benchmark)
    if not analysis.regimes:
        return AxisResult("regime", False, "no benchmark series on this run",
                          error="regime_analysis needs benchmark_equity or an external benchmark")

    rows = [
        {"label": r.label, "n_trades": r.n_trades, "sharpe": _r(r.sharpe),
         "win_rate": _r(r.win_rate), "profit_factor": _r(r.profit_factor),
         "total_pnl": _r(r.total_pnl, 2)}
        for r in analysis.regimes
    ]
    attributed = sum(r.n_trades for r in analysis.regimes)
    data = {"regimes": rows, "trades_attributed": attributed}

    bull = sum(r.total_pnl for r in analysis.regimes if r.label.startswith("Bull"))
    bear = sum(r.total_pnl for r in analysis.regimes if r.label.startswith("Bear"))
    if attributed == 0:
        note = "no trades fell inside a classified regime"
    elif bull <= 0 and bear <= 0:
        note = f"loses in both trend regimes (bull {bull:,.0f}, bear {bear:,.0f})"
    elif bull > 0 >= bear:
        note = f"all profit comes from Bull regimes (bull {bull:,.0f} vs bear {bear:,.0f})"
    elif bear > 0 >= bull:
        note = f"all profit comes from Bear regimes (bear {bear:,.0f} vs bull {bull:,.0f})"
    else:
        note = f"profitable in both trend regimes (bull {bull:,.0f}, bear {bear:,.0f})"
    return AxisResult("regime", True, note, data)


def axis_gate(result: Any) -> AxisResult:
    """Rubric 8a — was the AI gate actually in effect for this run?

    Cheap and non-negotiable for an AI-gated strategy: a run whose provider errored
    approved trades without a real decision, so its metrics describe a *less gated*
    strategy and must not be compared against a clean run.
    """
    d = result.to_dict()
    summary = d.get("llm_summary")
    if not summary:
        return AxisResult("gate", False, "no AI advisor on this run", error="no llm_summary")

    fail_open = int(summary.get("fail_open_calls") or 0)
    total = int(summary.get("total_calls") or 0)
    data = {
        **{k: _r(v) for k, v in summary.items()},
        "vetoed_signals": len(d.get("vetoed_signals", [])),
    }
    if fail_open and total:
        note = (f"{fail_open}/{total} advisor calls failed open — this run is less gated "
                f"than configured; do not compare it to a clean run")
    elif total == 0:
        note = "an advisor was configured but never consulted"
    else:
        rate = summary.get("approval_rate")
        note = (f"{total} calls, gate clean"
                + (f", approval rate {rate:.0%}" if isinstance(rate, (int, float)) else ""))
    return AxisResult("gate", True, note, data)


def axis_baseline(build_backtest: Any, *, approval_rate: float, n: int = 50,
                  seed: int = 42, workers: int = 1) -> AxisResult:
    """Rubric 8b — does the AI gate beat a coin weighted to the same approval rate?

    The control the AI-gate question needs: if the real gate's metrics land inside the
    random distribution, the gate has shown no skill — it is just trading less.
    Expensive (``n`` full backtests), so it is opt-in.
    """
    from ..backtest import run_random_simulations

    sims = run_random_simulations(
        build_backtest, n=n, p=approval_rate, seed=seed, workers=workers
    )
    returns = sorted(s.get("total_return") for s in sims if s.get("total_return") is not None)
    sharpes = sorted(s.get("sharpe") for s in sims if s.get("sharpe") is not None)

    def pct(xs: list[float], q: float) -> float | None:
        if not xs:
            return None
        return _r(xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))])

    data = {
        "n": n, "approval_rate": approval_rate, "seed": seed,
        "total_return": {"p5": pct(returns, 0.05), "p50": pct(returns, 0.5),
                         "p95": pct(returns, 0.95), "samples": len(returns)},
        "sharpe": {"p5": pct(sharpes, 0.05), "p50": pct(sharpes, 0.5),
                   "p95": pct(sharpes, 0.95), "samples": len(sharpes)},
    }
    note = (f"{len(returns)} random runs at p={approval_rate:.0%}: "
            f"return p5/p50/p95 = {data['total_return']['p5']}/"
            f"{data['total_return']['p50']}/{data['total_return']['p95']} — "
            f"compare the real run against this distribution")
    return AxisResult("baseline", True, note, data)


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

def analyze(
    backtest: Any | None = None,
    result: Any | None = None,
    *,
    axes: tuple[str, ...] | list[str] = DEFAULT_AXES,
    run_key: str | None = None,
    strategy: str | None = None,
    cost_multipliers: tuple[float, ...] = (0.0, 1.0, 2.0),
    param_steps: int = 4,
    is_frac: float = 0.7,
    seed: int = 42,
    param_grid: dict | None = None,
    baseline_n: int = 50,
    baseline_workers: int = 1,
    build_backtest: Any | None = None,
) -> Analysis:
    """Run the requested rubric axes and collect the evidence.

    ``result`` alone covers the axes that only read a finished run (``trades``,
    ``validation``, ``regime``, ``gate``). ``backtest`` is additionally required for
    the axes that re-run it (``costs``, ``oos``, ``walkforward``), and
    ``build_backtest`` for ``baseline``. An axis whose inputs are missing is recorded
    as not-run with the reason, rather than raising — so a partial analysis still
    lands.
    """
    out = Analysis(run_key=run_key, strategy=strategy)

    if result is None and backtest is not None:
        result = backtest.run()

    def add(axis: str, fn: Any, needs: Any, why: str) -> None:
        if axis not in axes:
            return
        if needs is None:
            out.axes.append(AxisResult(axis, False, f"skipped — {why}", error=why))
            return
        try:
            out.axes.append(fn())
        except Exception as e:  # noqa: BLE001 — one broken axis must not lose the others
            out.axes.append(
                AxisResult(axis, False, f"failed: {type(e).__name__}: {e}",
                           error=f"{type(e).__name__}: {e}")
            )

    add("trades", lambda: axis_trades(result), result, "needs a result")
    add("costs", lambda: axis_costs(backtest, cost_multipliers), backtest,
        "needs a Backtest to re-run")
    add("params", lambda: axis_params(backtest, steps=param_steps), backtest,
        "needs a Backtest to re-run")
    add("oos", lambda: axis_oos(backtest, is_frac), backtest, "needs a Backtest to re-run")
    add("walkforward", lambda: axis_walkforward(backtest, is_frac=is_frac, param_grid=param_grid),
        backtest, "needs a Backtest to re-run")
    add("validation", lambda: axis_validation(result, seed=seed), result, "needs a result")
    add("regime", lambda: axis_regime(result), result, "needs a result")
    add("gate", lambda: axis_gate(result), result, "needs a result")

    if "baseline" in axes:
        rate = None
        if result is not None:
            rate = (result.to_dict().get("llm_summary") or {}).get("approval_rate")
        if build_backtest is None:
            out.axes.append(AxisResult("baseline", False, "skipped — needs build_backtest",
                                       error="needs build_backtest"))
        elif rate is None:
            out.axes.append(AxisResult(
                "baseline", False,
                "skipped — no advisor approval rate to match; the baseline is only "
                "meaningful for an AI-gated run",
                error="no llm_summary.approval_rate on this run"))
        else:
            add("baseline",
                lambda: axis_baseline(build_backtest, approval_rate=float(rate),
                                      n=baseline_n, seed=seed, workers=baseline_workers),
                build_backtest, "needs build_backtest")

    # Report in rubric order regardless of which were requested.
    order = {a: i for i, a in enumerate(ALL_AXES)}
    out.axes.sort(key=lambda a: order.get(a.axis, 99))
    return out
