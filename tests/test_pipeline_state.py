"""Phase 3 steps 7-8 (library side): the thesis template, ``next_action``, the status report
and ``reopen``. Everything is a pure function of files on disk, so a crash costs nothing:
"resume" is "call next_action again"."""

from __future__ import annotations

import pytest

from hermes.research.approvals import ApprovalBook
from hermes.research.artifacts import ArtifactStore, Lessons, Validation
from hermes.research.ideas import IdeaBook, IllegalTransition
from hermes.research.ledger import RunLedger
from hermes.research.pipeline import Action, next_action, status_report
from hermes.research.thesis import HEADINGS, Thesis, due, ensure_template, read_thesis
from tests.artifact_helpers import good_plan, good_spec

FAIL = {"kind": "no_edge", "assumption": "x"}


@pytest.fixture
def book(tmp_path):
    return IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")


@pytest.fixture
def store(book):
    return ArtifactStore(book.ideas_dir)


def _at(book, status, **kw):
    idea = book.add(kw.pop("title", "x"))
    ideas = book._load()
    for i in ideas:
        if i.id == idea.id:
            i.status = status
            for k, v in kw.items():
                setattr(i, k, v)
    book._save(ideas)
    return book.get(idea.id)


# --- next_action ----------------------------------------------------------------


def _act(idea, store, **kw):
    a = next_action(idea, store, **kw)
    return None if a is None else (a.role, a.step)


def test_a_raw_idea_needs_a_spec_then_to_be_marked_quantified(book, store):
    idea = _at(book, "raw")
    assert _act(idea, store) == ("researcher", "write_spec")
    store.write(idea.id, "spec", good_spec(), role="researcher")
    assert _act(idea, store) == ("researcher", "mark_quantified")


def test_a_quantified_idea_walks_plan_review_strategy_and_coding(book, store):
    idea = _at(book, "quantified")
    assert _act(idea, store) == ("quant", "write_plan")
    store.write(idea.id, "plan", good_plan(), role="quant")
    assert _act(idea, store) == ("validator", "review_plan")
    store.approve_plan(idea.id, reviewer="v", role="validator")
    assert _act(idea, store) == ("quant", "write_strategy")
    idea = _at(book, "quantified", strategy="s")
    store.write(idea.id, "plan", good_plan(), role="quant")
    store.approve_plan(idea.id, reviewer="v", role="validator")
    assert _act(idea, store) == ("quant", "mark_coded")


def test_a_coded_idea_with_a_locked_plan_is_ready_to_start_testing(book, store):
    idea = _at(book, "coded")
    assert _act(idea, store) == ("quant", "write_plan")
    store.write(idea.id, "plan", good_plan(), role="quant")
    store.approve_plan(idea.id, reviewer="v", role="validator")
    assert _act(idea, store) == ("quant", "mark_testing")


def test_testing_runs_the_unrun_variants_then_asks_the_validator(book, store):
    idea = _at(book, "testing")
    store.write(idea.id, "plan", good_plan(), role="quant")
    store.approve_plan(idea.id, reviewer="v", role="validator")
    assert _act(idea, store) == ("quant", "run_variants")
    assert _act(idea, store, ran_variants={"v1"}) == ("quant", "run_variants")
    assert _act(idea, store, ran_variants={"v1", "v2"}) == ("validator", "validate")


@pytest.mark.parametrize("verdict,expected", [
    ("reject", ("validator", "reject_idea")),
    ("continue", ("quant", "amend_plan")),
    ("ready_for_unlock", ("owner", "unlock_holdout")),
])
def test_the_validators_verdict_decides_what_follows(book, store, verdict, expected):
    idea = _at(book, "testing")
    store.write(idea.id, "plan", good_plan(), role="quant")
    store.approve_plan(idea.id, reviewer="v", role="validator")
    store.write(idea.id, "validation", Validation(verdict=verdict), role="validator")
    assert _act(idea, store, ran_variants={"v1", "v2"}) == expected


def test_blocked_and_decided_ideas(book, store):
    assert _act(_at(book, "needs_infra"), store) == ("developer", "build_infra")
    assert _act(_at(book, "awaiting_approval", approval_id="a1"), store) == (
        "owner", "answer_approval")
    rejected = _at(book, "rejected")
    assert _act(rejected, store) == ("researcher", "reflect")
    store.write(rejected.id, "lessons", Lessons(outcome="x"), role="researcher")
    assert _act(rejected, store) is None
    validated = _at(book, "validated")
    assert _act(validated, store) == ("researcher", "reflect")
    store.write(validated.id, "lessons", Lessons(outcome="x"), role="researcher")
    assert _act(validated, store) == ("owner", "approve_forward_test")
    assert _act(_at(book, "parked"), store) is None
    assert _act(_at(book, "ready_to_forward_test"), store) is None


