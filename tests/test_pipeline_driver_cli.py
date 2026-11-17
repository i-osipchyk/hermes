"""Phase 4 through the CLI: ``hermes pipeline run|step|validate|amend-plan|extension|
estimate-advisor`` and the merge on ``answer``. The agent boundary is replaced by a scripted
runner (``hermes.cli._make_runner``): no agent starts."""

from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from types import SimpleNamespace

import pytest

from hermes import cli
from hermes.cli import EXIT_EMPTY, EXIT_ERR, EXIT_OK, main
from hermes.research.approvals import ApprovalBook
from hermes.research.artifacts import ArtifactStore, Lessons, Validation
from hermes.research.driver import EXIT_PAUSED, ScriptedRunner
from hermes.research.ideas import IdeaBook
from hermes.research.ledger import RunLedger
from hermes.research.roles import RunResult
from tests.artifact_helpers import good_plan, good_spec
from tests.pipeline_helpers import save_run


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_IDEAS_DIR", str(tmp_path / "ideas"))
    monkeypatch.delenv("HERMES_ROLE", raising=False)
    (tmp_path / "strategies").mkdir()
    argv = ["--ideas-file", str(tmp_path / "ideas.json"), "--ideas-dir", str(tmp_path / "ideas"),
            "--runs-dir", str(tmp_path / "runs"), "--approvals-file", str(tmp_path / "ap.json"),
            "--thesis-file", str(tmp_path / "thesis.md"), "--dir", str(tmp_path / "strategies")]
    book = IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")
    return SimpleNamespace(argv=argv, tmp=tmp_path, book=book,
                           store=ArtifactStore(tmp_path / "ideas"),
                           ledger=RunLedger(tmp_path / "runs"),
                           approvals=ApprovalBook(tmp_path / "ap.json", book))


def _pipeline(lab, capsys, *args):
    code = main(["pipeline", *args, *lab.argv, "--json"])
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def _scripted(lab, monkeypatch, overrides=None):
    scripts = {
        ("researcher", "write_spec"): lambda i, p: lab.store.write(i, "spec", good_spec()),
        ("quant", "write_plan"): lambda i, p: lab.store.write(i, "plan", good_plan()),
        ("validator", "review_plan"): lambda i, p: lab.store.approve_plan(i, reviewer="v"),
        ("researcher", "reflect"): lambda i, p: lab.store.write(i, "lessons", Lessons(outcome="x")),
    }
    scripts.update(overrides or {})
    runner = ScriptedRunner(scripts)
    monkeypatch.setattr(cli, "_make_runner", lambda: runner)
    return runner


def test_run_drives_an_idea_until_the_named_step(lab, capsys, monkeypatch):
    idea = lab.book.add("x", source="discretionary").id
    runner = _scripted(lab, monkeypatch)

    code, d, _ = _pipeline(lab, capsys, "run", idea, "--until", "write_strategy")

    assert code == EXIT_OK and d["stopped"] == "until"
    assert [c["step"] for c in runner.calls] == ["write_spec", "write_plan", "review_plan"]
    assert lab.book.get(idea).status == "quantified"


def test_dry_run_changes_nothing(lab, capsys, monkeypatch):
    idea = lab.book.add("x", source="discretionary").id
    runner = _scripted(lab, monkeypatch)
    code, d, _ = _pipeline(lab, capsys, "run", idea, "--dry-run")
    assert code == EXIT_OK and d["stopped"] == "dry_run" and runner.calls == []
    assert d["steps"][0]["step"] == "write_spec"


def test_step_runs_one_step(lab, capsys, monkeypatch):
    idea = lab.book.add("x", source="discretionary").id
    runner = _scripted(lab, monkeypatch)
    _pipeline(lab, capsys, "step", idea)
    assert len(runner.calls) == 1


def test_a_token_limit_exits_with_the_pause_code(lab, capsys, monkeypatch):
    idea = lab.book.add("x", source="discretionary").id
    _scripted(lab, monkeypatch, {("researcher", "write_spec"): lambda i, p: RunResult(
        ok=False, paused=True, reset_at="3pm", error="usage limit")})
    code, d, _ = _pipeline(lab, capsys, "run", idea)
    assert code == EXIT_PAUSED and d["stopped"] == "paused"


def test_an_unknown_idea_is_a_bad_request(lab, capsys, monkeypatch):
    _scripted(lab, monkeypatch)
    code, _, err = _pipeline(lab, capsys, "run", "nope")
    assert code == EXIT_ERR and "nope" in err


# --- validate / amend-plan / extension ------------------------------------------


