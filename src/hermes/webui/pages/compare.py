"""Compare two saved backtest runs side-by-side.

Second Streamlit page (accessible via sidebar navigation).
"""

from __future__ import annotations

import dotenv
dotenv.load_dotenv()

from datetime import UTC, datetime

import plotly.graph_objects as go
import streamlit as st

from hermes.webui import run_cache as rc
from hermes.webui.review import load_result

st.set_page_config(page_title="Compare Runs — Hermes", layout="wide")
st.title("Compare runs")


def _fmt_pct(x):
    return "—" if x is None else f"{x * 100:.2f}%"


def _fmt_num(x):
    return "—" if x is None else f"{x:.2f}"


def _restore_result(result_dict: dict):
    """Rebuild a display-ready result from a cached dict (mirrors review.load_result)."""
    from hermes.backtest.result import Metrics
    metrics = Metrics(**result_dict["metrics"])
    equity_curve = [
        (datetime.fromisoformat(ts).replace(tzinfo=UTC), eq)
        for ts, eq in result_dict.get("equity_curve", [])
    ]

    class _R:
        pass

    r = _R()
    r.metrics = metrics
    r.equity_curve = equity_curve
    r.trades = result_dict.get("trades", [])
    return r


runs = rc.list_runs()

if len(runs) < 2:
    st.info("Need at least 2 saved runs to compare. Run a backtest from the main page first.")
    st.stop()

labels = [f"{k[:8]} · {m.label}" for k, m in runs]

col_a, col_b = st.columns(2)
a_idx = col_a.selectbox("Run A", range(len(runs)), format_func=lambda i: labels[i], key="cmp_a")
b_idx = col_b.selectbox("Run B", range(len(runs)), format_func=lambda i: labels[i],
                        index=min(1, len(runs) - 1), key="cmp_b")

key_a, meta_a = runs[a_idx]
key_b, meta_b = runs[b_idx]

cached_a = rc.load_run(key_a)
cached_b = rc.load_run(key_b)

if cached_a is None or cached_b is None:
    st.error("Could not load one or both runs from cache.")
    st.stop()

res_dict_a, _ = cached_a
res_dict_b, _ = cached_b

result_a = _restore_result(res_dict_a)
result_b = _restore_result(res_dict_b)

ma = result_a.metrics
mb = result_b.metrics

# --- Metrics comparison table -----------------------------------------------

st.subheader("Metrics comparison")

METRIC_DEFS = [
    ("Total return",  "total_return",  True,  True),
    ("CAGR",          "cagr",          True,  True),
    ("Sharpe",        "sharpe",        False, True),
    ("Sortino",       "sortino",       False, True),
    ("Max drawdown",  "max_drawdown",  True,  False),
    ("Win rate",      "win_rate",      True,  True),
    ("Profit factor", "profit_factor", False, True),
    ("Num trades",    "num_trades",    False, None),
    ("Calmar",        "calmar",        False, True),
    ("Omega ratio",   "omega_ratio",   False, True),
    ("Adj. Sharpe",   "parameter_adjusted_sharpe", False, True),
]

rows = []
for label, attr, is_pct, higher_is_better in METRIC_DEFS:
    va = getattr(ma, attr, None)
    vb = getattr(mb, attr, None)
    fmt = _fmt_pct if is_pct else _fmt_num
    if va is not None and vb is not None and isinstance(va, (int, float)) and isinstance(vb, (int, float)):
        delta = vb - va
        delta_str = (_fmt_pct(delta) if is_pct else _fmt_num(delta))
        if higher_is_better is True:
            arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
        elif higher_is_better is False:
            arrow = "▼" if delta > 0 else ("▲" if delta < 0 else "—")
        else:
            arrow = ""
        delta_display = f"{arrow} {delta_str}"
    else:
        delta_display = "—"

    rows.append({
        "Metric": label,
        f"A  {meta_a.label}": fmt(va),
        f"B  {meta_b.label}": fmt(vb),
        "Δ (B − A)": delta_display,
    })

import pandas as pd
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# --- Overlaid equity curves -------------------------------------------------

st.subheader("Equity curves")

fig = go.Figure()

if result_a.equity_curve:
    ts_a = [t for t, _ in result_a.equity_curve]
    eq_a = [e for _, e in result_a.equity_curve]
    fig.add_trace(go.Scatter(
        x=ts_a, y=eq_a,
        name=f"A · {meta_a.label}",
        line=dict(color="#2563eb"),
    ))

if result_b.equity_curve:
    ts_b = [t for t, _ in result_b.equity_curve]
    eq_b = [e for _, e in result_b.equity_curve]
    fig.add_trace(go.Scatter(
        x=ts_b, y=eq_b,
        name=f"B · {meta_b.label}",
        line=dict(color="#f59e0b"),
    ))

fig.update_layout(
    height=450,
    margin=dict(l=0, r=0, t=10, b=0),
    yaxis=dict(title="Equity ($)"),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
)
st.plotly_chart(fig, use_container_width=True)

# --- Trade summary ----------------------------------------------------------

st.subheader("Trade summary")

tc1, tc2 = st.columns(2)

trades_a = result_a.trades
trades_b = result_b.trades

tc1.metric("Trades (A)", ma.num_trades)
tc1.metric("Win rate (A)", _fmt_pct(ma.win_rate))
tc2.metric("Trades (B)", mb.num_trades)
tc2.metric("Win rate (B)", _fmt_pct(mb.win_rate))

if ma.num_trades and mb.num_trades:
    trade_diff = mb.num_trades - ma.num_trades
    st.caption(
        f"B has {'more' if trade_diff > 0 else 'fewer'} trades than A "
        f"({abs(trade_diff):+d} difference)."
    )
