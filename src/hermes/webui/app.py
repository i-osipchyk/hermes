"""Hermes backtesting web UI (Streamlit). Launch with ``hermes-ui``.

Runs a strategy's Backtest in-process and shows the equity curve, metrics, trades, and a
Claude Code–driven review (ADR-0008). Run as a script by Streamlit, so imports are
absolute.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import plotly.graph_objects as go
import streamlit as st

from hermes.backtest import BacktestResult
from hermes.webui import discovery, review, sources, universes

st.set_page_config(page_title="Hermes Backtester", layout="wide")
st.title("Hermes — backtesting")

_SINGLE = "— single symbol —"


def _fmt_pct(x):
    return "—" if x is None else f"{x * 100:.2f}%"


def _fmt_num(x):
    return "—" if x is None else f"{x:.2f}"


def _param_widget(spec):
    """Render an editable control for a declared strategy Parameter; return its value."""
    label = spec.description or spec.name
    key = f"p_{spec.name}"
    if spec.choices:
        default = spec.default if spec.default in spec.choices else spec.choices[0]
        return st.selectbox(label, list(spec.choices), index=list(spec.choices).index(default), key=key)
    if isinstance(spec.default, bool):
        return st.checkbox(label, value=spec.default, key=key)
    cast = int if isinstance(spec.default, int) else float
    kw = {}
    if spec.bounds:
        kw["min_value"], kw["max_value"] = cast(spec.bounds[0]), cast(spec.bounds[1])
    if isinstance(spec.default, int):
        return int(st.number_input(label, value=spec.default, step=1, key=key, **kw))
    if isinstance(spec.default, float):
        return float(st.number_input(label, value=float(spec.default), key=key, **kw))
    return st.text_input(label, value=str(spec.default), key=key)


def _equity_figure(result):
    ts = [t for t, _ in result.equity_curve]
    eq = [e for _, e in result.equity_curve]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ts, y=eq, name="Equity", line=dict(color="#2563eb")))
    fig.update_layout(
        height=420,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis=dict(title="Equity"),
        showlegend=False,
    )
    return fig


def _equity_figure_split(is_result, oos_result):
    """Full equity curve with IS (blue) and OOS (orange) segments and a split marker."""
    fig = go.Figure()
    if is_result.equity_curve:
        is_ts = [t for t, _ in is_result.equity_curve]
        is_eq = [e for _, e in is_result.equity_curve]
        fig.add_trace(go.Scatter(x=is_ts, y=is_eq, name="In-Sample", line=dict(color="#2563eb")))
    if oos_result.equity_curve:
        oos_ts = [t for t, _ in oos_result.equity_curve]
        oos_eq = [e for _, e in oos_result.equity_curve]
        fig.add_trace(go.Scatter(x=oos_ts, y=oos_eq, name="Out-of-Sample", line=dict(color="#f59e0b")))
        fig.add_vline(
            x=oos_ts[0],
            line=dict(color="#6b7280", dash="dash", width=1),
            annotation_text="IS / OOS",
            annotation_position="top left",
        )
    fig.update_layout(
        height=420,
        margin=dict(l=0, r=0, t=30, b=0),
        yaxis=dict(title="Equity"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def _show_metrics(result):
    m = result.metrics
    r1 = st.columns(4)
    r1[0].metric("Total return", _fmt_pct(m.total_return))
    r1[1].metric("CAGR", _fmt_pct(m.cagr))
    r1[2].metric("Sharpe", _fmt_num(m.sharpe))
    r1[3].metric("Sortino", _fmt_num(m.sortino))
    r2 = st.columns(4)
    r2[0].metric("Max drawdown", _fmt_pct(m.max_drawdown))
    r2[1].metric("Win rate", _fmt_pct(m.win_rate))
    r2[2].metric("Profit factor", _fmt_num(m.profit_factor))
    r2[3].metric("Trades", m.num_trades)
    r3 = st.columns(4)
    r3[0].metric("Adj. Sharpe", _fmt_num(m.parameter_adjusted_sharpe), help="Sharpe penalised for free parameters: Sharpe × √((n−k)/n)")
    r3[1].metric("Calmar", _fmt_num(m.calmar), help="CAGR / |max drawdown|")
    r3[2].metric("Omega ratio", _fmt_num(m.omega_ratio), help="Sum of gains / sum of losses")
    r3[3].metric("VaR 95%", _fmt_pct(m.var_95), help="5th-percentile single-bar return")
    r4 = st.columns(4)
    r4[0].metric("CVaR 95%", _fmt_pct(m.cvar_95), help="Mean return of the worst 5% of bars")
    r4[1].metric("Exposure", _fmt_pct(m.exposure_pct), help="Fraction of bars with an open position")
    r4[2].metric("Turnover (ann.)", _fmt_num(m.turnover), help="Total traded notional / avg equity, annualised")
    r4[3].metric("Avg drawdown", _fmt_pct(m.avg_drawdown), help="Mean depth across all drawdown episodes")


def _show_trades(result):
    trades = result.to_dict()["trades"]
    if trades:
        st.dataframe(trades, use_container_width=True, hide_index=True)
    else:
        st.caption("No trades in this run.")


def _stat_validation_dict(result) -> dict | None:
    """Return stat_validation as a plain dict regardless of result type."""
    sv = getattr(result, "stat_validation", None)
    if sv is None:
        return getattr(result, "_dict", {}).get("stat_validation")
    if isinstance(sv, dict):
        return sv
    return sv.to_dict()


def _show_stat_validation(result) -> None:
    sv = _stat_validation_dict(result)
    with st.expander("Statistical validation", expanded=True):
        if sv is None:
            st.caption("No statistical validation — run the backtest with `validate=True` to enable.")
            return

        # Sample quality
        sq = sv.get("sample_quality", {})
        warning = sq.get("warning")
        if warning:
            st.warning(warning)
        else:
            st.success(
                f"Sample quality OK — {sq.get('num_trades')} trades, "
                f"{sq.get('num_params')} params"
                + (f", {sq.get('trades_per_param'):.1f} trades/param" if sq.get("trades_per_param") else "")
                + "."
            )

        # PSR / DSR / MinTRL row
        c1, c2, c3 = st.columns(3)
        c1.metric(
            "Probabilistic Sharpe",
            _fmt_num(sv.get("probabilistic_sharpe")),
            help="P(SR > 0) corrected for skewness and kurtosis (Lopez de Prado 2012). >0.95 = strong evidence of edge.",
        )
        c2.metric(
            "Deflated Sharpe",
            _fmt_num(sv.get("deflated_sharpe")),
            help="P(true SR > SR*) after correcting for multiple strategy trials (Bailey & Lopez de Prado 2014).",
        )
        min_trl = sv.get("min_trl")
        c3.metric(
            "Min track record (bars)",
            str(min_trl) if min_trl is not None else "—",
            help="Minimum number of bars needed for the measured Sharpe to be statistically significant.",
        )

        # Bootstrap CIs
        ci = sv.get("ci", {})
        ci_rows = []
        for key, label, is_pct in [
            ("sharpe", "Sharpe", False),
            ("sortino", "Sortino", False),
            ("cagr", "CAGR", True),
            ("max_drawdown", "Max drawdown", True),
            ("win_rate", "Win rate", True),
            ("profit_factor", "Profit factor", False),
        ]:
            v = ci.get(key)
            if v:
                fmt = _fmt_pct if is_pct else _fmt_num
                ci_rows.append({
                    "Metric": label,
                    f"Lower ({int(v['level']*100)}% CI)": fmt(v["lower"]),
                    "Upper": fmt(v["upper"]),
                })
        if ci_rows:
            st.markdown("**Bootstrap confidence intervals**")
            import pandas as pd
            st.dataframe(pd.DataFrame(ci_rows), use_container_width=True, hide_index=True)

        # Monte Carlo
        mc = sv.get("monte_carlo")
        if mc:
            st.markdown(f"**Monte Carlo trade-reorder** ({mc['n_simulations']} simulations)")
            mc1 = st.columns(4)
            mc1[0].metric("End equity p5", f"${mc['end_equity_p5']:,.0f}")
            mc1[1].metric("End equity p50", f"${mc['end_equity_p50']:,.0f}")
            mc1[2].metric("End equity p95", f"${mc['end_equity_p95']:,.0f}")
            mc1[3].metric("Prob. profitable", _fmt_pct(mc["prob_profit"]))
            mc2 = st.columns(2)
            mc2[0].metric("Max DD median", _fmt_pct(mc["max_drawdown_median"]))
            mc2[1].metric("Max DD worst", _fmt_pct(mc["max_drawdown_worst"]))


def _show_result_panels(result):
    """Full result view: metrics, equity curve, trades, stat validation."""
    _show_metrics(result)
    st.subheader("Equity curve")
    if result.equity_curve:
        st.plotly_chart(_equity_figure(result), use_container_width=True)
    else:
        st.warning("No equity curve — the backtest produced no trading bars.")
    st.subheader("Trades")
    _show_trades(result)
    _show_stat_validation(result)


def _show_split_or_full(result):
    """Show IS/OOS split view (with controls) or fall back to full view."""
    sc1, sc2 = st.columns([1, 2])
    show_split = sc1.checkbox("IS / OOS split", value=True, key="show_split")
    is_frac = sc2.slider(
        "In-sample fraction", 0.1, 0.95, 0.7, 0.05,
        key="is_frac",
        disabled=not show_split,
        help="Fraction of bars used as in-sample (e.g. 0.7 = 70% IS, 30% OOS).",
    )

    if show_split and result.equity_curve and len(result.equity_curve) >= 2:
        is_result, oos_result = _split_result(result, is_frac)
        tab_is, tab_oos, tab_combined = st.tabs(["In-Sample (IS)", "Out-of-Sample (OOS)", "Combined"])
        with tab_is:
            _show_metrics(is_result)
            st.subheader("Equity curve")
            if is_result.equity_curve:
                st.plotly_chart(_equity_figure(is_result), use_container_width=True)
            else:
                st.warning("No equity curve for the IS period.")
            st.subheader("Trades")
            _show_trades(is_result)
        with tab_oos:
            _show_metrics(oos_result)
            st.subheader("Equity curve")
            if oos_result.equity_curve:
                st.plotly_chart(_equity_figure(oos_result), use_container_width=True)
            else:
                st.warning("No equity curve for the OOS period.")
            st.subheader("Trades")
            _show_trades(oos_result)
        with tab_combined:
            _show_metrics(result)
            st.subheader("Equity curve")
            st.plotly_chart(_equity_figure_split(is_result, oos_result), use_container_width=True)
            _show_stat_validation(result)
    else:
        _show_result_panels(result)


def _split_result(result, is_frac: float):
    """Derive IS and OOS BacktestResults from a full result by splitting the equity curve."""
    ec = result.equity_curve
    if not ec or len(ec) < 2:
        return result, result
    split_idx = max(1, min(int(len(ec) * is_frac), len(ec) - 1))
    split_time = ec[split_idx][0]
    is_curve = ec[:split_idx + 1]   # inclusive of split point
    oos_curve = ec[split_idx:]      # starts at split point (continuous connection)
    trades = getattr(result, "trades", [])
    is_trades = [t for t in trades if t.exit_time and t.exit_time <= split_time]
    oos_trades = [t for t in trades if t.exit_time and t.exit_time > split_time]
    return BacktestResult.compute(is_curve, is_trades), BacktestResult.compute(oos_curve, oos_trades)


# --- pick a strategy -------------------------------------------------------

entries = discovery.discover()
if not entries:
    st.info(
        "No strategies found in `strategies/`. Create one with the **`/hermes-strategy`** "
        "skill in Claude Code, then reload."
    )
    st.stop()

names = [e.name for e in entries]
choice = st.selectbox("Strategy", names)
entry = next(e for e in entries if e.name == choice)
if entry.is_ai_generated:
    st.caption("🤖 AI-generated by hermes-strategy.")

# --- config form (pre-filled from the strategy's defaults) -----------------

defaults = discovery.default_config(entry)
param_specs = discovery.declared_parameters(entry)
src_names = sources.source_names()
with st.form("run"):
    c1, c2, c3 = st.columns(3)
    source_name = c1.selectbox(
        "Source",
        src_names,
        index=src_names.index(defaults.source.name) if defaults.source.name in src_names else 0,
        help="Data provider for the symbol. cTrader/Pepperstone needs CTRADER_* env credentials.",
    )
    universe = c1.selectbox(
        "Symbols",
        [_SINGLE, *universes.universe_names()],
        help="Run one symbol (typed below) or a whole ticker list from tickers/*.json.",
    )
    ticker = c1.text_input("Symbol", value=defaults.symbol.ticker, help=f"Used when Symbols = {_SINGLE}.")
    cash = c1.number_input(
        "Starting cash", value=float(defaults.starting_cash), step=1000.0,
        help="For a universe, this is the TOTAL — split equally across the symbols.",
    )
    start = c2.date_input("Start", value=defaults.start.date())
    end = c3.date_input("End", value=defaults.end.date())
    st.caption(f"Timeframes: {', '.join(str(tf) for tf in defaults.timeframes)}")
    if source_name in sources.NEEDS_SETUP:
        st.caption("⚠️ cTrader/Pepperstone needs `CTRADER_*` credentials and its live fetch wired.")

    param_values: dict = {}
    if param_specs:
        st.markdown("**Parameters**")
        pcols = st.columns(min(len(param_specs), 3))
        for i, spec in enumerate(param_specs):
            with pcols[i % len(pcols)]:
                param_values[spec.name] = _param_widget(spec)

    run = st.form_submit_button("Run backtest", type="primary")

if run:
    start_dt = datetime.combine(start, time(), tzinfo=UTC)
    end_dt = datetime.combine(end, time(), tzinfo=UTC)
    if universe == _SINGLE:
        bt = discovery.configured_backtest(
            entry, source_name=source_name, ticker=ticker,
            start=start_dt, end=end_dt, starting_cash=cash, params=param_values,
        )
        try:
            with st.spinner("Running backtest… (first run may fetch data)"):
                result = bt.run()
        except Exception as exc:
            st.error(f"Backtest failed on source `{source_name}` / `{ticker}`: {exc}")
            st.stop()
        rid = review.run_id(result.to_dict())
        review.write_result(result.to_dict(), rid)
        review.save_last_rid(rid)
        st.session_state.update(result=result, rid=rid, ai=entry.is_ai_generated, mode="single")
        st.session_state.pop("batch", None)
    else:
        src_override, tickers = universes.load_universe(universe)
        bar = st.progress(0.0, f"Running {universe} ({len(tickers)} symbols)…")
        batch = discovery.run_universe(
            entry, tickers=tickers, source_name=src_override or source_name,
            start=start_dt, end=end_dt, starting_cash=cash, params=param_values,
            progress=lambda done, total: bar.progress(done / max(total, 1), f"Backtesting {done}/{total}…"),
        )
        bar.empty()
        st.session_state.update(batch=batch, mode="batch")
        st.session_state.pop("result", None)

# --- results ---------------------------------------------------------------

mode = st.session_state.get("mode")
batch = st.session_state.get("batch")
result = None
rid = None

if mode == "batch" and batch is not None:
    agg = batch.aggregate()
    st.subheader("Universe scan — by symbol")
    a = st.columns(5)
    a[0].metric("Symbols", agg["symbols"])
    a[1].metric("Mean return / symbol", _fmt_pct(agg["mean_return"]))
    a[2].metric("Median Sharpe", _fmt_num(agg["median_sharpe"]))
    a[3].metric("% profitable", _fmt_pct(agg["pct_profitable"]))
    a[4].metric("Total trades", agg["total_trades"])
    st.dataframe(batch.summary_rows(), use_container_width=True, hide_index=True)
    if batch.errors:
        with st.expander(f"⚠️ {len(batch.errors)} symbol(s) failed"):
            for t, e in batch.errors.items():
                st.write(f"**{t}** — {e}")
    if not batch.results:
        st.stop()

    st.divider()
    view = st.selectbox("Equity curve, metrics & review for", ["Combined portfolio", *batch.results])
    if view == "Combined portfolio":
        result = batch.combined_result()
        st.caption(
            "One shared, compounding capital pool — each trade risks a % of the *current* "
            "equity, so a win on one symbol grows the capital for the next trade on any symbol."
        )
    else:
        result = batch.results[view]
    rid = review.run_id(result.to_dict())
    review.write_result(result.to_dict(), rid)
    st.session_state["rid"] = rid
    _show_split_or_full(result)

else:
    result = st.session_state.get("result")
    rid = st.session_state.get("rid")
    if result is None:
        rid = rid or review.load_last_rid()
        if rid:
            result = review.load_result(rid)
        if result is None:
            st.stop()
        st.info("Showing results from the last run — re-run the backtest above to refresh.")

    _show_split_or_full(result)

# --- Claude review ---------------------------------------------------------

if result is None:
    st.stop()

st.subheader("Claude review")
st.caption("On-demand only — runs Claude Code and spends tokens when you click.")
state = review.status(rid)
cols = st.columns([1, 1, 4])
if cols[0].button("Run Claude review", type="primary", disabled=not review.claude_available()):
    review.launch(rid)
    state = review.status(rid)
cols[1].button("Refresh")  # any interaction reruns the page and re-reads the file
if not review.claude_available():
    cols[2].caption("`claude` not on PATH — use the manual fallback below.")

if state.state == "done":
    st.markdown(review.read_review(rid))
elif state.state == "running":
    st.info("Review is running in Claude Code… click **Refresh** in a bit.")
elif state.state == "failed":
    st.error("Review failed. Log tail:")
    st.code(state.detail or "(no log)")
else:
    st.caption("No review yet.")

with st.expander("Run the review manually"):
    st.markdown(review.manual_instructions(rid))
