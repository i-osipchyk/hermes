"""Phase 3 step 8: ``hermes pipeline`` — the read/answer side, end to end through the CLI."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from hermes.cli import EXIT_EMPTY, EXIT_ERR, EXIT_OK, main
from tests.artifact_helpers import good_plan, good_spec


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_IDEAS_DIR", str(tmp_path / "ideas"))
    monkeypatch.delenv("HERMES_ROLE", raising=False)
    argv = ["--ideas-file", str(tmp_path / "ideas.json"), "--ideas-dir", str(tmp_path / "ideas"),
            "--runs-dir", str(tmp_path / "runs"), "--approvals-file", str(tmp_path / "ap.json"),
            "--thesis-file", str(tmp_path / "thesis.md")]
    return {"argv": argv, "tmp": tmp_path}


def _run(lab, capsys, *args):
    code = main(["pipeline", *args, *lab["argv"], "--json"])
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def _add(lab, capsys, title="x"):
    main(["ideas", "add", title, "--source", "discretionary", "--ideas-file", lab["argv"][1], "--json"])
    return json.loads(capsys.readouterr().out)["idea"]["id"]


def _file(lab, name, obj):
    path = lab["tmp"] / f"{name}.json"
    path.write_text(json.dumps(asdict(obj)))
    return str(path)


def test_status_shows_the_queue_and_each_ideas_next_step(lab, capsys):
    idea = _add(lab, capsys)
    code, d, _ = _run(lab, capsys, "status")
    assert code == EXIT_OK and d["by_status"]["raw"] == 1
    assert d["open"][0]["idea"] == idea and d["open"][0]["next_action"]["step"] == "write_spec"


def test_write_then_status_advances_the_idea_without_any_agent(lab, capsys):
    idea = _add(lab, capsys)
    assert _run(lab, capsys, "write", idea, "spec", "--file", _file(lab, "s", good_spec()))[0] == EXIT_OK
    assert _run(lab, capsys, "status")[1]["open"][0]["next_action"]["step"] == "check_data"


def test_an_agent_role_cannot_write_the_validators_file(lab, capsys, monkeypatch):
    from hermes.research.artifacts import Validation

    idea = _add(lab, capsys)
    monkeypatch.setenv("HERMES_ROLE", "quant")
    code, _, err = _run(lab, capsys, "write", idea, "validation",
                        "--file", _file(lab, "v", Validation(verdict="continue")))
    assert code == EXIT_ERR and "validator" in err


def test_the_plan_is_approved_by_the_validator_and_then_locked(lab, capsys, monkeypatch):
    idea = _add(lab, capsys)
    _run(lab, capsys, "write", idea, "plan", "--file", _file(lab, "p", good_plan()))
    monkeypatch.setenv("HERMES_ROLE", "quant")
    assert _run(lab, capsys, "approve-plan", idea, "--reviewer", "q")[0] == EXIT_ERR
    monkeypatch.setenv("HERMES_ROLE", "validator")
    code, d, _ = _run(lab, capsys, "approve-plan", idea, "--reviewer", "v1")
    assert code == EXIT_OK and d["locked_hash"]


_EST = {"model": "m", "low": 0.5, "high": 1.0, "blocked": ""}


def _parked(lab, capsys, kind="advisor"):
    from hermes.research.approvals import ApprovalBook
    from hermes.research.ideas import IdeaBook

    idea = _add(lab, capsys)
    book = IdeaBook(lab["argv"][1], holdout_dir=lab["argv"][3])
    ideas = book._load()
    ideas[0].status = "coded"
    book._save(ideas)
    return idea, ApprovalBook(lab["argv"][7], book).park(idea, kind, "Run it?", "yes", estimate=_EST)


def test_approvals_lists_the_decision_and_answer_resumes_the_idea(lab, capsys):
    idea, approval = _parked(lab, capsys)
    code, d, _ = _run(lab, capsys, "approvals", "--pending")
    assert code == EXIT_OK and d["approvals"][0]["question"] == "Run it?"
    code, d, _ = _run(lab, capsys, "answer", approval.id[:4], "approve", "--note", "ok")
    assert code == EXIT_OK and d["idea"]["status"] == "coded"
    assert _run(lab, capsys, "approvals", "--pending")[0] == EXIT_EMPTY


def test_an_agent_cannot_answer_from_the_cli(lab, capsys, monkeypatch):
    _, approval = _parked(lab, capsys)
    monkeypatch.setenv("HERMES_ROLE", "researcher")
    code, _, err = _run(lab, capsys, "answer", approval.id, "approve")
    assert code == EXIT_ERR and "owner" in err
    assert _run(lab, capsys, "approvals", "--pending")[1]["approvals"][0]["status"] == "pending"


def test_deny_with_reject_closes_the_idea(lab, capsys):
    idea, approval = _parked(lab, capsys, "instrument_change")
    code, d, _ = _run(lab, capsys, "answer", approval.id, "deny", "--reject", "--note", "wrong market")
    assert code == EXIT_OK and d["idea"]["status"] == "rejected"


def test_reopen_needs_a_reason_and_works_only_on_a_rejected_idea(lab, capsys):
    idea = _add(lab, capsys)
    code, _, err = _run(lab, capsys, "reopen", idea, "--reason", "new data")
    assert code == EXIT_ERR and "rejected" in err
