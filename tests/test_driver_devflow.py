"""Phase 4 step 7 through the driver: needs_infra → worktree → parked merge approval → resume."""

import pytest

from hermes.research.dev_flow import ExtensionLimit, read_extensions
from tests.pipeline_helpers import Project
from tests.test_dev_flow import build, env  # noqa: F401  (fixture reuse)


@pytest.fixture
def setup(tmp_path, env):  # noqa: F811
    flow, repo, approvals, idea, book = env
    proj = Project(tmp_path / "p", devflow=flow)
    # the helper env made the idea, approvals and extension trail: drive those
    proj.driver.book, proj.driver.approvals = book, approvals
    proj.cfg.ideas_dir = proj.driver.store.root = flow.ideas_dir
    return proj, flow, repo, approvals, idea, book


def test_the_developer_step_parks_a_merge_approval_and_the_driver_stops(setup):
    proj, flow, repo, approvals, idea, book = setup

    def developer(i, prompt):
        ext = read_extensions(flow.ideas_dir, i)[-1]
        assert ext["worktree"] in prompt                  # told where to work
        from pathlib import Path
        (Path(ext["worktree"]) / "src/hermes/data/c.py").write_text("C = 1\n")
        import subprocess
        subprocess.run(["git", "add", "-A"], cwd=ext["worktree"], check=True)
        subprocess.run(["git", "commit", "-qm", "dev"], cwd=ext["worktree"], check=True)
        flow.record(i, adds="a tick source", tests_added=2)

    proj.runner.scripts[("developer", "build_infra")] = developer

    summary = proj.driver.run(idea)

    assert summary.stopped == "parked" and "dev_merge" in summary.detail
    assert proj.runner.calls[-1]["cwd"] is not None
    assert book.get(idea).status == "awaiting_approval"


def test_a_developer_that_records_nothing_is_retried_then_parked(setup):
    proj, flow, repo, approvals, idea, book = setup
    proj.runner.scripts[("developer", "build_infra")] = lambda i, p: None

    summary = proj.driver.run(idea)

    assert summary.stopped == "stalled"
    assert len([c for c in proj.runner.calls if c["step"] == "build_infra"]) == 3
    assert "no record of what it adds" in proj.runner.calls[1]["prompt"]


def test_after_a_merge_the_idea_resumes_from_infra(setup):
    proj, flow, repo, approvals, idea, book = setup
    build(flow, repo, idea)
    approval = flow.submit(idea, approvals)
    flow.merge(idea, approval.branch)
    approvals.answer(approval.id, "approved", "merged")

    act = proj.driver.next(idea)

    assert (act.role, act.step) == ("driver", "resume_from_infra")
    proj.driver.step(idea)
    assert book.get(idea).status == "quantified"


def test_the_fourth_extension_parks_the_idea_with_an_infra_failure(setup):
    proj, flow, repo, approvals, idea, book = setup
    items = [{"topic": f"{idea}-{n}", "branch": f"dev/{idea}-{n}", "worktree": "x",
              "status": "merged"} for n in (1, 2, 3)]
    from hermes.research import dev_flow
    dev_flow._write(flow.ideas_dir, idea, items)
    # a merged last extension would resume; make the latest one refused instead
    items[-1]["status"] = "refused"
    dev_flow._write(flow.ideas_dir, idea, items)

    with pytest.raises(ExtensionLimit):
        flow.start(idea)
    summary = proj.driver.run(idea)

    assert summary.stopped == "parked"
    got = book.get(idea)
    assert got.status == "parked" and got.failure["kind"] == "infra"
