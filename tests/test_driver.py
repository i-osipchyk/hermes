"""Phase 4 steps 1–2 (docs/phase-4-roles-and-pipeline-plan.md §1, acceptance 1, 2, 3, 9, 10).

Seam: ``Driver.run`` with a ``ScriptedRunner`` — no agent is ever started. The scripted roles
write through the real ``ArtifactStore`` and ``IdeaBook`` guards."""

import json

import pytest

from hermes.research.artifacts import Spec
from hermes.research.driver import EXIT_PAUSED
from hermes.research.roles import RunResult
from tests.pipeline_helpers import Project


@pytest.fixture
def proj(tmp_path):
    return Project(tmp_path)


def test_a_raw_idea_is_driven_to_validated_with_every_artifact_valid(proj):
    idea = proj.idea()

    summary = proj.driver.run(idea)

    assert summary.stopped == "owner" and "approve_forward_test" in summary.detail
    assert proj.book.get(idea).status == "validated"
    for name in ("spec", "plan", "validation", "lessons"):
        assert proj.store.read(idea, name) is not None, name
    assert proj.unlocked == [f"{idea}-v1"]
    assert proj.history(idea) == [("raw", "quantified", "driver"), ("quantified", "coded", "driver"),
                                  ("coded", "testing", "driver"), ("testing", "validated", "driver")]
    assert summary.exit_code == 0


def test_the_run_log_is_the_audit_trail(proj):
    idea = proj.idea()
    proj.driver.run(idea)
    rows = proj.run_log(idea)
    assert [r["step"] for r in rows if r["role"] != "driver"] == [
        "write_spec", "write_plan", "review_plan", "write_strategy", "run_variants", "validate",
        "reflect"]
    assert all({"at", "step", "role", "attempt", "outcome", "duration_s"} <= r.keys()
               for r in rows)
    assert {r["outcome"] for r in rows} == {"verified", "done"}
    assert rows[0]["session_id"] == "scripted"


def test_a_crash_resumes_without_repeating_what_finished(tmp_path):
    clean = Project(tmp_path / "clean")
    clean_idea = clean.idea()
    clean.driver.run(clean_idea)

    proj = Project(tmp_path / "crash")
    idea = proj.idea()
    scripts = proj.runner.scripts
    boom = scripts[("quant", "write_plan")]

    def crash(i, p):
        raise RuntimeError("the machine went away")

    scripts[("quant", "write_plan")] = crash
    with pytest.raises(RuntimeError):
        proj.driver.run(idea)
    assert proj.store.read(idea, "spec") is not None and proj.store.read(idea, "plan") is None

    scripts[("quant", "write_plan")] = boom
    proj.runner.calls.clear()
    summary = proj.driver.run(idea)

    assert "write_spec" not in [c["step"] for c in proj.runner.calls]     # nothing repeated
    assert summary.stopped == "owner" and proj.book.get(idea).status == "validated"
    assert [h["to"] for h in proj.book.get(idea).history] == [
        h["to"] for h in clean.book.get(clean_idea).history]


def test_a_clean_exit_that_wrote_an_invalid_spec_is_retried_then_parked(proj):
    idea = proj.idea()
    proj.runner.scripts[("researcher", "write_spec")] = lambda i, p: proj.store.write(
        i, "spec", Spec(claim="only a claim"))

    summary = proj.driver.run(idea)

    calls = [c for c in proj.runner.calls if c["step"] == "write_spec"]
    assert len(calls) == 3                                            # 1 try + 2 retries
    assert "entry is missing" in calls[1]["prompt"] and "falsifier is missing" in calls[2]["prompt"]
    assert summary.stopped == "stalled" and summary.exit_code == 2
    approval = proj.approvals.get(summary.approval)
    assert approval.kind == "other" and "driver_stalled" in approval.question
    assert proj.book.get(idea).status == "awaiting_approval"
    assert proj.book.get(idea).resume_status == "raw"


def test_a_runner_that_reports_failure_is_retried_too(proj):
    idea = proj.idea()
    attempts = []

    def flaky(i, p):
        attempts.append(1)
        if len(attempts) < 2:
            return RunResult(ok=False, error="tool crashed")
        proj.store.write(i, "spec", __import__("tests.artifact_helpers",
                                                fromlist=["good_spec"]).good_spec())

    proj.runner.scripts[("researcher", "write_spec")] = flaky
    proj.driver.run(idea, max_steps=2)
    assert len(attempts) == 2 and proj.store.read(idea, "spec") is not None


# --- limits ----------------------------------------------------------------------


def test_max_steps_stops_resumably(proj):
    idea = proj.idea()
    first = proj.driver.run(idea, max_steps=3)
    assert first.stopped == "max_steps" and first.exit_code == 0 and len(first.steps) == 3
    second = proj.driver.run(idea)
    assert second.stopped == "owner" and proj.book.get(idea).status == "validated"


def test_step_runs_exactly_one(proj):
    idea = proj.idea()
    summary = proj.driver.step(idea)
    assert len(summary.steps) == 1 and summary.steps[0]["step"] == "write_spec"


def test_until_stops_before_the_named_step(proj):
    idea = proj.idea()
    summary = proj.driver.run(idea, until="run_variants")
    assert summary.stopped == "until" and proj.book.get(idea).status == "testing"
    assert not proj.ledger.entries()


