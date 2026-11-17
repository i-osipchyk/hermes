"""Phase 2 step 3 (ADR-0013): the Sharpe is deflated by the effective number of trials and
by the spread of the Sharpes those trials produced. Seams: ``validate()`` and ``analyze()``.

Expected values come from ``statistics.NormalDist`` and the Bailey & López de Prado
formula's structure, not from the implementation's own helpers."""

import math
import random
import statistics
from datetime import UTC, datetime, timedelta
from statistics import NormalDist, fmean, pstdev

import pytest

from hermes.backtest import validate
from hermes.cli import EXIT_OK, main
from hermes.research.analysis import ALL_AXES, DEFAULT_AXES, DeflationInputs, analyze
from hermes.research.ideas import IdeaBook
from hermes.research.ledger import RunLedger, RunMeta
from hermes.research.trials import EffectiveTrials, TrialRegistry
from tests.test_cli import STRATEGY
from tests.test_validation import _make_result

_N = NormalDist()
_GAMMA = 0.5772156649015328


def _z_factor(n_eff: int) -> float:
    """The bracketed term of SR0 = sqrt(V) * [(1-γ)Φ⁻¹(1-1/N) + γΦ⁻¹(1-1/(N·e))]."""
    return (1 - _GAMMA) * _N.inv_cdf(1 - 1 / n_eff) + _GAMMA * _N.inv_cdf(1 - 1 / (n_eff * math.e))


def _result():
    rng = random.Random(7)
    return _make_result([rng.gauss(6.0, 40.0) for _ in range(80)])


def _validate(**kw):
    return validate(_result(), n_bootstrap=10, n_mc=10, seed=0, **kw)


def _z(p):
    return _N.inv_cdf(p)


def test_with_one_effective_trial_there_is_nothing_to_deflate():
    sv = _validate(n_trials=1, trial_sharpes=[0.2])
    assert sv.deflated_sharpe == sv.probabilistic_sharpe


def test_more_trials_deflate_further():
    spread = [0.05, 0.12, 0.2, 0.0, 0.1]
    few = _validate(n_trials=3, trial_sharpes=spread).deflated_sharpe
    many = _validate(n_trials=30, trial_sharpes=spread).deflated_sharpe
    psr = _validate().probabilistic_sharpe
    assert many < few < psr


def test_the_threshold_scales_with_the_spread_of_trial_sharpes():
    base = [0.00, 0.05, 0.10, 0.15, 0.20]
    wide = [2 * s for s in base]                 # twice the dispersion -> twice SR0
    psr = _validate().probabilistic_sharpe
    z0 = _z(psr)
    z1 = _z(_validate(n_trials=5, trial_sharpes=base).deflated_sharpe)
    z2 = _z(_validate(n_trials=5, trial_sharpes=wide).deflated_sharpe)
    assert (z0 - z2) / (z0 - z1) == pytest_approx(pstdev(wide) / pstdev(base))


def test_the_threshold_follows_the_bailey_lopez_de_prado_trial_factor():
    spread = [0.0, 0.05, 0.10, 0.15, 0.20]
    z0 = _z(_validate().probabilistic_sharpe)
    z5 = _z(_validate(n_trials=5, trial_sharpes=spread).deflated_sharpe)
    z20 = _z(_validate(n_trials=20, trial_sharpes=spread).deflated_sharpe)
    assert (z0 - z20) / (z0 - z5) == pytest_approx(_z_factor(20) / _z_factor(5))


def pytest_approx(x):
    return pytest.approx(x, rel=1e-6)


# --- representative Sharpes from the ledger -----------------------------------



_T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _save_trades(ledger, key, trade_returns, *, idea, minute, first_day=0, side="buy"):
    """A stored run on a flat 10,000 account whose trade i returns trade_returns[i]."""
    days = len(trade_returns) + first_day + 30
    equity = [((_T0 + timedelta(days=d, hours=20)).isoformat(), 10_000.0) for d in range(days)]
    trades = [
        {"side": side, "net_pnl": r * 10_000.0,
         "entry_time": (_T0 + timedelta(days=first_day + i, hours=9)).isoformat()}
        for i, r in enumerate(trade_returns)
    ]
    meta = RunMeta(
        strategy="s", strategy_hash="h", source="memory", ticker="T", universe=None,
        start=_T0.isoformat(), end=(_T0 + timedelta(days=days)).isoformat(), params={},
        sizer="", unconstrained=False,
        created_at=(_T0 + timedelta(minutes=minute)).isoformat(), idea=idea,
    )
    ledger.save(key, {"equity_curve": equity, "trades": trades, "metrics": {}}, meta)


def _gauss(seed, mu, n=40):
    rng = random.Random(seed)
    return [rng.gauss(mu, 0.02) for _ in range(n)]


