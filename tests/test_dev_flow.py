"""Phase 4 step 7 (acceptance 8): a missing capability becomes a reviewed branch.

Seam: ``DevFlow`` against a real temporary git repo (no network, no push)."""

import subprocess

import pytest

from hermes.research import dev_flow
from hermes.research.approvals import ApprovalBook
from hermes.research.dev_flow import DevFlow, ExtensionLimit, GateFailed, classify, read_extensions
from hermes.research.ideas import IdeaBook
from tests.artifact_helpers import ready_for_testing

PASS = [["python3", "-c", "print('1 passed')"], ["python3", "-c", "print('All checks passed!')"]]
FAIL = [["python3", "-c", "import sys; print('1 failed'); sys.exit(1)"]]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src/hermes/data").mkdir(parents=True)
    (repo / "src/hermes/backtest").mkdir(parents=True)
    (repo / "src/hermes/data/a.py").write_text("A = 1\n")
    (repo / "src/hermes/backtest/engine.py").write_text("E = 1\n")
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "t@example.org")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")

    ideas_dir = tmp_path / "ideas"
    book = IdeaBook(tmp_path / "ideas.json", holdout_dir=ideas_dir)
    idea = book.add("needs ticks", source="discretionary").id
    (i,) = book._load()
    i.status = "needs_infra"
    i.failure = {"kind": "infra", "assumption": "", "detail": "needs tick data"}
    book._save([i])
    ready_for_testing(ideas_dir, idea)
    flow = DevFlow(repo, ideas_dir, gates=PASS)
    return flow, repo, ApprovalBook(tmp_path / "approvals.json", book), idea, book


def build(flow, repo, idea, *, path="src/hermes/data/b.py", adds="a tick source"):
    ext = flow.start(idea)
    tree = ext.worktree
    (__import__("pathlib").Path(tree) / path).write_text("B = 1\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "dev")
    flow.record(idea, adds=adds, tests_added=3)
    return ext


def test_the_developer_works_in_a_worktree_not_the_working_tree(env):
    flow, repo, approvals, idea, _ = env
    ext = flow.start(idea)
    assert ext.branch == f"dev/{idea}-1" and ext.worktree != str(repo)
    assert f"dev/{idea}-1" in git(repo, "branch", "--list")
    assert "b.py" not in git(repo, "ls-files")
    assert flow.start(idea).worktree == ext.worktree            # a crashed build resumes


def test_an_additive_diff_is_a_dev_merge_and_parks_with_the_evidence(env):
    flow, repo, approvals, idea, book = env
    build(flow, repo, idea)

    approval = flow.submit(idea, approvals)

    assert approval.kind == "dev_merge" and approval.branch == f"dev/{idea}-1"
    assert "b.py" in approval.evidence[0] and "1 passed" in approval.evidence[1]
    assert book.get(idea).status == "awaiting_approval"
    assert read_extensions(flow.ideas_dir, idea)[0]["files"] == ["src/hermes/data/b.py"]


def test_a_diff_touching_an_engine_path_is_engine_semantics_whatever_the_manifest_says(env):
    flow, repo, approvals, idea, _ = env
    build(flow, repo, idea, path="src/hermes/backtest/engine.py",
          adds="purely additive, honestly")
    assert flow.submit(idea, approvals).kind == "engine_semantics"


def test_classification_is_mechanical():
    assert classify(["src/hermes/data/x.py", "tests/test_x.py"]) == "dev_merge"
    for path in ("src/hermes/execution/fills.py", "src/hermes/core/bar.py",
                 "src/hermes/indicators/sma.py", "src/hermes/research/holdout.py",
                 "src/hermes/research/ideas.py"):
        assert classify(["tests/x.py", path]) == "engine_semantics", path


def test_a_failing_gate_at_submit_parks_nothing(env):
    flow, repo, approvals, idea, book = env
    flow.gates = FAIL
    build(flow, repo, idea)
    with pytest.raises(GateFailed, match="1 failed"):
        flow.submit(idea, approvals)
    assert book.get(idea).status == "needs_infra" and approvals.all() == []


def test_a_branch_with_no_record_or_no_change_is_not_submitted(env):
    flow, repo, approvals, idea, _ = env
    flow.start(idea)
    with pytest.raises(ValueError, match="no record of what it adds"):
        flow.submit(idea, approvals)
    flow.record(idea, adds="nothing")
    with pytest.raises(ValueError, match="no changes"):
        flow.submit(idea, approvals)


def test_approving_reruns_the_gates_and_merges_locally(env):
    flow, repo, approvals, idea, _ = env
    build(flow, repo, idea)
    approval = flow.submit(idea, approvals)

    out = flow.merge(idea, approval.branch)

    assert "1 passed" in out
    assert (repo / "src/hermes/data/b.py").exists()
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "master"
    assert read_extensions(flow.ideas_dir, idea)[0]["status"] == "merged"


def test_a_gate_that_fails_at_merge_time_refuses_the_merge(env):
    flow, repo, approvals, idea, _ = env
    build(flow, repo, idea)
    approval = flow.submit(idea, approvals)
    flow.gates = FAIL

    with pytest.raises(GateFailed):
        flow.merge(idea, approval.branch)

    assert not (repo / "src/hermes/data/b.py").exists()
    assert read_extensions(flow.ideas_dir, idea)[0]["status"] == "submitted"


def test_nothing_in_the_flow_ever_pushes(env, monkeypatch):
    flow, repo, approvals, idea, _ = env
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        seen.append(list(cmd))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(dev_flow.subprocess, "run", spy)
    build(flow, repo, idea)
    flow.merge(idea, flow.submit(idea, approvals).branch)
    assert seen and not [c for c in seen if "push" in c or "remote" in c]


def test_the_fourth_extension_is_refused(env):
    flow, repo, approvals, idea, _ = env
    for _ in range(3):
        flow.start(idea)
        items = read_extensions(flow.ideas_dir, idea)
        items[-1]["status"] = "merged"
        dev_flow._write(flow.ideas_dir, idea, items)
    with pytest.raises(ExtensionLimit, match="3 extensions"):
        flow.start(idea)


def test_merging_from_the_wrong_branch_is_refused(env):
    flow, repo, approvals, idea, _ = env
    build(flow, repo, idea)
    approval = flow.submit(idea, approvals)
    git(repo, "checkout", "-q", "-b", "elsewhere")
    with pytest.raises(RuntimeError, match="merge from master"):
        flow.merge(idea, approval.branch)
