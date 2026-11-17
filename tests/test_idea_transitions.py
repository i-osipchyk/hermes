"""Phase 3 step 1 (docs/phase-3-memory-and-state-plan.md, ADR-0012/0017): the status model and
its guarded transitions live in the library, not in the CLI.

Seam: ``IdeaBook.update(id, status=…, reason=…)``. An illegal edge raises ``IllegalTransition``;
``validated`` needs a passed ``holdout.json``; ``ready_to_forward_test`` is not reachable here;
every change is appended to ``history``."""

from __future__ import annotations

import json

import pytest

from hermes.research.ideas import (
    FAILURE_KINDS,
    STATUSES,
    TRANSITIONS,
    IdeaBook,
    IllegalTransition,
)
from tests.artifact_helpers import ready_for_testing

FAILURE = {"kind": "no_edge", "assumption": "momentum persists", "detail": ""}


@pytest.fixture
def book(tmp_path):
    return IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")


def _earn(book, idea_id, passed=True):
    path = book.holdout_dir / idea_id / "holdout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": "done", "passed": passed}))


def _at(book, status, *, source="trader"):
    """An idea parked directly at ``status`` (bypassing the rules, as an old record would be)."""
    idea = book.add("x", source=source)
    ideas = book._load()
    ideas[0].status = status
    book._save(ideas)
    return idea


def _changes(to):
    """The fields a transition to ``to`` requires, so a test of the edge is only about the edge."""
    extra = {"rejected": {"failure": FAILURE, "verdict": "no edge"},
             "needs_infra": {"failure": {"kind": "infra", "assumption": "", "detail": "needs L2 data"}},
             "literature_done": {"links": ["https://example.org/paper"]}}
    return extra.get(to, {})


# --- the graph ---------------------------------------------------------------

_ALL = [(a, b) for a in STATUSES for b in STATUSES if a != b]
# validated needs holdout.json and ready_to_forward_test an approval; both are tested below
_EDGES = [(a, b) for a, b in _ALL if b in TRANSITIONS[a] and b not in ("validated", "ready_to_forward_test")]
_NON_EDGES = [(a, b) for a, b in _ALL if b not in TRANSITIONS[a]]


@pytest.mark.parametrize("src,dst", _EDGES)
def test_every_legal_edge_passes_and_is_recorded(book, src, dst):
    idea = _at(book, src)
    if src == "awaiting_approval":
        book._save([_with(book._load()[0], resume_status=dst)])
    if dst in ("quantified", "testing"):
        ready_for_testing(book.ideas_dir, idea.id)
    done = book.update(idea.id, status=dst, reason="because", **_changes(dst))

    assert done.status == dst
    last = done.history[-1]
    assert (last["from"], last["to"], last["reason"]) == (src, dst, "because")
    assert last["by"] == "owner" and last["at"]


def _with(idea, **kw):
    for k, v in kw.items():
        setattr(idea, k, v)
    return idea


@pytest.mark.parametrize("src,dst", _NON_EDGES)
def test_every_other_edge_raises_and_changes_nothing(book, src, dst):
    idea = _at(book, src)
    if dst == "validated":
        _earn(book, idea.id)
    with pytest.raises(IllegalTransition, match=dst):
        book.update(idea.id, status=dst, **_changes(dst))
    after = book.get(idea.id)
    assert after.status == src and after.history == []


def test_a_role_is_recorded_as_the_actor(book, monkeypatch):
    monkeypatch.setenv("HERMES_ROLE", "quant")
    idea = _at(book, "raw")
    done = book.update(idea.id, status="parked", reason="waiting on data")
    assert done.history[-1]["by"] == "quant"


def test_setting_the_status_an_idea_already_has_is_not_a_transition(book):
    idea = _at(book, "raw")
    done = book.update(idea.id, status="raw", hypothesis="h")
    assert done.history == [] and done.hypothesis == "h"


def test_the_graph_has_no_edge_out_of_rejected():
    assert TRANSITIONS["rejected"] == frozenset()


def test_a_parked_idea_resumes_where_it_was(book):
    idea = _at(book, "raw")
    ready_for_testing(book.ideas_dir, idea.id)
    book.update(idea.id, status="quantified")
    book.update(idea.id, status="parked", reason="blocked")
    with pytest.raises(IllegalTransition):
        book.update(idea.id, status="testing")                # never been there
    assert book.update(idea.id, status="quantified").status == "quantified"