def test_representative_sharpes_are_per_trade_and_cover_inherited_clusters(tmp_path):
    ledger, book = RunLedger(tmp_path / "runs"), IdeaBook(tmp_path / "ideas.json")
    mine, used = book.add("mine"), book.add("used")
    a, b, c = _gauss(1, 0.004), _gauss(2, 0.001), _gauss(3, 0.006)
    # two clusters of mine (distinct entry days AND sides so neither rule merges them)
    _save_trades(ledger, "a", a, idea=mine.id, minute=1, first_day=0, side="buy")
    _save_trades(ledger, "b", b, idea=mine.id, minute=2, first_day=60, side="sell")
    _save_trades(ledger, "c", c, idea=used.id, minute=3, first_day=120, side="buy")
    book.update(mine.id, informed_by=[used.id])

    sharpes = TrialRegistry(ledger, book).representative_sharpes(mine.id)

    expected = [fmean(r) / pstdev(r) for r in (a, b, c)]      # own clusters, then inherited
    assert sharpes == pytest.approx(expected)


def test_a_cluster_with_too_few_trades_to_measure_is_left_out(tmp_path):
    ledger, book = RunLedger(tmp_path / "runs"), IdeaBook(tmp_path / "ideas.json")
    mine = book.add("mine")
    _save_trades(ledger, "few", _gauss(4, 0.01, n=4), idea=mine.id, minute=1)
    _save_trades(ledger, "enough", _gauss(5, 0.01), idea=mine.id, minute=2, first_day=90,
                 side="sell")
    assert len(TrialRegistry(ledger, book).representative_sharpes(mine.id)) == 1


# --- the `deflation` axis -----------------------------------------------------



def _axis(result=None, deflation=None):
    out = analyze(result=result or _result(), axes=("deflation",), deflation=deflation, seed=0)
    (axis,) = out.axes
    return axis


def test_deflation_is_a_rubric_axis_but_not_a_default_one():
    assert "deflation" in ALL_AXES and "deflation" not in DEFAULT_AXES


def test_without_an_idea_the_axis_says_how_to_measure_it_instead_of_passing():
    axis = _axis(deflation=None)
    assert axis.ran is False
    assert "--idea" in axis.error and "hermes analyze" in axis.error


def test_a_single_effective_trial_has_nothing_to_deflate():
    axis = _axis(deflation=DeflationInputs(EffectiveTrials(own=1, inherited={}), [0.2]))
    assert axis.ran is True
    assert axis.data["n_eff"] == 1
    assert axis.data["dsr"] == axis.data["psr"]
    assert "nothing to deflate" in axis.note


def test_many_trials_deflate_and_the_axis_shows_where_the_count_came_from():
    eff = EffectiveTrials(own=4, inherited={"abc": 3}, batch_floor=0)
    spread = [0.05, 0.12, 0.2, 0.0, 0.1, 0.15, 0.08]
    axis = _axis(deflation=DeflationInputs(eff, spread))
    assert axis.ran is True
    d = axis.data
    assert (d["n_eff"], d["own"], d["inherited"]) == (7, 4, {"abc": 3})
    assert d["dsr"] < d["psr"]
    assert d["trial_sharpe_std"] == pytest.approx(statistics.stdev(spread))
    assert d["basis"] == "trades"


def test_a_run_with_too_few_trades_is_not_deflated_on_bar_returns():
    few = _make_result([5.0, -3.0, 4.0, 2.0, -1.0])
    axis = _axis(few, DeflationInputs(EffectiveTrials(own=3, inherited={}), [0.1, 0.2]))
    assert axis.ran is False and "10 trades" in axis.error


# --- `hermes analyze --axes deflation` ----------------------------------------



def _cli_project(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    # a strategy that trades often enough (>= 10 trades) for trade-level inference
    (sdir / "buyhold.py").write_text(STRATEGY.replace("bar.close * 1.01", "bar.close * 1.0001"))
    return ["--dir", str(sdir), "--runs-dir", str(tmp_path / "runs")], ["--ideas-file",
                                                                       str(tmp_path / "ideas.json")]


def _json(capsys):
    import json
    return json.loads(capsys.readouterr().out)


def test_analyze_deflates_by_the_trials_behind_the_run(tmp_path, capsys):
    common, ideas = _cli_project(tmp_path)
    main(["ideas", "add", "mine", *ideas, "--json"])
    mine = _json(capsys)["idea"]["id"]
    main(["ideas", "add", "used", *ideas, "--json"])
    used = _json(capsys)["idea"]["id"]
    main(["ideas", "set", mine, *ideas, "--informed-by", used, "--json"])
    capsys.readouterr()
    main(["run", "buyhold", *common, "--idea", used, "--cash", "5000", "--json", "--note", "t"])
    capsys.readouterr()
    main(["run", "buyhold", *common, "--idea", mine, "--json", "--note", "t"])
    key = _json(capsys)["run_key"]

    code = main(["analyze", key, *common, *ideas, "--axes", "deflation", "--json"])

    assert code == EXIT_OK
    (axis,) = _json(capsys)["axes"]
    assert axis["axis"] == "deflation" and axis["ran"] is True, axis
    assert axis["data"]["n_eff"] == 2 and axis["data"]["inherited"] == {used: 1}


def test_analyze_a_run_with_no_idea_reports_deflation_as_not_measured(tmp_path, capsys):
    common, ideas = _cli_project(tmp_path)
    main(["run", "buyhold", *common, "--json", "--note", "t"])
    key = _json(capsys)["run_key"]
    main(["analyze", key, *common, *ideas, "--axes", "deflation", "--json"])
    (axis,) = _json(capsys)["axes"]
    assert axis["ran"] is False and "--idea" in axis["error"]