def test_the_token_limit_pauses_and_a_later_run_picks_up(proj):
    idea = proj.idea()
    good = proj.runner.scripts[("quant", "write_plan")]
    proj.runner.scripts[("quant", "write_plan")] = lambda i, p: RunResult(
        ok=False, paused=True, reset_at="3pm", error="usage limit reached")

    summary = proj.driver.run(idea)

    assert summary.stopped == "paused" and summary.exit_code == EXIT_PAUSED
    assert "3pm" in summary.detail
    assert proj.run_log(idea)[-1]["outcome"] == "paused"
    assert proj.store.read(idea, "spec") is not None                  # finished work is intact
    proj.runner.scripts[("quant", "write_plan")] = good
    assert proj.driver.run(idea).stopped == "owner"


def test_a_dry_run_runs_no_role_and_changes_nothing(proj):
    idea = proj.idea()
    summary = proj.driver.run(idea, dry_run=True)
    assert summary.stopped == "dry_run" and proj.runner.calls == []
    assert [s["step"] for s in summary.steps] == ["write_spec"]
    assert proj.book.get(idea).status == "raw" and proj.run_log(idea) == []

    proj.driver.run(idea, max_steps=1)                                # spec written
    assert [s["step"] for s in proj.driver.run(idea, dry_run=True).steps] == ["check_data"]
    assert proj.book.get(idea).status == "raw"                        # still nothing changed


# --- approvals do not block, and answering resumes ----------------------------------


def test_a_pending_approval_stops_the_driver_naming_it_and_answering_resumes(proj):
    idea = proj.idea()
    proj.driver.run(idea, max_steps=2)
    approval = proj.approvals.park(idea, "instrument_change", "trade EURUSD instead?",
                                   "yes, the spec says so")

    summary = proj.driver.run(idea)

    assert summary.stopped == "parked" and summary.exit_code == 0
    assert summary.approval == approval.id and "EURUSD" in summary.detail
    assert proj.runner.calls[-1]["step"] != "write_plan"

    proj.approvals.answer(approval.id, "approved", "ok")
    assert proj.driver.run(idea).stopped == "owner"
    assert proj.book.get(idea).status == "validated"


def test_the_prompt_carries_the_step_and_the_context_pack(proj):
    idea = proj.idea()
    proj.driver.run(idea, max_steps=1)
    prompt = proj.runner.calls[0]["prompt"]
    assert "write_spec" in prompt and "## idea" in prompt and idea in prompt


def test_the_run_log_is_strict_json(proj):
    idea = proj.idea()
    proj.driver.run(idea, max_steps=2)
    for line in (proj.cfg.ideas_dir / idea / "run.jsonl").read_text().splitlines():
        json.loads(line, parse_constant=lambda c: pytest.fail(c))


def test_a_missing_source_link_is_asked_for_not_left_to_stall_the_transition(proj):
    idea = proj.book.add("index momentum", source="book").id
    calls = []
    proj.runner.scripts[("researcher", "add_source")] = lambda i, p: (
        calls.append(p), proj.book.update(i, links=["https://ssrn.com/x"]))

    summary = proj.driver.run(idea, until="write_plan")

    assert summary.stopped == "until" and len(calls) == 1
    assert proj.book.get(idea).status == "quantified"


def test_a_role_that_does_not_record_a_link_is_retried_then_parked(proj):
    idea = proj.book.add("index momentum", source="book").id
    proj.runner.scripts[("researcher", "add_source")] = lambda i, p: None

    summary = proj.driver.run(idea)

    assert summary.stopped == "stalled" and "link" in summary.detail


# --- the data check between the spec and `quantified` -------------------------------


def test_the_data_check_runs_between_the_spec_and_quantified_and_is_recorded(proj):
    idea = proj.idea()
    summary = proj.driver.run(idea, until="write_plan")
    assert summary.stopped == "until"
    assert [r["step"] for r in proj.run_log(idea) if r["role"] == "driver"] == [
        "check_data", "mark_quantified"]
    saved = json.loads((proj.cfg.ideas_dir / idea / "data_check.json").read_text())
    assert saved["verdict"] == "ok" and saved["rows"][0]["state"] == "cached"


def test_missing_credentials_park_an_owner_approval_and_answering_rechecks(proj):
    idea = proj.idea()
    proj.data_verdict = "owner_action"

    summary = proj.driver.run(idea)

    assert summary.stopped == "parked" and summary.approval
    approval = proj.approvals.get(summary.approval)
    assert approval.kind == "other" and "needs_credentials" in approval.question
    assert proj.book.get(idea).status == "awaiting_approval"

    proj.data_verdict = "ok"                                  # the owner fixed the environment
    proj.approvals.answer(summary.approval, "approved", "credentials set")
    assert proj.book.get(idea).status == "raw"
    assert proj.driver.run(idea, until="write_plan").stopped == "until"
    assert proj.book.get(idea).status == "quantified"


def test_a_still_missing_credential_parks_again_instead_of_slipping_through(proj):
    idea = proj.idea()
    proj.data_verdict = "owner_action"
    first = proj.driver.run(idea)
    proj.approvals.answer(first.approval, "approved", "i think i set them")

    second = proj.driver.run(idea)

    assert second.stopped == "parked" and second.approval != first.approval


def test_missing_capability_sends_the_idea_to_the_developer_with_the_gap_named(proj):
    idea = proj.idea()
    proj.data_verdict = "needs_infra"

    proj.driver.run(idea, max_steps=2)

    got = proj.book.get(idea)
    assert got.status == "needs_infra"
    assert got.failure["kind"] == "infra" and "unavailable" in got.failure["detail"]
    assert proj.driver.next(idea).role == "developer"