def test_next_action_is_pure_and_resumes_from_what_is_on_disk(book, store):
    idea = _at(book, "raw")
    first = next_action(idea, store)
    assert next_action(idea, store) == first and isinstance(first, Action)
    store.write(idea.id, "spec", good_spec(), role="researcher")   # "a crash", then a new call
    assert next_action(idea, store).step == "mark_quantified"
    assert first.produces == ("spec.json",)


# --- the thesis ---------------------------------------------------------------


def test_the_template_has_fixed_headings_and_is_not_overwritten(tmp_path):
    path = tmp_path / "thesis.md"
    ensure_template(path)
    thesis = read_thesis(path)
    assert tuple(thesis.sections) == HEADINGS and thesis.front["ideas_included"] == []
    path.write_text(path.read_text() + "\nhand-written\n")
    ensure_template(path)
    assert "hand-written" in path.read_text()


def test_a_heading_and_a_tag_excerpt_can_be_read(tmp_path):
    path = tmp_path / "thesis.md"
    path.write_text("---\nupdated: 2026-01-01T00:00:00+00:00\nideas_included: [\"a\"]\n---\n"
                    "# Thesis\n## Survived\n- dip buy holds (equities)\n- gold orb (gold)\n"
                    "## Failed\nnothing about fx\n## Open questions\n")
    thesis = read_thesis(path)
    assert thesis.front["updated"] == "2026-01-01T00:00:00+00:00"
    assert thesis.front["ideas_included"] == ["a"]
    assert "dip buy" in thesis.section("Survived")
    excerpt = thesis.excerpt(["gold"])
    assert "gold orb" in excerpt and "dip buy" not in excerpt and "Survived" in excerpt
    assert Thesis.empty().excerpt(["gold"]) == ""


def test_the_thesis_is_due_after_five_new_lessons_or_an_empty_queue(tmp_path, book, store):
    path = tmp_path / "thesis.md"
    ensure_template(path)
    for n in range(4):
        idea = _at(book, "rejected", title=f"r{n}")
        store.write(idea.id, "lessons", Lessons(outcome="x"), role="researcher")
    open_idea = _at(book, "raw", title="still open")
    assert due(book, store, path) is False
    idea = _at(book, "rejected", title="r5")
    store.write(idea.id, "lessons", Lessons(outcome="x"), role="researcher")
    assert due(book, store, path) is True
    # lessons older than the thesis do not count
    path.write_text(path.read_text().replace('updated: ""', 'updated: "2999-01-01T00:00:00+00:00"'))
    assert due(book, store, path) is False
    book.remove(open_idea.id)                       # the queue is now empty
    assert due(book, store, path) is True


# --- reopen ----------------------------------------------------------------------


def test_reopen_needs_a_reason_and_is_the_only_way_out_of_rejected(book, store):
    idea = _at(book, "rejected")
    with pytest.raises(IllegalTransition):
        book.update(idea.id, status="quantified")
    with pytest.raises(ValueError, match="reason"):
        book.reopen(idea.id, "")
    with pytest.raises(IllegalTransition, match="spec"):
        book.reopen(idea.id, "new data")
    store.write(idea.id, "spec", good_spec(), role="researcher")
    done = book.reopen(idea.id, "new data arrived")
    assert done.status == "quantified"
    assert done.history[-1]["reason"] == "reopen: new data arrived"
    with pytest.raises(IllegalTransition):
        book.reopen(done.id, "again")               # only rejected ideas can be reopened


# --- the status report ----------------------------------------------------------


def test_the_status_report_gathers_the_queue(tmp_path, book, store):
    approvals = ApprovalBook(tmp_path / "approvals.json", book)
    thesis = tmp_path / "thesis.md"
    ensure_template(thesis)
    ledger = RunLedger(tmp_path / "runs")
    waiting = _at(book, "coded", title="waits")
    approvals.park(waiting.id, "advisor", "Run it?", "yes")
    unreflected = _at(book, "rejected", title="decided")
    _at(book, "raw", title="fresh")

    report = status_report(book, store, approvals, ledger, thesis)

    assert report["by_status"]["raw"] == 1 and report["by_status"]["awaiting_approval"] == 1
    assert [a["idea"] for a in report["pending_approvals"]] == [waiting.id]
    assert report["awaiting_reflection"] == [unreflected.id]
    assert report["thesis_due"] is False
    steps = {n["idea"]: n["next_action"]["step"] for n in report["open"]}
    assert steps[waiting.id] == "answer_approval" and "write_spec" in steps.values()