# --- validated is earned ------------------------------------------------------


def test_validated_is_refused_without_a_passed_holdout(book):
    idea = _at(book, "testing")
    with pytest.raises(IllegalTransition, match="holdout"):
        book.update(idea.id, status="validated")
    _earn(book, idea.id, passed=False)
    with pytest.raises(IllegalTransition, match="holdout"):
        book.update(idea.id, status="validated")
    assert book.get(idea.id).status == "testing"


def test_validated_with_a_passed_holdout_records_its_basis(book):
    idea = _at(book, "testing")
    _earn(book, idea.id)
    done = book.update(idea.id, status="validated", verdict="held out")
    assert done.status == "validated" and done.validation_basis == "holdout"


def test_a_started_but_unfinished_holdout_does_not_count(book):
    idea = _at(book, "testing")
    path = book.holdout_dir / idea.id / "holdout.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"state": "started", "passed": None}))
    with pytest.raises(IllegalTransition):
        book.update(idea.id, status="validated")


# --- the way to ready_to_forward_test is not through update ------------------


def test_ready_to_forward_test_cannot_be_set_through_update(book):
    idea = _at(book, "validated")
    with pytest.raises(IllegalTransition, match="approval"):
        book.update(idea.id, status="ready_to_forward_test")


# --- rules attached to a transition ------------------------------------------


def test_literature_done_needs_a_source_link_unless_the_idea_is_discretionary_or_data(book):
    book_idea = _at(book, "raw", source="book")
    with pytest.raises(IllegalTransition, match="link"):
        book.update(book_idea.id, status="literature_done")
    assert book.update(book_idea.id, status="literature_done",
                       links=["https://ssrn.com/x"]).links == ["https://ssrn.com/x"]


@pytest.mark.parametrize("source", ["discretionary", "data"])
def test_no_link_is_needed_for_a_discretionary_or_data_idea(book, source):
    idea = _at(book, "raw", source=source)
    assert book.update(idea.id, status="literature_done").status == "literature_done"


def test_rejected_needs_a_failure_and_a_verdict(book):
    idea = _at(book, "testing")
    with pytest.raises(IllegalTransition, match="failure"):
        book.update(idea.id, status="rejected", verdict="no edge")
    with pytest.raises(IllegalTransition, match="verdict"):
        book.update(idea.id, status="rejected", failure=FAILURE)
    done = book.update(idea.id, status="rejected", failure=FAILURE, verdict="no edge")
    assert done.failure["kind"] == "no_edge"


def test_a_failure_kind_must_come_from_the_closed_vocabulary(book):
    idea = _at(book, "testing")
    assert "holdout_failed" in FAILURE_KINDS and "duplicate" in FAILURE_KINDS
    with pytest.raises(ValueError, match="failure kind"):
        book.update(idea.id, status="rejected", verdict="v",
                    failure={"kind": "bad luck", "assumption": "a"})


def test_needs_infra_names_the_missing_capability(book):
    idea = _at(book, "quantified")
    with pytest.raises(IllegalTransition, match="capability"):
        book.update(idea.id, status="needs_infra")
    done = book.update(idea.id, status="needs_infra",
                       failure={"kind": "infra", "assumption": "", "detail": "needs tick data"})
    assert done.failure["detail"] == "needs tick data"


def test_entering_awaiting_approval_remembers_where_to_resume(book):
    idea = _at(book, "coded")
    done = book.update(idea.id, status="awaiting_approval", approval_id="a1")
    assert done.resume_status == "coded" and done.approval_id == "a1"


# --- old records stay valid ---------------------------------------------------


def test_an_old_record_loads_with_the_new_fields_defaulted(tmp_path):
    path = tmp_path / "ideas.json"
    path.write_text(json.dumps([{"id": "old1", "title": "t", "status": "rejected",
                                 "verdict": "no", "source": "book", "created_at": "2025-01-01"}]))
    (idea,) = IdeaBook(path).all()
    assert (idea.history, idea.links, idea.failure, idea.parent) == ([], [], None, None)
    assert idea.schema_version == 2


def test_a_hand_edit_of_the_file_is_still_read_as_is(tmp_path):
    """The guard is on writes through the library; it does not re-judge what is on disk."""
    path = tmp_path / "ideas.json"
    path.write_text(json.dumps([{"id": "v1", "title": "t", "status": "validated"}]))
    assert IdeaBook(path).get("v1").status == "validated"
