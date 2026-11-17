"""Phase 4 step 6 (acceptance 7): the advisor's cost is computed, and a run is gated on it.

Seams: ``estimate_advisor`` (arithmetic on a fixture of observability rows), the approval, and
``hermes run --advisor-approval``."""

import json
from datetime import UTC, datetime

import pytest
from llmkit import Usage, cost

from hermes.ai.observability import LLMCallRecord, LLMObservabilityLog
from hermes.research.advisor_estimate import (
    AdvisorRefused,
    check_advisor_run,
    estimate_advisor,
    history_tokens,
    spent_for_idea,
)
from hermes.research.approvals import ApprovalBook
from hermes.research.ideas import IdeaBook
from hermes.research.ledger import RunLedger
from tests.artifact_helpers import ready_for_testing
from tests.pipeline_helpers import save_run

MODEL = "deepseek-v4-flash"
AT = datetime(2026, 11, 17, tzinfo=UTC)


def _price(model, i, o):
    c = cost(model, Usage(input_tokens=i, output_tokens=o), AT)
    return None if c is None else c.total


def _call(model=MODEL, i=1000, o=100, cached=False, cost_usd=None, error=False):
    return LLMCallRecord(
        request_time=AT, model_id=model, from_decision_cache=cached,
        input_tokens=None if cached else i, output_tokens=None if cached else o,
        cache_read_tokens=None, cache_creation_tokens=None, latency_ms=None, cost_usd=cost_usd,
        approved=True, confidence=0.5, reason="r", system_prompt="", user_prompt="", is_error=error)


def _ledger_with_calls(tmp_path, calls, idea="i1"):
    ledger = RunLedger(tmp_path / "runs")
    save_run(ledger, "k1", idea, "v1", 1)
    rec = ledger.load("k1")
    ledger.save("k1", rec.result, rec.meta, llm_log=LLMObservabilityLog(calls=calls))
    return ledger


@pytest.fixture(autouse=True)
def _known_price():
    if _price(MODEL, 1000, 100) is None:
        pytest.skip(f"{MODEL} is not in this llmkit's pricing table")


def test_history_is_the_billed_calls_for_that_model_only(tmp_path):
    ledger = _ledger_with_calls(tmp_path, [_call(i=1000), _call(i=3000), _call(cached=True),
                                           _call(error=True), _call(model="other")])
    assert history_tokens(ledger, MODEL) == [(1000, 100), (3000, 100)]


def test_the_estimate_is_signals_times_tokens_times_price_minus_cached():
    est = estimate_advisor(model=MODEL, signals=120, cached=20, variants=3,
                           history=[(1000, 100), (3000, 100)], at=AT)
    each_low, each_high = _price(MODEL, 2000, 100), _price(MODEL, 3000, 100)
    assert est["calls"] == 100 and est["blocked"] == ""
    assert est["low"] == pytest.approx(100 * each_low * 3, abs=1e-4)
    assert est["high"] == pytest.approx(100 * each_high * 3 * 1.2, abs=1e-4)
    assert est["high"] > est["low"] and est["source"].startswith("history (2")


def test_no_history_and_no_given_tokens_is_blocked_not_guessed():
    est = estimate_advisor(model=MODEL, signals=50, at=AT)
    assert est["low"] is None and "no billed advisor calls" in est["blocked"]


def test_tokens_given_by_the_caller_are_disclosed_as_given():
    est = estimate_advisor(model=MODEL, signals=10, given_tokens=(1000, 100), at=AT)
    assert est["source"] == "given" and est["low"] == pytest.approx(10 * _price(MODEL, 1000, 100),
                                                                    abs=1e-4)


def test_an_unpriced_model_blocks_and_names_the_platform_fix():
    est = estimate_advisor(model="no-such-model-9", signals=10, history=[(100, 10)], at=AT)
    assert est["low"] is None and "unpriced" in est["blocked"] and "pricing.py" in est["blocked"]


def test_spend_is_summed_from_the_idea_runs_only(tmp_path):
    ledger = _ledger_with_calls(tmp_path, [_call(cost_usd=0.25), _call(cost_usd=0.5)])
    save_run(ledger, "k2", "other", "v1", 2)
    assert spent_for_idea(ledger, "i1") == pytest.approx(0.75)
    assert spent_for_idea(ledger, "other") == 0.0


# --- the approval ---------------------------------------------------------------


@pytest.fixture
def approvals(tmp_path):
    book = IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")
    idea = book.add("x", source="discretionary")
    book._save([_with_status(book, "testing")])
    ready_for_testing(book.ideas_dir, idea.id)
    return ApprovalBook(tmp_path / "approvals.json", book), idea.id


def _with_status(book, status):
    (i,) = book._load()
    i.status = status
    return i


def test_an_unpriced_or_blocked_estimate_cannot_be_approved(approvals):
    book, idea = approvals
    a = book.park(idea, "advisor", "use the advisor?", "yes", estimate=estimate_advisor(
        model="no-such-model-9", signals=10, history=[(100, 10)], at=AT))
    with pytest.raises(ValueError, match="cannot be approved on a number"):
        book.answer(a.id, "approved")
    assert book.get(a.id).status == "pending"
    assert book.answer(a.id, "denied").status == "denied"
    assert book.ideas.get(idea).status == "testing"                  # the idea carries on without it


def test_a_priced_estimate_can_be_approved(approvals):
    book, idea = approvals
    est = estimate_advisor(model=MODEL, signals=10, history=[(1000, 100)], at=AT)
    a = book.park(idea, "advisor", "use the advisor?", "yes", estimate=est)
    assert book.answer(a.id, "approved").status == "approved"


def _approved(book, idea, model=MODEL, high=1.0):
    a = book.park(idea, "advisor", "q", "r", estimate={"model": model, "high": high, "low": 0.1,
                                                        "blocked": ""})
    return book.answer(a.id, "approved")


def test_a_run_needs_an_approved_advisor_approval_for_its_model(approvals):
    book, idea = approvals
    with pytest.raises(AdvisorRefused, match="needs --advisor-approval"):
        check_advisor_run(None, idea=idea, model=MODEL, spent=0)
    a = _approved(book, idea)
    check_advisor_run(a, idea=idea, model=MODEL, spent=0.2)           # fine
    with pytest.raises(AdvisorRefused, match="different model"):
        check_advisor_run(a, idea=idea, model="other-model", spent=0)
    with pytest.raises(AdvisorRefused, match="budget is used"):
        check_advisor_run(a, idea=idea, model=MODEL, spent=1.0)
    with pytest.raises(AdvisorRefused, match="not zzz"):
        check_advisor_run(a, idea="zzz", model=MODEL, spent=0)


def test_a_pending_approval_does_not_cover_a_run(approvals):
    book, idea = approvals
    a = book.park(idea, "advisor", "q", "r", estimate={"model": MODEL, "high": 1.0})
    with pytest.raises(AdvisorRefused, match="pending"):
        check_advisor_run(a, idea=idea, model=MODEL, spent=0)


def test_estimates_are_strict_json():
    est = estimate_advisor(model=MODEL, signals=5, history=[(100, 10)], at=AT)
    assert json.loads(json.dumps(est, allow_nan=False)) == est
