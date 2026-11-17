"""Phase 4 (docs/phase-4-roles-and-pipeline-plan.md §2–4): the Validator's files are sealed
read-only after it writes them, the computed recommendation can only be tightened, and a plan
that explores one axis is refused. Seam: ``ArtifactStore``."""

import os

import pytest

from hermes.research.artifacts import (
    ArtifactPermission,
    ArtifactStore,
    Validation,
    VerdictLoosened,
    validate_plan,
    validate_validation,
)
from tests.artifact_helpers import good_plan, good_spec


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "ideas")


def _filled(store, recommendation="continue"):
    store.write_mechanical("i1", Validation(recommendation=recommendation,
                                            recommendation_reason="gate", tier="exploratory",
                                            candidate_run="k1"), role="validator")


def test_a_verdict_as_cautious_as_the_recommendation_is_kept(store):
    _filled(store, "continue")
    done = store.write("i1", "validation", Validation(verdict="continue", rationale="r"),
                       role="validator")
    assert done.verdict == "continue" and done.recommendation == "continue"


def test_a_verdict_more_permissive_than_the_recommendation_is_refused(store):
    _filled(store, "continue")
    with pytest.raises(VerdictLoosened, match="more permissive"):
        store.write("i1", "validation",
                    Validation(verdict="ready_for_unlock", rationale="looks fine"),
                    role="validator")
    assert store.read("i1", "validation").verdict == ""


def test_a_more_cautious_verdict_needs_a_reason(store):
    _filled(store, "ready_for_unlock")
    with pytest.raises(VerdictLoosened, match="override_reason"):
        store.write("i1", "validation", Validation(verdict="reject", rationale="r"),
                    role="validator")
    done = store.write("i1", "validation", Validation(
        verdict="reject", rationale="r", override_reason="the edge is one regime",
        failure={"kind": "regime_dependent", "assumption": "edge persists"}), role="validator")
    assert done.verdict == "reject"


def test_the_agent_cannot_rewrite_the_computed_fields(store):
    _filled(store, "continue")
    done = store.write("i1", "validation", Validation(
        verdict="continue", rationale="r", recommendation="ready_for_unlock", tier="deployable",
        candidate_run="other"), role="validator")
    assert (done.recommendation, done.tier, done.candidate_run) == ("continue", "exploratory", "k1")


def test_only_the_validator_fills_validation(store):
    with pytest.raises(ArtifactPermission):
        store.write_mechanical("i1", Validation(), role="quant")


def test_a_sealed_file_cannot_be_written_again(store):
    _filled(store)
    store.write("i1", "validation", Validation(verdict="continue", rationale="r"),
                role="validator")
    store.seal("i1", "validation")
    assert not os.access(store.path("i1", "validation"), os.W_OK)
    with pytest.raises(ArtifactPermission, match="sealed"):
        store.write("i1", "validation", Validation(verdict="continue", rationale="x"),
                    role="validator")


def test_a_new_round_unseals_and_amending_a_plan_archives_the_old_verdict(store):
    store.write("i1", "spec", good_spec(), role="researcher")
    store.write("i1", "plan", good_plan(), role="quant")
    store.approve_plan("i1", reviewer="v", role="validator")
    _filled(store)
    store.seal("i1", "plan")
    store.seal("i1", "validation")

    store.amend_plan("i1", good_plan(), role="quant")

    assert store.read("i1", "validation") is None
    assert store.path("i1", "validation").with_name("validation.round1.json").exists()
    assert store.read("i1", "plan").locked_hash is None


def test_validation_shape_needs_a_rationale_and_a_failure_for_reject():
    assert validate_validation(Validation(verdict="reject", rationale="r"))
    assert validate_validation(Validation(verdict="ready_for_unlock", rationale="r"))
    assert validate_validation(Validation(verdict="continue", rationale="")) == [
        "rationale is missing"]
    ok = Validation(verdict="reject", rationale="r",
                    failure={"kind": "no_edge", "assumption": "momentum"})
    assert validate_validation(ok) == []


def test_a_plan_whose_variants_mostly_share_an_axis_is_refused():
    one_axis = [{"id": f"v{i}", "axis": "entry", "change": "c", "tests": "t",
                 "decision_rule": "d"} for i in range(3)]
    assert any("same axis" in p for p in validate_plan(good_plan(variants=one_axis)))
    mixed = one_axis[:1] + [{**one_axis[1], "axis": "exit"}, {**one_axis[2], "axis": "horizon"}]
    assert validate_plan(good_plan(variants=mixed)) == []
    assert validate_plan(good_plan(variants=one_axis[:1])) == []     # one variant: nothing to spread
