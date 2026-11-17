"""Phase 4 step 5 (acceptance 6): the Validator's mechanical half is computed, not asked of an LLM.

Seam: ``fill_validation`` over a real ledger, registry and ``analysis.json`` on disk."""

import json

import pytest

from hermes.research.analysis import ALL_AXES
from hermes.research.artifacts import ArtifactStore
from hermes.research.ideas import IdeaBook
from hermes.research.ledger import RunLedger
from hermes.research.reads import ReadsLog
from hermes.research.trials import TrialRegistry
from hermes.research.validation_fill import fill_validation
from tests.artifact_helpers import good_plan
from tests.pipeline_helpers import save_run


class Env:
    def __init__(self, tmp_path):
        self.book = IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")
        self.store = ArtifactStore(tmp_path / "ideas")
        self.ledger = RunLedger(tmp_path / "runs")
        self.reads = ReadsLog(tmp_path / "ideas")
        self.registry = TrialRegistry(self.ledger, self.book, self.reads)
        self.idea = self.book.add("x", source="discretionary").id
        self.store.write(self.idea, "plan", good_plan(), role="quant")

    def run(self, key, variant, seed):
        save_run(self.ledger, key, self.idea, variant, seed)

    def analysis(self, key, *, tier="credible", dsr=0.95, null_p=0.01, ran=("validation",
                                                                            "deflation", "null")):
        axes = []
        if "validation" in ran:
            axes.append({"axis": "validation", "ran": True, "note": "n",
                         "data": {"sample_quality": {"tier": tier}}, "error": None})
        if "deflation" in ran:
            axes.append({"axis": "deflation", "ran": True, "note": "n", "data": {"dsr": dsr},
                         "error": None})
        if "null" in ran:
            axes.append({"axis": "null", "ran": True, "note": "n", "data": {"p_value": null_p},
                         "error": None})
        axes.append({"axis": "costs", "ran": False, "note": "", "data": {}, "error": "not run"})
        d = self.ledger.path / key
        (d / "analysis.json").write_text(json.dumps({"run_key": key, "axes": axes}))

    def fill(self):
        return fill_validation(self.idea, book=self.book, store=self.store, ledger=self.ledger,
                               registry=self.registry, reads=self.reads)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_a_real_looking_idea_is_recommended_for_unlock(env):
    env.run("k1", "v1", 1)
    env.run("k2", "v2", 2)
    env.analysis("k1")
    v = env.fill()
    assert v.recommendation == "ready_for_unlock" and v.candidate_run in ("k1", "k2")
    assert (v.tier, v.dsr, v.null_p) == ("credible", 0.95, 0.01)
    assert v.n_eff == 2 and v.holdout_state == "locked"


def test_unmeasured_axes_are_listed_with_the_command_that_measures_them(env):
    env.run("k1", "v1", 1)
    env.analysis("k1")
    v = env.fill()
    missing = {m["axis"]: m["command"] for m in v.not_measured}
    assert set(missing) == set(ALL_AXES) - {"validation", "deflation", "null"}
    assert missing["costs"] == f"hermes analyze k1 --axes costs --idea {env.idea}"
    assert v.axes["costs"]["ran"] is False and v.axes["costs"]["error"] == "not run"


def test_a_missing_gate_axis_means_continue_never_clean(env):
    env.run("k1", "v1", 1)
    env.analysis("k1", ran=("validation",))
    v = env.fill()
    assert v.recommendation == "continue" and "deflation" in v.recommendation_reason
    assert "hermes analyze" in v.recommendation_reason


def test_no_analysis_at_all_is_continue_with_every_axis_unmeasured(env):
    env.run("k1", "v1", 1)
    v = env.fill()
    assert v.recommendation == "continue" and len(v.not_measured) == len(ALL_AXES)


def test_a_failed_gate_with_budget_left_is_continue(env):
    env.run("k1", "v1", 1)
    env.analysis("k1", tier="exploratory", dsr=0.4, null_p=0.4)
    v = env.fill()
    assert v.recommendation == "continue"
    for part in ("tier", "DSR 0.4", "null p 0.4"):
        assert part in v.recommendation_reason


def test_a_failed_gate_with_the_cluster_budget_spent_is_reject(env):
    env.store.write(env.idea, "plan", good_plan(budget={"max_clusters": 2, "max_extensions": 3}),
                    role="quant")
    env.run("k1", "v1", 1)
    env.run("k2", "v2", 2)
    env.analysis("k1", dsr=0.3)
    assert env.fill().recommendation == "reject"


def test_no_positive_cluster_is_reject(env):
    env.run("k1", "v1", 1)
    rec = env.ledger.load("k1")
    env.ledger.save("k1", {**rec.result, "metrics": {"sharpe": -0.3}}, rec.meta)
    env.analysis("k1", dsr=0.3)
    v = env.fill()
    assert v.recommendation == "reject" and "positive Sharpe" in v.recommendation_reason


def test_an_undeclared_read_blocks_ready_for_unlock(env):
    other = env.book.add("other", source="discretionary").id
    env.run("k1", "v1", 1)
    env.analysis("k1")
    env.reads.record(env.idea, command="show", target="zzz", target_idea=other)
    v = env.fill()
    assert v.read_not_declared == [other] and v.recommendation == "continue"


def test_variants_that_collapsed_into_one_cluster_are_flagged(env):
    env.run("k1", "v1", 1)
    rec = env.ledger.load("k1")
    env.ledger.save("k2", rec.result, rec.meta.__class__(**{**rec.meta.__dict__, "variant": "v2"}))
    env.analysis("k1")
    check = env.fill().plan_check
    assert check["variants_sharing_a_cluster"] == [["v1", "v2"]]
    assert check["planned_not_run"] == [] and check["run_not_planned"] == []


def test_the_plan_check_names_planned_not_run_and_run_not_planned(env):
    env.run("k1", "v1", 1)
    env.run("k9", "vX", 9)
    assert env.fill().plan_check["planned_not_run"] == ["v2"]
    assert env.fill().plan_check["run_not_planned"] == ["vX"]


def test_a_holdout_already_looked_at_is_recorded(env):
    env.run("k1", "v1", 1)
    env.analysis("k1")
    p = env.store.root / env.idea / "holdout.json"
    p.write_text(json.dumps({"state": "done", "passed": False}))
    v = env.fill()
    assert v.holdout_state == "unlocked:failed" and v.recommendation == "reject"
