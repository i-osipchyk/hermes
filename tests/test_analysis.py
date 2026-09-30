"""The rubric-as-computation module (ADR-0010).

These tests care about two things the reviewer downstream depends on: that an axis
*reaches a correct verdict* on a run whose character is known, and that a broken or
under-specified axis degrades to `ran: false` with a reason instead of taking the
analysis down.
"""

from __future__ import annotations

from hermes.research.analysis import (
    ALL_AXES,
    DEFAULT_AXES,
    Analysis,
    AxisResult,
    analyze,
    axis_gate,
    axis_trades,
)
from tests.test_research_primitives import _backtest


class _FakeResult:
    """A result-shaped stand-in — the read-only axes only ever call to_dict()."""

    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


def _run_with(trades, **extra):
    return _FakeResult({"metrics": {"num_trades": len(trades)}, "trades": trades,
                        "vetoed_signals": [], **extra})


def _t(pnl, side="buy", year="2021", reason="take_profit"):
    return {"net_pnl": pnl, "side": side, "exit_reason": reason,
            "entry_time": f"{year}-06-01T00:00:00+00:00"}


# --- axis: trade concentration ---------------------------------------------

def test_trades_axis_flags_a_single_dominant_trade():
    r = _run_with([_t(1000), *[_t(10) for _ in range(9)]])
    a = axis_trades(r)
    assert a.ran
    assert "single best trade" in a.note
    assert a.data["top_trade_share_of_wins"] > 0.9


def test_trades_axis_is_quiet_when_pnl_is_spread():
    r = _run_with([_t(100, year=str(2015 + i % 8)) for i in range(40)]
                  + [_t(-90, side="sell", year=str(2015 + i % 8)) for i in range(40)])
    a = axis_trades(r)
    assert a.ran
    assert "not concentrated" in a.note


def test_trades_axis_flags_a_one_directional_strategy():
    a = axis_trades(_run_with([_t(10, side="buy") for _ in range(30)]))
    assert "untested in the other direction" in a.note
    assert a.data["sides"] == {"buy": 30}


def test_trades_axis_flags_clustering_in_one_year():
    trades = [_t(10, year="2020") for _ in range(9)] + [_t(10, year="2021")]
    assert "2020" in axis_trades(_run_with(trades)).note


def test_trades_axis_on_a_zero_trade_run_does_not_raise():
    a = axis_trades(_run_with([]))
    assert not a.ran and a.error


def test_trades_axis_survives_an_all_losing_run():
    """gross_win is 0, so the share-of-wins ratios must be None, not a ZeroDivisionError."""
    a = axis_trades(_run_with([_t(-10) for _ in range(5)]))
    assert a.ran
    assert a.data["top_trade_share_of_wins"] is None


# --- axis: the AI gate ------------------------------------------------------

def test_gate_axis_reports_no_advisor():
    a = axis_gate(_run_with([_t(1)]))
    assert not a.ran and "no AI advisor" in a.note


def test_gate_axis_shouts_about_fail_open():
    a = axis_gate(_run_with([_t(1)], llm_summary={
        "total_calls": 10, "fail_open_calls": 4, "approval_rate": 0.5}))
    assert a.ran
    assert "failed open" in a.note and "less gated" in a.note
    assert a.data["fail_open_calls"] == 4


def test_gate_axis_reports_a_clean_gate():
    a = axis_gate(_run_with([_t(1)], llm_summary={
        "total_calls": 10, "fail_open_calls": 0, "approval_rate": 0.8}))
    assert a.ran and "clean" in a.note


def test_gate_axis_notices_an_advisor_that_was_never_consulted():
    a = axis_gate(_run_with([_t(1)], llm_summary={"total_calls": 0, "fail_open_calls": 0}))
    assert a.ran and "never consulted" in a.note


# --- the driver -------------------------------------------------------------

def test_analyze_runs_the_read_only_axes_from_a_result_alone():
    bt = _backtest()
    result = bt.run()
    a = analyze(None, result, axes=("trades", "validation", "regime", "gate"))
    assert "trades" in a.ran and "validation" in a.ran
    # the re-run axes were not requested, so they are absent entirely
    assert [x.axis for x in a.axes] == ["trades", "validation", "regime", "gate"]


def test_analyze_skips_rerun_axes_without_a_backtest_and_says_why():
    result = _backtest().run()
    a = analyze(None, result, axes=("costs", "oos", "trades"))
    assert a["costs"].ran is False
    assert "needs a Backtest" in a["costs"].note
    assert a["oos"].ran is False
    assert a["trades"].ran is True, "the runnable axis still ran"


def test_analyze_reports_axes_in_rubric_order_not_request_order():
    a = analyze(None, _backtest().run(), axes=("gate", "trades", "validation"))
    assert [x.axis for x in a.axes] == ["trades", "validation", "gate"]


def test_one_failing_axis_does_not_lose_the_others(monkeypatch):
    import hermes.research.analysis as mod

    def boom(*a, **k):
        raise RuntimeError("synthetic axis failure")

    monkeypatch.setattr(mod, "axis_validation", boom)
    a = analyze(None, _backtest().run(), axes=("trades", "validation", "gate"))
    assert a["validation"].ran is False
    assert "synthetic axis failure" in a["validation"].error
    assert a["trades"].ran is True


def test_analyze_runs_the_backtest_when_only_given_one():
    a = analyze(_backtest(), axes=("trades", "costs"))
    assert a["trades"].ran and a["costs"].ran


def test_baseline_is_skipped_for_a_run_with_no_advisor():
    """The random baseline matches the gate's approval rate; without a gate there is
    nothing to match, and running it anyway would be meaningless, not merely slow."""
    a = analyze(None, _backtest().run(), axes=("baseline",),
                build_backtest=_backtest)
    assert a["baseline"].ran is False
    assert "approval rate" in a["baseline"].note


def test_default_axes_are_the_cheap_ones():
    assert "baseline" not in DEFAULT_AXES
    assert "walkforward" not in DEFAULT_AXES
    assert set(DEFAULT_AXES) < set(ALL_AXES)


# --- the contract the CLI and the reviewer rely on -------------------------

def test_notes_are_always_a_single_line():
    """`note` is printed in an aligned column and read back by the reviewer;
    upstream warnings contain newlines."""
    a = AxisResult("x", True, "first line\n  second line\t\tthird")
    assert a.note == "first line second line third"


def test_analysis_to_dict_is_json_serialisable():
    import json

    a = analyze(_backtest(), axes=DEFAULT_AXES, run_key="deadbeef", strategy="smoke")
    blob = json.loads(json.dumps(a.to_dict(), default=str))
    assert blob["run_key"] == "deadbeef"
    assert blob["strategy"] == "smoke"
    for axis in blob["axes"]:
        assert set(axis) == {"axis", "ran", "note", "data", "error"}


def test_analysis_lookup_and_summaries():
    a = Analysis(axes=[AxisResult("trades", True, "fine"), AxisResult("costs", False, "nope")])
    assert a["trades"].note == "fine"
    assert a.ran == ["trades"] and a.failed == ["costs"]
    assert a.notes == ["trades: fine", "costs: nope"]
