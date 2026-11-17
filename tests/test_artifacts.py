"""Phase 3 step 3 (docs/phase-3-memory-and-state-plan.md): per-idea artifacts.

Seams: ``ArtifactStore`` (typed read/write, role permissions, plan lock), ``validate_spec`` /
``validate_plan`` (structural checks only), and the ``quantified`` / ``testing`` transitions
of ``IdeaBook``, which now need a valid spec and a locked, reviewed plan."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from hermes.research.artifacts import (
    PLAN_AXES,
    ArtifactPermission,
    ArtifactStore,
    Lessons,
    PlanLocked,
    Validation,
    validate_plan,
    validate_spec,
)
from hermes.research.ideas import IdeaBook, IllegalTransition
from tests.artifact_helpers import good_plan, good_spec


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "ideas")


# --- the store -----------------------------------------------------------------


def test_an_artifact_round_trips_and_carries_a_schema_version(store):
    store.write("i1", "spec", good_spec(), role="researcher")
    got = store.read("i1", "spec")
    assert got == good_spec() and got.schema_version == 1
    assert json.loads((store.root / "i1" / "spec.json").read_text())["schema_version"] == 1


def test_a_missing_artifact_reads_as_none(store):
    assert store.read("i1", "lessons") is None


def test_an_older_file_with_unknown_and_missing_keys_still_loads(store):
    path = store.root / "i1" / "spec.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"claim": "c", "from_the_future": 1}))
    got = store.read("i1", "spec")
    assert got.claim == "c" and got.falsifier == "" and got.parameters == []


def test_files_are_strict_json_and_nonfinite_numbers_survive(store):
    store.write("i1", "validation", Validation(verdict="continue", dsr=float("nan")),
                role="validator")
    text = (store.root / "i1" / "validation.json").read_text()
    json.loads(text, parse_constant=lambda c: pytest.fail(f"bare {c}"))
    assert store.read("i1", "validation").dsr != store.read("i1", "validation").dsr


def test_a_write_leaves_no_temp_file_behind(store):
    store.write("i1", "spec", good_spec(), role="researcher")
    assert [p.name for p in (store.root / "i1").iterdir()] == ["spec.json"]


def test_an_unknown_artifact_name_is_refused(store):
    with pytest.raises(ValueError, match="unknown artifact"):
        store.write("i1", "notes", good_spec(), role="researcher")


# --- who may write what --------------------------------------------------------


@pytest.mark.parametrize("name,artifact,allowed,denied", [
    ("spec", good_spec(), ["researcher", "quant"], ["validator", "developer"]),
    ("plan", good_plan(), ["quant", "validator"], ["researcher"]),
    ("validation", Validation(verdict="continue"), ["validator"], ["quant", "researcher"]),
    ("lessons", Lessons(outcome="rejected"), ["researcher"], ["quant", "validator"]),
])
def test_each_artifact_has_its_writers(store, name, artifact, allowed, denied):
    for role in allowed:
        store.write("i1", name, artifact, role=role)
    for role in denied:
        with pytest.raises(ArtifactPermission, match=role):
            store.write("i1", name, artifact, role=role)


def test_the_role_comes_from_the_environment_at_call_time(store, monkeypatch):
    monkeypatch.setenv("HERMES_ROLE", "quant")
    with pytest.raises(ArtifactPermission):
        store.write("i1", "validation", Validation(verdict="continue"))
    monkeypatch.setenv("HERMES_ROLE", "validator")
    store.write("i1", "validation", Validation(verdict="continue"))


def test_the_owner_with_no_role_can_write_anything(store, monkeypatch):
    monkeypatch.delenv("HERMES_ROLE", raising=False)
    store.write("i1", "validation", Validation(verdict="continue"))
    monkeypatch.setenv("HERMES_ROLE", "owner")
    store.write("i1", "lessons", Lessons(outcome="x"))


# --- the plan lock -------------------------------------------------------------


def test_a_plan_is_unlocked_until_the_validator_approves_it(store):
    store.write("i1", "plan", good_plan(), role="quant")
    assert store.read("i1", "plan").locked_hash is None


def test_a_writer_cannot_forge_an_approval(store):
    forged = good_plan(locked_hash="abc", review={"approved": True, "reviewer": "me"})
    store.write("i1", "plan", forged, role="quant")
    plan = store.read("i1", "plan")
    assert plan.locked_hash is None and plan.review.get("approved") is not True


def test_only_the_validator_can_approve_a_plan(store):
    store.write("i1", "plan", good_plan(), role="quant")
    with pytest.raises(ArtifactPermission):
        store.approve_plan("i1", reviewer="q", role="quant")
    plan = store.approve_plan("i1", reviewer="validator-1", role="validator")
    assert plan.locked_hash and plan.review == {
        "approved": True, "objections": [], "reviewer": "validator-1"}


def test_an_invalid_plan_cannot_be_approved(store):
    store.write("i1", "plan", good_plan(budget={"max_clusters": 4, "max_extensions": 9}),
                role="quant")
    with pytest.raises(ValueError, match="extensions"):
        store.approve_plan("i1", reviewer="v", role="validator")


def test_editing_the_variants_of_a_locked_plan_raises(store):
    store.write("i1", "plan", good_plan(), role="quant")
    locked = store.approve_plan("i1", reviewer="v", role="validator")
    edited = replace(locked, variants=locked.variants + [
        {"id": "v3", "axis": "exit", "change": "x", "tests": "y", "decision_rule": "z"}])
    with pytest.raises(PlanLocked):
        store.write("i1", "plan", edited, role="quant")
    assert len(store.read("i1", "plan").variants) == 2


def test_rewriting_a_locked_plan_without_touching_variants_keeps_the_lock(store):
    store.write("i1", "plan", good_plan(), role="quant")
    locked = store.approve_plan("i1", reviewer="v", role="validator")
    store.write("i1", "plan", replace(locked, budget={"max_clusters": 6, "max_extensions": 3}),
                role="quant")
    assert store.read("i1", "plan").locked_hash == locked.locked_hash


def test_an_amendment_unlocks_the_plan_and_needs_review_again(store):
    store.write("i1", "plan", good_plan(), role="quant")
    locked = store.approve_plan("i1", reviewer="v", role="validator")
    new = replace(locked, variants=locked.variants[:1])
    amended = store.amend_plan("i1", new, role="quant")
    assert amended.locked_hash is None and amended.review.get("approved") is not True
    assert amended.amendments[-1]["replaced_hash"] == locked.locked_hash
    store.approve_plan("i1", reviewer="v", role="validator")


# --- the structural validators ------------------------------------------------


def test_a_good_spec_and_a_good_plan_have_no_problems():
    assert validate_spec(good_spec()) == []
    assert validate_plan(good_plan()) == []


def test_a_number_in_the_rules_must_be_named():
    problems = validate_spec(good_spec(exit="exit after 8 bars or at the stop"))
    assert any("8" in p and "exit" in p for p in problems), problems


def test_a_named_number_is_accepted_wherever_it_appears():
    assert validate_spec(good_spec(entry="buy 3 bars after the break",
                                   invented_numbers=[{"name": "delay", "value": 3, "why": "w"},
                                                     {"name": "hold", "value": 6, "why": "w"},
                                                     {"name": "risk", "value": 1, "why": "w"}])) == []


def test_more_than_three_parameters_need_a_justification():
    params = [{"name": f"p{i}", "value": i} for i in range(4)]
    assert any("parameter" in p for p in validate_spec(good_spec(parameters=params)))
    assert validate_spec(good_spec(parameters=params,
                                   extra_parameters_justification="each is a physical constant")) == []


def test_a_spec_needs_a_falsifier_a_claim_and_a_lookahead_statement():
    problems = validate_spec(good_spec(falsifier="", claim="", lookahead=""))
    for word in ("falsifier", "claim", "look-ahead"):
        assert any(word in p for p in problems), (word, problems)


def test_the_axis_vocabulary_is_closed_and_a_parameter_nudge_is_not_an_axis():
    assert set(PLAN_AXES) == {"entry", "exit", "regime_filter", "instrument", "timeframe",
                              "horizon"}
    nudge = good_plan(variants=[{"id": "v1", "axis": "parameter", "change": "fast 10 -> 12",
                                 "tests": "t", "decision_rule": "r"}])
    assert any("nudge" in p for p in validate_plan(nudge))
    odd = good_plan(variants=[{"id": "v1", "axis": "vibes", "change": "c", "tests": "t",
                               "decision_rule": "r"}])
    assert any("vibes" in p for p in validate_plan(odd))


def test_every_variant_needs_a_decision_rule_and_a_unique_id():
    bad = good_plan(variants=[
        {"id": "v1", "axis": "entry", "change": "c", "tests": "t", "decision_rule": ""},
        {"id": "v1", "axis": "exit", "change": "c", "tests": "t", "decision_rule": "r"}])
    problems = validate_plan(bad)
    assert any("decision_rule" in p for p in problems) and any("duplicate" in p for p in problems)


def test_a_plan_needs_a_variant_and_a_bounded_budget():
    assert validate_plan(good_plan(variants=[]))
    assert any("extensions" in p for p in
               validate_plan(good_plan(budget={"max_clusters": 4, "max_extensions": 4})))


# --- the transitions that depend on them --------------------------------------


@pytest.fixture
def book(tmp_path):
    return IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")


def test_ideas_dir_is_the_artifact_directory(book, tmp_path):
    assert book.ideas_dir == tmp_path / "ideas"


def test_quantified_needs_a_valid_spec(book):
    idea = book.add("x", source="discretionary")
    with pytest.raises(IllegalTransition, match="spec"):
        book.update(idea.id, status="quantified")
    ArtifactStore(book.ideas_dir).write(idea.id, "spec", good_spec(falsifier=""), role="researcher")
    with pytest.raises(IllegalTransition, match="falsifier"):
        book.update(idea.id, status="quantified")
    ArtifactStore(book.ideas_dir).write(idea.id, "spec", good_spec(), role="researcher")
    assert book.update(idea.id, status="quantified").status == "quantified"


def test_testing_needs_a_locked_reviewed_plan(book):
    idea = book.add("x", source="discretionary")
    store = ArtifactStore(book.ideas_dir)
    store.write(idea.id, "spec", good_spec(), role="researcher")
    for status in ("quantified", "coded"):
        book.update(idea.id, status=status)
    with pytest.raises(IllegalTransition, match="plan"):
        book.update(idea.id, status="testing")
    store.write(idea.id, "plan", good_plan(), role="quant")
    with pytest.raises(IllegalTransition, match="reviewed"):
        book.update(idea.id, status="testing")
    store.approve_plan(idea.id, reviewer="v", role="validator")
    assert book.update(idea.id, status="testing").status == "testing"


def test_resuming_from_park_is_not_re_judged(book):
    """A legacy idea parked at `testing` (no plan) goes back to where it was."""
    idea = book.add("x", source="discretionary")
    ideas = book._load()
    ideas[0].status = "testing"
    book._save(ideas)
    book.update(idea.id, status="parked")
    assert book.update(idea.id, status="testing").status == "testing"
