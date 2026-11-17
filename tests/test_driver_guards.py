"""Phase 4 steps 3–4 (acceptance 4, 5): the boundaries the driver enforces, not the prompt."""

import os

import pytest

from hermes.research.artifacts import ArtifactPermission, Validation
from hermes.research.driver import role_env
from tests.artifact_helpers import good_plan
from tests.pipeline_helpers import Project


@pytest.fixture
def proj(tmp_path):
    return Project(tmp_path)


def test_a_quant_cannot_write_the_validators_file(proj):
    idea = proj.idea()
    seen = {}

    def greedy(i, p):
        try:
            proj.store.write(i, "validation", Validation(verdict="ready_for_unlock"))
        except ArtifactPermission as e:
            seen["error"] = str(e)
        proj.store.write(i, "plan", good_plan())

    proj.runner.scripts[("quant", "write_plan")] = greedy
    proj.driver.run(idea, max_steps=3)

    assert "quant" in seen["error"] and proj.store.read(idea, "validation") is None


def test_the_role_environment_is_restored_after_a_step(proj):
    before = os.environ.get("HERMES_ROLE")
    with role_env("quant", "x"):
        assert os.environ["HERMES_ROLE"] == "quant" and os.environ["HERMES_IDEA"] == "x"
    assert os.environ.get("HERMES_ROLE") == before and "HERMES_IDEA" not in os.environ


def test_the_validators_files_are_read_only_on_disk_once_it_is_done(proj):
    idea = proj.idea()
    proj.driver.run(idea, until="write_strategy")                       # plan reviewed
    assert not os.access(proj.store.path(idea, "plan"), os.W_OK)
    with pytest.raises(ArtifactPermission, match="sealed"):
        proj.store.write(idea, "plan", good_plan())

    proj.driver.run(idea, until="reflect")                              # verdict written
    assert not os.access(proj.store.path(idea, "validation"), os.W_OK)


def test_a_plan_on_one_axis_is_refused_before_the_validator_sees_it(proj):
    idea = proj.idea()
    one_axis = good_plan(variants=[
        {"id": f"v{i}", "axis": "entry", "change": "c", "tests": "t", "decision_rule": "d"}
        for i in range(3)])
    proj.runner.scripts[("quant", "write_plan")] = lambda i, p: proj.store.write(
        i, "plan", one_axis)

    summary = proj.driver.run(idea)

    assert summary.stopped == "stalled"
    assert not [c for c in proj.runner.calls if c["role"] == "validator"]
    retry_prompt = [c for c in proj.runner.calls if c["step"] == "write_plan"][1]["prompt"]
    assert "same axis" in retry_prompt


def test_a_plan_the_validator_sends_back_goes_to_the_quant_again(proj):
    idea = proj.idea()
    reviews = []

    def review(i, p):
        reviews.append(1)
        if len(reviews) == 1:
            plan = proj.store.read(i, "plan")
            plan.review = {"approved": False, "objections": ["v2 is a parameter nudge"],
                           "reviewer": "v"}
            proj.store.write(i, "plan", plan)
        else:
            proj.store.approve_plan(i, reviewer="v")

    proj.runner.scripts[("validator", "review_plan")] = review
    proj.driver.run(idea, until="write_strategy")

    steps = [c["step"] for c in proj.runner.calls]
    assert steps[:5] == ["write_spec", "write_plan", "review_plan", "write_plan", "review_plan"]
    assert proj.store.read(idea, "plan").locked_hash


def test_a_continue_verdict_amends_the_plan_and_the_old_verdict_is_archived(proj):
    idea = proj.idea()
    proj.recommendation = "continue"
    rounds = []

    def validate(i, p):
        rounds.append(1)
        v = "continue" if len(rounds) == 1 else "reject"
        proj.recommendation = v
        proj.store.write(i, "validation", Validation(
            verdict=v, rationale="r", override_reason="the second look is clearer",
            failure={"kind": "no_edge", "assumption": "a"}))

    def amend(i, p):
        plan = good_plan(variants=[
            {"id": "v3", "axis": "horizon", "change": "hold 2 sessions", "tests": "t",
             "decision_rule": "d"},
            {"id": "v4", "axis": "exit", "change": "trail", "tests": "t", "decision_rule": "d"}])
        proj.store.amend_plan(i, plan)

    proj.runner.scripts[("validator", "validate")] = validate
    proj.runner.scripts[("quant", "amend_plan")] = amend
    proj.driver.run(idea)

    steps = [c["step"] for c in proj.runner.calls]
    assert steps.count("validate") == 2 and "amend_plan" in steps
    assert steps.index("amend_plan") < len(steps) - steps[::-1].index("review_plan") - 1 + 1
    assert proj.book.get(idea).status == "rejected"
    assert proj.store.path(idea, "validation").with_name("validation.round1.json").exists()
