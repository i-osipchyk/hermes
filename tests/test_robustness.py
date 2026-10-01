"""Robustness primitives the five-step framework names explicitly (ADR-0011).

`param_sensitivity` answers "is the reported result a plateau or a spike?" and
`correlate_*` answers "do these strategies do different things?" — the two checks
Hermes had no support for.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from hermes import (
    REDUNDANT_ABOVE,
    CorrelationMatrix,
    ParamSensitivity,
    correlate_curves,
    param_sensitivity,
)
from hermes.backtest.param_sensitivity import PLATEAU_RATIO, _sweep_values
from hermes.strategy import Parameter
from tests.test_research_primitives import _backtest

T0 = datetime(2021, 1, 1, tzinfo=UTC)


# --- sweep construction -----------------------------------------------------

def test_sweep_keeps_integers_integral():
    """A lookback of 12.5 bars is not a thing."""
    vals = _sweep_values(Parameter("fast", 10, bounds=(2, 50)), 10, steps=4, relative=0.25)
    assert all(isinstance(v, int) for v in vals)
    assert 10 in vals


def test_sweep_respects_bounds():
    vals = _sweep_values(Parameter("p", 3, bounds=(2, 4)), 3, steps=6, relative=1.0)
    assert min(vals) >= 2 and max(vals) <= 4


def test_sweep_uses_discrete_choices_verbatim():
    vals = _sweep_values(Parameter("mode", "a", choices=("a", "b", "c")), "a", 4, 0.25)
    assert vals == ["a", "b", "c"]


def test_sweep_always_includes_the_base_value():
    """Without the base point there is nothing to compare neighbours against."""
    vals = _sweep_values(Parameter("p", 7, bounds=(7, 7)), 7, steps=4, relative=0.5)
    assert 7 in vals


def test_sweep_leaves_non_numeric_alone():
    assert _sweep_values(Parameter("flag", True), True, 4, 0.25) == [True]


# --- the sensitivity run ----------------------------------------------------

def test_param_sensitivity_sweeps_every_declared_parameter():
    ps = param_sensitivity(_backtest(), metric="sharpe", steps=2)
    assert isinstance(ps, ParamSensitivity)
    assert sorted(c.name for c in ps.curves) == ["fast", "slow"]
    for c in ps.curves:
        assert any(p.is_base for p in c.points), "the configured value must be swept"
        assert c.base_value in [p.value for p in c.points]


def test_param_sensitivity_can_target_one_parameter():
    ps = param_sensitivity(_backtest(), params=["fast"], steps=2)
    assert [c.name for c in ps.curves] == ["fast"]


def test_degradation_detects_a_plateau():
    """Neighbours performing like the base point is the good case."""
    from hermes.backtest.param_sensitivity import ParamCurve, ParamPoint

    c = ParamCurve("p", 10, 1.0, points=[
        ParamPoint(9, 0.95, 50), ParamPoint(10, 1.0, 50, is_base=True), ParamPoint(11, 0.98, 50),
    ])
    assert c.neighbour_mean == pytest.approx(0.965)
    assert c.degradation == pytest.approx(0.965)
    assert c.is_plateau is True


def test_degradation_detects_a_spike():
    """The failure this axis exists to catch: the reported number is unreachable one
    notch away, so it was found rather than earned."""
    from hermes.backtest.param_sensitivity import ParamCurve, ParamPoint

    c = ParamCurve("p", 10, 1.4, points=[
        ParamPoint(9, 0.2, 50), ParamPoint(10, 1.4, 50, is_base=True), ParamPoint(11, 0.1, 50),
    ])
    assert c.degradation == pytest.approx(0.15 / 1.4, rel=1e-3)
    assert c.is_plateau is False
    assert c.degradation < PLATEAU_RATIO


def test_degradation_is_none_when_the_base_metric_is_unusable():
    from hermes.backtest.param_sensitivity import ParamCurve, ParamPoint

    c = ParamCurve("p", 10, None, points=[ParamPoint(10, None, 0, is_base=True)])
    assert c.degradation is None and c.is_plateau is None


def test_sensitivity_to_dict_is_json_serialisable():
    import json

    blob = json.loads(json.dumps(param_sensitivity(_backtest(), steps=2).to_dict(),
                                 default=str))
    assert blob["metric"] == "sharpe"
    assert "fragile" in blob and isinstance(blob["curves"], list)


# --- correlation ------------------------------------------------------------

def _curve(returns, start=T0):
    eq, out = 10_000.0, []
    for i, r in enumerate(returns):
        eq *= 1 + r
        out.append((start + timedelta(days=i), eq))
    return out


def test_identical_streams_correlate_at_one():
    import random

    random.seed(3)
    rets = [random.gauss(0, 0.01) for _ in range(120)]
    m = correlate_curves({"a": _curve(rets), "b": _curve(rets)})
    assert m.pair("a", "b") == pytest.approx(1.0, abs=1e-6)
    assert ("a", "b", m.pair("a", "b")) in [(x, y, z) for x, y, z in m.redundant]


def test_inverse_streams_correlate_at_minus_one():
    import random

    random.seed(4)
    rets = [random.gauss(0, 0.01) for _ in range(120)]
    m = correlate_curves({"a": _curve(rets), "b": _curve([-r for r in rets])})
    assert m.pair("a", "b") == pytest.approx(-1.0, abs=1e-6)


def test_independent_streams_are_flagged_as_diversifying():
    import random

    random.seed(5)
    a = _curve([random.gauss(0, 0.01) for _ in range(300)])
    b = _curve([random.gauss(0, 0.01) for _ in range(300)])
    m = correlate_curves({"a": a, "b": b})
    assert abs(m.pair("a", "b")) < 0.3
    assert [p[:2] for p in m.diversifying] == [("a", "b")]
    assert m.redundant == []


def test_correlation_is_symmetric_with_a_unit_diagonal():
    import random

    random.seed(6)
    curves = {k: _curve([random.gauss(0, 0.01) for _ in range(150)]) for k in "abc"}
    m = correlate_curves(curves)
    for i in range(3):
        assert m.matrix[i][i] == 1.0
        for j in range(3):
            assert m.matrix[i][j] == pytest.approx(m.matrix[j][i])


def test_too_little_overlap_is_unmeasurable_not_zero():
    """Two strategies that barely traded together have no measurable correlation;
    reporting 0.0 would read as 'independent', which is a different claim."""
    import random

    random.seed(7)
    a = _curve([random.gauss(0, 0.01) for _ in range(100)], start=T0)
    b = _curve([random.gauss(0, 0.01) for _ in range(100)],
               start=T0 + timedelta(days=95))     # ~5 days of overlap
    m = correlate_curves({"a": a, "b": b})
    assert m.pair("a", "b") is None
    assert m.unmeasurable and m.unmeasurable[0][:2] == ("a", "b")


def test_empty_and_single_stream_inputs_do_not_raise():
    assert correlate_curves({}).labels == []
    m = correlate_curves({"only": _curve([0.01] * 50)})
    assert m.diversification_ratio is None     # nothing to diversify against


def test_a_flat_stream_has_no_correlation():
    """Zero variance means undefined correlation, not zero correlation."""
    import random

    random.seed(8)
    live = _curve([random.gauss(0, 0.01) for _ in range(100)])
    flat = [(T0 + timedelta(days=i), 10_000.0) for i in range(100)]
    assert correlate_curves({"live": live, "flat": flat}).pair("live", "flat") is None


def test_diversification_ratio_falls_as_streams_decorrelate():
    """The Holy Grail measurement: N uncorrelated streams beat N copies of one bet."""
    import random

    random.seed(9)
    drv = [random.gauss(0, 0.01) for _ in range(400)]
    same = {f"s{i}": _curve([d + random.gauss(0, 0.0005) for d in drv]) for i in range(4)}
    indep = {f"i{i}": _curve([random.gauss(0, 0.01) for _ in range(400)]) for i in range(4)}

    r_same = correlate_curves(same).diversification_ratio
    r_indep = correlate_curves(indep).diversification_ratio
    assert r_same > 0.9, "four copies of one bet should not diversify"
    assert r_indep < r_same
    assert r_indep == pytest.approx(1 / math.sqrt(4), abs=0.12)


def test_correlation_to_dict_is_json_serialisable():
    import json
    import random

    random.seed(10)
    curves = {k: _curve([random.gauss(0, 0.01) for _ in range(150)]) for k in "ab"}
    blob = json.loads(json.dumps(correlate_curves(curves).to_dict(), default=str))
    assert blob["thresholds"]["redundant_above"] == REDUNDANT_ABOVE
    assert isinstance(blob["matrix"], list)


def test_unknown_stream_lookup_raises():
    m = CorrelationMatrix(labels=["a"], matrix=[[1.0]], overlap=[[10]])
    with pytest.raises(KeyError):
        m.pair("a", "nope")
