"""Random Baseline — validate whether the AI's veto selection adds edge.

Runs N simulations where each golden-cross signal is approved with probability p
(the AI's historical pass rate), then compares the distribution of outcomes to
the selected AI run.  Supports both single-symbol and universe (e.g. sp500) runs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import dotenv
dotenv.load_dotenv()

import plotly.graph_objects as go
import plotly.subplots as sp
import streamlit as st

from hermes.webui import run_cache as rc

BASELINES_DIR = Path(".hermes_cache/random_baselines")

st.set_page_config(page_title="Random Baseline — Hermes", layout="wide")
st.title("Random Baseline")
st.caption(
    "Does the AI's specific signal selection add edge, or would any random "
    "filter at the same pass rate do just as well?"
)

# ---------------------------------------------------------------------------
# Step 1 — Select AI run
# ---------------------------------------------------------------------------

st.subheader("Step 1 — Select the AI run to benchmark against")

all_runs = rc.list_runs()
ai_runs = [(k, m) for k, m in all_runs if m.strategy == "ema_crossover_ai"]

if not ai_runs:
    st.info("No `ema_crossover_ai` runs found. Run a backtest from the main page first.")
    st.stop()

run_labels = [
    f"{k[:8]} · {m.start[:10]}–{m.end[:10]} · {m.ticker or m.universe or '?'} · {m.sizer}"
    for k, m in ai_runs
]
selected_idx = st.selectbox(
    "AI run", range(len(ai_runs)), format_func=lambda i: run_labels[i]
)
sel_key, sel_meta = ai_runs[selected_idx]

cached = rc.load_run(sel_key)
if cached is None:
    st.error("Could not load the selected run from cache.")
    st.stop()

ai_result_dict, _ = cached
ai_metrics = ai_result_dict["metrics"]
ai_equity = ai_result_dict.get("equity_curve", [])

# Compute AI pass rate from trades + vetoed signals
ai_trades = ai_result_dict.get("trades", [])
ai_vetoed = ai_result_dict.get("vetoed_signals", [])
total_signals = len(ai_trades) + len(ai_vetoed)
ai_pass_rate = len(ai_trades) / total_signals if total_signals > 0 else 0.05

col_a, col_b, col_c = st.columns(3)
col_a.metric("AI Sharpe", f"{ai_metrics.get('sharpe') or 0:.3f}")
col_b.metric("AI CAGR", f"{(ai_metrics.get('cagr') or 0) * 100:.2f}%")
col_c.metric(
    "AI pass rate",
    f"{ai_pass_rate * 100:.1f}% ({len(ai_trades)}/{total_signals})",
    help="Signals approved by the AI vs total golden-cross signals seen",
)

_is_universe = sel_meta.universe is not None

# ---------------------------------------------------------------------------
# Step 2 — Simulation settings
# ---------------------------------------------------------------------------

st.subheader("Step 2 — Simulation settings")

import os as _os
_max_workers = _os.cpu_count() or 4

sc1, sc2, sc3, sc4 = st.columns(4)
sim_n = sc1.slider("Simulations (n)", 10, 500, 100, step=10)
sim_p = sc2.number_input(
    "Pass rate (p)", min_value=0.001, max_value=1.0,
    value=round(ai_pass_rate, 4), step=0.001, format="%.4f",
    help="Each signal is approved with this probability.",
)
sim_seed = sc3.number_input("Seed", value=42, step=1)
sim_workers = sc4.slider(
    "Workers", 1, _max_workers, min(_max_workers, 4),
    help=f"Parallel processes. {_max_workers} logical CPUs available.",
)

if not _is_universe:
    sim_symbol = sel_meta.ticker or "SPY"
else:
    sim_symbol = None  # universe — symbol not applicable

# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

_cache_inputs = json.dumps({
    "strategy": sel_meta.strategy,
    "strategy_hash": sel_meta.strategy_hash,
    "start": sel_meta.start,
    "end": sel_meta.end,
    "sizer": sel_meta.sizer,
    "universe": sel_meta.universe,
    "ticker": sim_symbol,
    "n": sim_n,
    "p": sim_p,
    "seed": int(sim_seed),
    # workers not included — doesn't affect results, only speed
}, sort_keys=True).encode()
_sim_key = hashlib.sha256(_cache_inputs).hexdigest()[:16]
_sim_dir = BASELINES_DIR / _sim_key
_results_path = _sim_dir / "results.json"

_cached_sims = None
if _results_path.exists():
    try:
        _cached_sims = json.loads(_results_path.read_text())
    except Exception:
        pass

if _cached_sims:
    st.info(f"⚡ Cached — {len(_cached_sims)} simulations. Click **Clear & re-run** to refresh.")
    if st.button("Clear & re-run", key="clear_sims"):
        _results_path.unlink(missing_ok=True)
        st.rerun()

run_sims = st.button("Run simulations", type="primary", disabled=bool(_cached_sims))

# ---------------------------------------------------------------------------
# Helpers shared by both run paths
# ---------------------------------------------------------------------------

def _parse_sizer(s: str):
    import re
    from hermes.strategy import EquityFraction, NotionalCash, RiskCash, RiskPercent, Units
    if s.startswith("EquityFraction"):
        m = re.search(r"fraction=([\d.]+)", s)
        return EquityFraction(float(m.group(1))) if m else EquityFraction(0.95)
    if s.startswith("NotionalCash"):
        m = re.search(r"cash=([\d.]+)", s)
        return NotionalCash(float(m.group(1))) if m else NotionalCash(10000)
    if s.startswith("RiskPercent"):
        m = re.search(r"risk=([\d.]+)", s)
        return RiskPercent(float(m.group(1))) if m else RiskPercent(0.01)
    if s.startswith("Units"):
        m = re.search(r"units=([\d.]+)", s)
        return Units(float(m.group(1))) if m else Units(100)
    return EquityFraction(0.95)


def _make_progress_cb(bar_widget, txt_widget):
    import time as _time
    _t0 = [None]

    def cb(i: int, n: int) -> None:
        if _t0[0] is None:
            _t0[0] = _time.monotonic()
        frac = i / n
        bar_widget.progress(frac)
        elapsed = _time.monotonic() - _t0[0]
        if 0 < frac < 1.0 and elapsed > 0.5:
            eta = elapsed / frac * (1.0 - frac)
            txt_widget.caption(f"Simulation {i}/{n}  |  elapsed {elapsed:.0f}s  eta {eta:.0f}s")
        elif frac >= 1.0:
            txt_widget.caption(f"Done — {n} simulations in {elapsed:.1f}s")

    return cb


# ---------------------------------------------------------------------------
# Run simulations
# ---------------------------------------------------------------------------

if run_sims:
    sizer = _parse_sizer(sel_meta.sizer)
    start_dt = datetime.fromisoformat(sel_meta.start).replace(tzinfo=UTC)
    end_dt = datetime.fromisoformat(sel_meta.end).replace(tzinfo=UTC)

    prog_bar = st.progress(0.0)
    prog_txt = st.empty()
    progress_cb = _make_progress_cb(prog_bar, prog_txt)

    try:
        if _is_universe:
            # --- Universe path -------------------------------------------
            from hermes.backtest.random_baseline import run_universe_random_simulations
            from hermes.webui.universes import load_calendar
            from hermes.data import YFinanceSource
            from strategies.ema_crossover_ai import EmaCrossoverAI, D1

            sim_results = run_universe_random_simulations(
                strategy_factory=EmaCrossoverAI,
                source=YFinanceSource(),
                calendar=load_calendar(sel_meta.universe),
                timeframes=[D1],
                start=start_dt,
                end=end_dt,
                starting_cash=100_000,
                sizer=sizer,
                unconstrained=sel_meta.unconstrained,
                n=sim_n,
                p=float(sim_p),
                seed=int(sim_seed),
                workers=sim_workers,
                progress_cb=progress_cb,
            )

        else:
            # --- Single-symbol path --------------------------------------
            from hermes.backtest.random_baseline import run_random_simulations
            from strategies.ema_crossover_ai import build_backtest
            from hermes import Symbol

            symbol = Symbol(sim_symbol, sel_meta.source)
            sim_results = run_random_simulations(
                build_backtest,
                n=sim_n,
                p=float(sim_p),
                seed=int(sim_seed),
                workers=sim_workers,
                progress_cb=progress_cb,
                symbol=symbol,
                start=start_dt,
                end=end_dt,
                sizer=sizer,
            )

    except Exception as exc:
        st.error(f"Simulation failed: {exc}")
        st.stop()

    _sim_dir.mkdir(parents=True, exist_ok=True)
    _results_path.write_text(json.dumps(sim_results))
    _cached_sims = sim_results
    prog_bar.empty()
    st.success(f"Done — {len(sim_results)} simulations complete.")
    st.rerun()

# ---------------------------------------------------------------------------
# Step 3 — Results
# ---------------------------------------------------------------------------

if not _cached_sims:
    st.stop()

sims = _cached_sims
n_sims = len(sims)

st.subheader("Step 3 — Results")

# ---------------------------------------------------------------------------
# Panel A — Distribution plots
# ---------------------------------------------------------------------------

st.markdown("#### A — Distribution of key metrics")

DIST_METRICS = [
    ("sharpe",       "Sharpe ratio", False),
    ("cagr",         "CAGR",         True),
    ("max_drawdown", "Max drawdown", True),
    ("num_trades",   "Num trades",   False),
]


def _safe(v, pct=False):
    if v is None:
        return None
    return v * 100 if pct else v


fig_dist = sp.make_subplots(rows=1, cols=4, subplot_titles=[lbl for _, lbl, _ in DIST_METRICS])

for i, (key, label, is_pct) in enumerate(DIST_METRICS):
    vals = [_safe(r.get(key), is_pct) for r in sims if r.get(key) is not None]
    ai_val = _safe(ai_metrics.get(key), is_pct)
    if not vals:
        continue
    col = i + 1
    fig_dist.add_trace(
        go.Box(y=vals, name=label, marker_color="#94a3b8", showlegend=False),
        row=1, col=col,
    )
    if ai_val is not None:
        fig_dist.add_hline(
            y=ai_val, line=dict(color="#ef4444", width=2, dash="dash"),
            annotation_text=f"AI: {ai_val:.2f}{'%' if is_pct else ''}",
            annotation_font_color="#ef4444",
            row=1, col=col,
        )

fig_dist.update_layout(height=400, margin=dict(l=0, r=0, t=40, b=0))
st.plotly_chart(fig_dist, use_container_width=True)

# ---------------------------------------------------------------------------
# Panel B — Percentile table
# ---------------------------------------------------------------------------

st.markdown("#### B — Percentile table")


def _percentile(vals: list[float], q: float) -> float:
    s = sorted(v for v in vals if v is not None)
    if not s:
        return float("nan")
    idx = q * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def _ai_pct_rank(vals: list[float], ai_val: float | None) -> str:
    if ai_val is None:
        return "—"
    s = sorted(v for v in vals if v is not None)
    if not s:
        return "—"
    rank = sum(1 for v in s if v <= ai_val) / len(s)
    return f"{rank * 100:.0f}%"


TABLE_METRICS = [
    ("sharpe",        "Sharpe",        False),
    ("cagr",          "CAGR",          True),
    ("max_drawdown",  "Max DD",        True),
    ("win_rate",      "Win Rate",      True),
    ("profit_factor", "Profit Factor", False),
    ("num_trades",    "Num Trades",    False),
]

rows = []
for key, label, is_pct in TABLE_METRICS:
    vals = [_safe(r.get(key), is_pct) for r in sims if r.get(key) is not None]
    ai_val = _safe(ai_metrics.get(key), is_pct)
    fmt = (lambda v: f"{v:.2f}%" if v is not None else "—") if is_pct \
        else (lambda v: f"{v:.3f}" if v is not None else "—")
    rows.append({
        "Metric": label,
        "AI Run": fmt(ai_val),
        "Random p5": fmt(_percentile(vals, 0.05)),
        "Random p50": fmt(_percentile(vals, 0.50)),
        "Random p95": fmt(_percentile(vals, 0.95)),
        "AI Percentile": _ai_pct_rank(vals, ai_val),
    })

import pandas as pd
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Panel C — Summary verdict
# ---------------------------------------------------------------------------

st.markdown("#### C — Summary verdict")

sharpe_vals = [r.get("sharpe") for r in sims if r.get("sharpe") is not None]
cagr_vals = [r.get("cagr") for r in sims if r.get("cagr") is not None]
ai_sharpe = ai_metrics.get("sharpe")
ai_cagr = ai_metrics.get("cagr")

lines = []
if sharpe_vals and ai_sharpe is not None:
    sharpe_pct = sum(1 for v in sharpe_vals if v <= ai_sharpe) / len(sharpe_vals) * 100
    verdict = "beats" if sharpe_pct >= 50 else "underperforms"
    lines.append(
        f"The AI run's Sharpe ({ai_sharpe:.3f}) **{verdict} {sharpe_pct:.0f}%** "
        f"of {n_sims} random runs at the same pass rate (p={sim_p:.3f})."
    )
if cagr_vals and ai_cagr is not None:
    cagr_pct = sum(1 for v in cagr_vals if v <= ai_cagr) / len(cagr_vals) * 100
    verdict = "beats" if cagr_pct >= 50 else "underperforms"
    lines.append(
        f"For CAGR ({ai_cagr * 100:.2f}%) it **{verdict} {cagr_pct:.0f}%** of random runs."
    )

if lines:
    st.markdown("  \n".join(lines))
else:
    st.caption("Not enough data to compute a verdict.")

# ---------------------------------------------------------------------------
# Panel D — Equity curve fan
# ---------------------------------------------------------------------------

st.markdown("#### D — Equity curve fan")

fig_fan = go.Figure()

for i, row in enumerate(sims):
    ec = row.get("equity_curve", [])
    if not ec:
        continue
    fig_fan.add_trace(go.Scatter(
        x=[e[0] for e in ec],
        y=[e[1] for e in ec],
        mode="lines",
        line=dict(color="rgba(148,163,184,0.3)", width=1),
        showlegend=(i == 0),
        name=f"Random ({n_sims} runs)",
        legendgroup="random",
    ))

if ai_equity:
    fig_fan.add_trace(go.Scatter(
        x=[e[0] for e in ai_equity],
        y=[e[1] for e in ai_equity],
        mode="lines",
        line=dict(color="#2563eb", width=3),
        name="AI run",
    ))

fig_fan.update_layout(
    height=500,
    margin=dict(l=0, r=0, t=10, b=0),
    yaxis=dict(title="Equity ($)"),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
)
st.plotly_chart(fig_fan, use_container_width=True)