def _testing_idea(lab, strategy="s1"):
    idea = lab.book.add("x", source="discretionary").id
    (i,) = lab.book._load()
    i.status, i.strategy = "testing", strategy
    lab.book._save([i])
    lab.store.write(idea, "plan", good_plan(), role="quant")
    lab.store.approve_plan(idea, reviewer="v", role="validator")
    save_run(lab.ledger, "k1", idea, "v1", 1)
    return idea


def test_validate_fills_the_computed_fields_and_names_what_is_unmeasured(lab, capsys):
    idea = _testing_idea(lab)
    code, d, _ = _pipeline(lab, capsys, "validate", idea)
    v = d["validation"]
    assert code == EXIT_OK and v["recommendation"] == "continue" and v["candidate_run"] == "k1"
    assert any(m["axis"] == "deflation" for m in v["not_measured"])
    assert lab.store.read(idea, "validation").verdict == ""


def test_the_verdict_cannot_be_looser_than_the_recommendation_through_the_cli(lab, capsys,
                                                                               monkeypatch):
    idea = _testing_idea(lab)
    _pipeline(lab, capsys, "validate", idea)
    path = lab.tmp / "v.json"
    path.write_text(json.dumps({"verdict": "ready_for_unlock", "rationale": "trust me"}))
    monkeypatch.setenv("HERMES_ROLE", "validator")
    code, _, err = _pipeline(lab, capsys, "write", idea, "validation", "--file", str(path))
    assert code == EXIT_ERR and "more permissive" in err


def test_amend_plan_withdraws_the_lock_and_archives_the_verdict(lab, capsys):
    idea = _testing_idea(lab)
    lab.store.write_mechanical(idea, Validation(recommendation="continue"))
    path = lab.tmp / "p.json"
    plan = good_plan(variants=[{"id": "v9", "axis": "horizon", "change": "c", "tests": "t",
                                "decision_rule": "d"}])
    path.write_text(json.dumps(plan.__dict__))
    code, d, _ = _pipeline(lab, capsys, "amend-plan", idea, "--file", str(path))
    assert code == EXIT_OK and d["amendments"] == 1
    assert lab.store.read(idea, "plan").locked_hash is None
    assert lab.store.read(idea, "validation") is None


def test_estimate_advisor_computes_parks_and_blocks_the_unpriced(lab, capsys):
    idea = _testing_idea(lab)
    code, d, _ = _pipeline(lab, capsys, "estimate-advisor", idea, "v1", "--model",
                           "deepseek-v4-flash", "--signals", "40", "--tokens-in", "1000",
                           "--tokens-out", "100")
    if code == EXIT_OK:
        assert d["estimate"]["calls"] == 40 and d["estimate"]["high"] > d["estimate"]["low"] > 0
    code, d, _ = _pipeline(lab, capsys, "estimate-advisor", idea, "v1", "--model", "no-such-9",
                           "--signals", "40", "--tokens-in", "1000", "--tokens-out", "100",
                           "--park")
    assert code == EXIT_EMPTY and "unpriced" in d["estimate"]["blocked"]
    approval = lab.approvals.get(d["approval"])
    assert approval.kind == "advisor" and approval.estimate["blocked"]
    code, _, err = _pipeline(lab, capsys, "answer", approval.id, "approve")
    assert code == EXIT_ERR and "cannot be approved on a number" in err


def test_estimate_without_a_run_or_signals_says_what_to_do(lab, capsys):
    idea = _testing_idea(lab)
    code, _, err = _pipeline(lab, capsys, "estimate-advisor", idea, "v2", "--model", "m")
    assert code == EXIT_ERR and "--signals" in err


# --- the advisor gate on `hermes run` --------------------------------------------


def _gate_args(lab, approval_id=None, idea="i1"):
    return Namespace(idea=idea, advisor_approval=approval_id,
                     approvals_file=str(lab.tmp / "ap.json"),
                     ideas_file=str(lab.tmp / "ideas.json"))


def _bt(model="m"):
    return SimpleNamespace(advisor=SimpleNamespace(provider=SimpleNamespace(model_id=model)))


def test_an_advisor_run_for_an_idea_without_an_approval_is_refused(lab):
    with pytest.raises(SystemExit, match="--advisor-approval"):
        cli._check_advisor_gate(_gate_args(lab), _bt(), lab.ledger)


def test_a_run_with_no_idea_and_no_role_is_not_gated(lab):
    cli._check_advisor_gate(_gate_args(lab, idea=None), _bt(), lab.ledger)
    cli._check_advisor_gate(_gate_args(lab), SimpleNamespace(advisor=None), lab.ledger)


