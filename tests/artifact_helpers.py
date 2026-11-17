"""Fixtures for tests that walk an idea through the pipeline: a valid spec, a reviewed plan."""

from __future__ import annotations

from hermes.research.artifacts import ArtifactStore, Plan, Spec


def good_spec(**kw) -> Spec:
    base = dict(
        claim="Gold continues in the direction of the London break",
        mechanism="stop orders cluster beyond the session range",
        instruments=["XAUUSD"], timeframes=["1h"],
        entry="buy a close above the London high",
        exit="exit after 6 bars or at the stop",
        sizing="risk 1% of equity per trade",
        invented_numbers=[{"name": "hold", "value": 6, "why": "one session"},
                          {"name": "risk", "value": 1, "why": "house risk"}],
        parameters=[{"name": "hold", "value": 6}],
        lookahead="uses only closed bars; the range is final before the entry bar",
        expected_sign="long", expected_size="Sharpe 0.5",
        falsifier="no excess return after costs in 2012-2019",
    )
    base.update(kw)
    return Spec(**base)


def good_plan(**kw) -> Plan:
    base = dict(
        variants=[
            {"id": "v1", "axis": "entry", "change": "break of the Tokyo range",
             "tests": "same mechanism, different range", "decision_rule": "keep if net Sharpe > 0"},
            {"id": "v2", "axis": "regime_filter", "change": "only when ATR is above median",
             "tests": "vol dependence", "decision_rule": "keep if Sharpe improves in-sample"},
        ],
        budget={"max_clusters": 4, "max_extensions": 3},
    )
    base.update(kw)
    return Plan(**base)


def ready_for_testing(ideas_dir, idea_id: str) -> None:
    """Write the artifacts that `quantified` and `testing` require."""
    store = ArtifactStore(ideas_dir)
    store.write(idea_id, "spec", good_spec(), role="researcher")
    store.write(idea_id, "plan", good_plan(), role="quant")
    store.approve_plan(idea_id, reviewer="test", role="validator")
