"""Phase 3 step 3 follow-through: a run names the plan variant it executes
(``hermes run --idea <id> --variant <id>``), and ``hermes trials --plan`` shows what is planned
but unrun and what ran unplanned. The variant is intent, outside the cache key."""

from __future__ import annotations

import json

import pytest

from hermes.cli import EXIT_ERR, EXIT_OK, main
from hermes.research.artifacts import ArtifactStore
from hermes.research.ledger import RunLedger
from tests.artifact_helpers import good_plan
from tests.test_cli import STRATEGY


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_IDEAS_DIR", str(tmp_path / "ideas"))
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "buyhold.py").write_text(STRATEGY)
    store = ArtifactStore(tmp_path / "ideas")
    store.write("i1", "plan", good_plan(), role="quant")
    return {"common": ["--dir", str(sdir), "--runs-dir", str(tmp_path / "runs")],
            "ideas": ["--ideas-file", str(tmp_path / "ideas.json")], "tmp": tmp_path}


def _run(lab, *extra):
    return main(["run", "buyhold", *lab["common"], "--idea", "i1", "--json", "--note", "t", *extra])


def test_a_run_records_the_variant_it_executes(lab, capsys):
    assert _run(lab, "--variant", "v1") == EXIT_OK
    (entry,) = RunLedger(lab["tmp"] / "runs").entries()
    assert entry.meta.variant == "v1"


def test_a_variant_that_is_not_in_the_plan_is_refused(lab, capsys):
    assert _run(lab, "--variant", "v9") == EXIT_ERR
    assert "v9" in capsys.readouterr().err
    assert RunLedger(lab["tmp"] / "runs").entries() == []


def test_a_variant_needs_an_idea(lab, capsys):
    code = main(["run", "buyhold", *lab["common"], "--variant", "v1", "--json", "--note", "t"])
    assert code == EXIT_ERR and "--idea" in capsys.readouterr().err


def test_a_cached_run_gains_the_variant_it_is_now_claimed_for(lab, capsys):
    _run(lab)
    capsys.readouterr()
    _run(lab, "--variant", "v2")
    (entry,) = RunLedger(lab["tmp"] / "runs").entries()
    assert (entry.meta.idea, entry.meta.variant) == ("i1", "v2")


def test_trials_plan_lists_unrun_and_unplanned_variants(lab, capsys):
    main(["ideas", "add", "mine", *lab["ideas"], "--json"])
    idea = json.loads(capsys.readouterr().out)["idea"]["id"]
    ArtifactStore(lab["tmp"] / "ideas").write(idea, "plan", good_plan(), role="quant")
    main(["run", "buyhold", *lab["common"], "--idea", idea, "--variant", "v1", "--json",
          "--note", "t"])
    capsys.readouterr()
    assert main(["trials", idea, "--plan", *lab["common"], *lab["ideas"],
                 "--out-dir", str(lab["tmp"] / "ideas"), "--json"]) == EXIT_OK
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert plan["unrun"] == ["v2"] and plan["unplanned_runs"] == []