def test_an_approved_covering_approval_lets_the_run_through(lab):
    idea = _testing_idea(lab)
    a = lab.approvals.park(idea, "advisor", "q", "r", estimate={"model": "m", "high": 1.0,
                                                               "low": 0.1, "blocked": ""})
    lab.approvals.answer(a.id, "approved")
    cli._check_advisor_gate(_gate_args(lab, a.id, idea), _bt("m"), lab.ledger)
    with pytest.raises(SystemExit, match="different model"):
        cli._check_advisor_gate(_gate_args(lab, a.id, idea), _bt("other"), lab.ledger)


def test_spending_the_estimate_stops_the_run_and_parks_a_new_approval(lab):
    from datetime import UTC, datetime

    from hermes.ai.observability import LLMCallRecord, LLMObservabilityLog

    idea = _testing_idea(lab)
    a = lab.approvals.park(idea, "advisor", "q", "r", estimate={"model": "m", "high": 1.0,
                                                               "low": 0.1, "blocked": ""})
    lab.approvals.answer(a.id, "approved")
    call = LLMCallRecord(request_time=datetime.now(UTC), model_id="m", from_decision_cache=False,
                         input_tokens=1, output_tokens=1, cache_read_tokens=None,
                         cache_creation_tokens=None, latency_ms=None, cost_usd=1.5, approved=True,
                         confidence=0.5, reason="r", system_prompt="", user_prompt="")
    rec = lab.ledger.load("k1")
    lab.ledger.save("k1", rec.result, rec.meta, llm_log=LLMObservabilityLog(calls=[call]))

    with pytest.raises(SystemExit, match="budget is used"):
        cli._check_advisor_gate(_gate_args(lab, a.id, idea), _bt("m"), lab.ledger)

    pending = lab.approvals.all(pending_only=True)
    assert len(pending) == 1 and pending[0].estimate["supersedes"] == a.id
    assert pending[0].estimate["high"] == pytest.approx(2.5)
    assert lab.book.get(idea).status == "awaiting_approval"


# --- merge on approval -----------------------------------------------------------


def test_approving_a_dev_merge_reruns_the_gates_and_merges_or_refuses(lab, capsys, monkeypatch,
                                                                       tmp_path):
    from hermes.research.dev_flow import DevFlow
    from tests.test_dev_flow import FAIL, PASS, build, git

    repo = tmp_path / "repo"
    (repo / "src/hermes/data").mkdir(parents=True)
    (repo / "src/hermes/data/a.py").write_text("A = 1\n")
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "t@example.org")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    idea = lab.book.add("x", source="discretionary").id
    (i,) = lab.book._load()
    i.status, i.failure = "needs_infra", {"kind": "infra", "assumption": "", "detail": "ticks"}
    lab.book._save([i])
    flow = DevFlow(repo, lab.tmp / "ideas", gates=PASS)
    build(flow, repo, idea)
    approval = flow.submit(idea, lab.approvals)

    monkeypatch.setattr(cli, "_devflow", lambda args: DevFlow(repo, lab.tmp / "ideas", gates=FAIL))
    code, _, err = _pipeline(lab, capsys, "answer", approval.id, "approve")
    assert code == EXIT_ERR and "not merged" in err and "1 failed" in err
    assert lab.approvals.get(approval.id).status == "pending"
    assert not (repo / "src/hermes/data/b.py").exists()

    monkeypatch.setattr(cli, "_devflow", lambda args: DevFlow(repo, lab.tmp / "ideas", gates=PASS))
    code, d, _ = _pipeline(lab, capsys, "answer", approval.id, "approve")
    assert code == EXIT_OK and "1 passed" in d["merge_output"]
    assert (repo / "src/hermes/data/b.py").exists()
    assert lab.book.get(idea).status == "needs_infra"
    assert subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True,
                          text=True).stdout.count("Merge") == 1


def test_extension_records_what_the_branch_adds(lab, capsys, monkeypatch, tmp_path):
    from hermes.research.dev_flow import DevFlow, read_extensions

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@e.org", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "base"], cwd=repo, check=True)
    idea = lab.book.add("x", source="discretionary").id
    flow = DevFlow(repo, lab.tmp / "ideas")
    monkeypatch.setattr(cli, "_devflow", lambda args: flow)
    flow.start(idea)
    code, _, _ = _pipeline(lab, capsys, "extension", idea, "--adds", "a tick source", "--tests",
                           "4")
    assert code == EXIT_OK
    assert read_extensions(lab.tmp / "ideas", idea)[0]["adds"] == "a tick source"
