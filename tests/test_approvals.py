"""Phase 3 step 5: approvals. An idea that needs an owner decision is *parked* on it, in one
library call, and the owner's answer moves it on. Seams: ``ApprovalBook.park`` / ``answer``;
``ready_to_forward_test`` is reachable only through a ``forward_test`` approval."""

from __future__ import annotations

import json

import pytest

from hermes.research.approvals import ApprovalBook, ApprovalPermission
from hermes.research.ideas import IdeaBook, IllegalTransition

FAIL = {"kind": "no_edge", "assumption": "x"}


_EST = {"model": "m", "low": 0.5, "high": 1.0, "blocked": ""}


@pytest.fixture
def book(tmp_path):
    return IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")


@pytest.fixture
def approvals(tmp_path, book):
    return ApprovalBook(tmp_path / "approvals.json", book)


def _idea(book, status):
    idea = book.add("x")
    ideas = book._load()
    ideas[0].status = status
    book._save(ideas)
    return idea


def test_parking_records_the_question_and_parks_the_idea_in_one_call(book, approvals):
    idea = _idea(book, "coded")
    a = approvals.park(idea.id, "advisor", "Run the advisor on 3,000 signals?",
                       "yes, capped at $5", evidence=["research/ideas/x/plan.json"],
                       estimate={"usd": 4.2})

    assert a.status == "pending" and a.kind == "advisor" and a.estimate == {"usd": 4.2}
    got = book.get(idea.id)
    assert (got.status, got.resume_status, got.approval_id) == ("awaiting_approval", "coded", a.id)
    assert [h["to"] for h in got.history] == ["awaiting_approval"]


def test_approving_returns_the_idea_to_where_it_was(book, approvals):
    idea = _idea(book, "testing")
    a = approvals.park(idea.id, "advisor", "q", "r", estimate=_EST)
    done = approvals.answer(a.id, "approved", note="go", by="owner")

    assert done.status == "approved" and done.answered_by == "owner" and done.note == "go"
    got = book.get(idea.id)
    assert got.status == "testing" and got.approval_id is None and got.resume_status is None


def test_a_denied_advisor_approval_sends_the_idea_back_and_the_pipeline_keeps_moving(book, approvals):
    idea = _idea(book, "coded")
    a = approvals.park(idea.id, "advisor", "q", "r", estimate=_EST)
    approvals.answer(a.id, "denied", note="no budget")
    got = book.get(idea.id)
    assert got.status == "coded" and "no budget" in got.history[-1]["reason"]


def test_other_denials_park_the_idea_unless_the_owner_says_reject(book, approvals):
    a_idea, b_idea = _idea(book, "needs_infra"), _idea(book, "coded")
    a = approvals.park(a_idea.id, "dev_merge", "merge?", "yes", branch="feat/l2")
    approvals.answer(a.id, "denied", note="not now")
    assert book.get(a_idea.id).status == "parked"

    b = approvals.park(b_idea.id, "instrument_change", "switch to XAGUSD?", "no")
    approvals.answer(b.id, "denied", note="wrong market", deny_to="rejected")
    got = book.get(b_idea.id)
    assert got.status == "rejected" and got.verdict == "wrong market"
    assert got.failure["kind"] == "other"


def test_a_forward_test_approval_is_the_only_way_to_ready_to_forward_test(book, approvals):
    idea = _idea(book, "validated")
    with pytest.raises(IllegalTransition, match="approval"):
        book.update(idea.id, status="ready_to_forward_test")
    a = approvals.park(idea.id, "forward_test", "Forward test it?", "yes")
    assert book.get(idea.id).status == "awaiting_approval"
    approvals.answer(a.id, "approved")
    assert book.get(idea.id).status == "ready_to_forward_test"


def test_an_approval_of_another_kind_does_not_license_forward_testing(book, approvals):
    idea = _idea(book, "validated")
    a = approvals.park(idea.id, "other", "q", "r")
    approvals.answer(a.id, "approved")
    assert book.get(idea.id).status == "validated"


def test_a_denied_forward_test_leaves_the_idea_validated(book, approvals):
    idea = _idea(book, "validated")
    a = approvals.park(idea.id, "forward_test", "q", "r")
    approvals.answer(a.id, "denied", note="not yet")
    assert book.get(idea.id).status == "validated"


def test_an_agent_role_cannot_answer(book, approvals, monkeypatch):
    idea = _idea(book, "coded")
    a = approvals.park(idea.id, "advisor", "q", "r", estimate=_EST)
    monkeypatch.setenv("HERMES_ROLE", "quant")
    with pytest.raises(ApprovalPermission):
        approvals.answer(a.id, "approved")
    assert approvals.get(a.id).status == "pending"
    assert book.get(idea.id).status == "awaiting_approval"


def test_the_owner_can_answer_with_no_role_or_the_owner_role(book, approvals, monkeypatch):
    idea = _idea(book, "coded")
    a = approvals.park(idea.id, "advisor", "q", "r", estimate=_EST)
    monkeypatch.setenv("HERMES_ROLE", "owner")
    assert approvals.answer(a.id, "approved").answered_by == "owner"


def test_an_approval_is_answered_once(book, approvals):
    idea = _idea(book, "coded")
    a = approvals.park(idea.id, "advisor", "q", "r", estimate=_EST)
    approvals.answer(a.id, "approved")
    with pytest.raises(ValueError, match="already"):
        approvals.answer(a.id, "denied")


def test_an_idea_already_waiting_cannot_be_parked_again(book, approvals):
    idea = _idea(book, "coded")
    approvals.park(idea.id, "advisor", "q", "r", estimate=_EST)
    with pytest.raises(ValueError, match="awaiting"):
        approvals.park(idea.id, "other", "q2", "r2")
    assert len(approvals.all()) == 1


def test_a_rejected_idea_cannot_be_parked(book, approvals):
    idea = _idea(book, "rejected")
    with pytest.raises(IllegalTransition):
        approvals.park(idea.id, "other", "q", "r")
    assert approvals.all() == []


def test_unknown_kind_and_decision_are_refused(book, approvals):
    idea = _idea(book, "coded")
    with pytest.raises(ValueError, match="kind"):
        approvals.park(idea.id, "vibes", "q", "r")
    a = approvals.park(idea.id, "other", "q", "r")
    with pytest.raises(ValueError, match="decision"):
        approvals.answer(a.id, "maybe")


def test_pending_lists_and_prefixes_resolve(book, approvals):
    a_idea, b_idea = _idea(book, "coded"), _idea(book, "coded")
    a = approvals.park(a_idea.id, "advisor", "q", "r", estimate=_EST)
    b = approvals.park(b_idea.id, "other", "q", "r")
    approvals.answer(a.id, "approved")
    assert [x.id for x in approvals.all(pending_only=True)] == [b.id]
    assert approvals.get(b.id[:4]).id == b.id


def test_the_file_is_strict_json_and_survives_a_new_process(tmp_path, book, approvals):
    idea = _idea(book, "coded")
    a = approvals.park(idea.id, "advisor", "q", "r", estimate={"usd": float("inf")})
    text = (tmp_path / "approvals.json").read_text()
    json.loads(text, parse_constant=lambda c: pytest.fail(c))
    again = ApprovalBook(tmp_path / "approvals.json", book).get(a.id)
    assert again.estimate == {"usd": float("inf")}
