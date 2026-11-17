"""Phase 3 step 6: the context pack — everything one iteration needs to know about an idea,
in one deterministic, size-capped document. Seam: ``build_context`` / ``render_context`` and
``hermes pipeline context``."""

from __future__ import annotations

import json

import pytest

from hermes.cli import EXIT_OK, main
from hermes.research.approvals import ApprovalBook
from hermes.research.artifacts import ArtifactStore, Lessons, Validation
from hermes.research.context import build_context, render_context
from hermes.research.ideas import IdeaBook
from hermes.research.ledger import RunLedger
from hermes.research.reads import ReadsLog
from hermes.research.thesis import ensure_template
from hermes.research.trials import TrialRegistry
from tests.artifact_helpers import good_plan, good_spec
from tests.test_trials import _returns, _save, _trades


@pytest.fixture
def lab(tmp_path):
    book = IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")
    store = ArtifactStore(book.ideas_dir)
    ledger = RunLedger(tmp_path / "runs")
    reads = ReadsLog(book.ideas_dir)
    thesis = tmp_path / "thesis.md"
    ensure_template(thesis)
    thesis.write_text(thesis.read_text().replace(
        "## Survived\n", "## Survived\n- gold orb survived the holdout (gold)\n- bonds carry (rates)\n"))

    parent = book.add("parent idea", tags=["gold"])
    book.update(parent.id, status="rejected", verdict="no edge", failure={
        "kind": "no_edge", "assumption": "range breaks continue"})
    store.write(parent.id, "lessons", Lessons(outcome="rejected", would_change_my_mind="L2 data",
                                              surprises=["costs ate it"]), role="researcher")
    idea = book.add("child idea", tags=["gold"], parent=parent.id)
    store.write(idea.id, "spec", good_spec(), role="researcher")
    store.write(idea.id, "plan", good_plan(), role="quant")
    store.approve_plan(idea.id, reviewer="v", role="validator")
    for n, variant in enumerate(("v1", None)):
        key = f"{idea.id}-{n}"
        _save(ledger, key, _returns(n + 1), _trades([n + 1, n + 20]), idea=idea.id, minute=n)
        if variant:
            ledger.attribute(key, idea.id, "trial", variant)
    store.write(idea.id, "validation", Validation(verdict="continue", tier="exploratory"),
                role="validator")
    approvals = ApprovalBook(tmp_path / "approvals.json", book)
    return dict(book=book, store=store, ledger=ledger, reads=reads, thesis_path=thesis,
                approvals=approvals, registry=TrialRegistry(ledger, book, reads), idea=idea,
                parent=parent, tmp=tmp_path)


def _build(lab, idea=None):
    return build_context((idea or lab["idea"]).id, **{
        k: lab[k] for k in ("book", "store", "ledger", "registry", "approvals", "reads",
                            "thesis_path")})


def test_the_pack_has_every_section_the_iteration_needs(lab):
    ctx = _build(lab)
    assert list(ctx) == ["idea", "spec", "plan", "trials", "validation", "holdout", "lessons",
                         "approvals", "thesis"]
    assert ctx["idea"]["title"] == "child idea" and ctx["idea"]["parent"] == lab["parent"].id
    assert ctx["spec"]["falsifier"]
    assert ctx["validation"]["verdict"] == "continue"
    assert ctx["holdout"] == {"state": "none"}


def test_the_plan_is_joined_to_the_runs_that_executed_each_variant(lab):
    variants = {v["id"]: v for v in _build(lab)["plan"]["variants"]}
    assert variants["v1"]["ran"] is True and len(variants["v1"]["runs"]) == 1
    assert variants["v2"]["ran"] is False and variants["v2"]["runs"] == []


def test_trial_clusters_come_with_their_counts_and_where_they_came_from(lab):
    trials = _build(lab)["trials"]
    assert trials["own"] == 2 and trials["n_eff"] >= 2
    assert trials["clusters"] and trials["clusters"][0]["representative"]
    assert trials["inherited_from"] == {}


def test_lessons_of_the_parent_and_of_informing_ideas_are_included(lab):
    other = lab["book"].add("other")
    lab["book"].update(other.id, status="rejected", verdict="v",
                       failure={"kind": "overfit", "assumption": "a"})
    lab["store"].write(other.id, "lessons", Lessons(outcome="overfit"), role="researcher")
    lab["book"].update(lab["idea"].id, informed_by=[other.id])

    lessons = {row["idea"]: row for row in _build(lab)["lessons"]}

    assert set(lessons) == {lab["parent"].id, other.id}
    assert lessons[lab["parent"].id]["would_change_my_mind"] == "L2 data"


def test_the_thesis_excerpt_follows_the_ideas_tags(lab):
    excerpt = _build(lab)["thesis"]
    assert "gold orb" in excerpt and "bonds carry" not in excerpt


def test_open_approvals_for_the_idea_are_listed(lab):
    lab["book"].update(lab["idea"].id, status="coded") if False else None
    idea = lab["book"].add("waiting")
    a = lab["approvals"].park(idea.id, "other", "q?", "r")
    assert [x["id"] for x in _build(lab, idea)["approvals"]] == [a.id]


def test_the_render_is_deterministic_and_capped(lab):
    ctx = _build(lab)
    a, b = render_context(ctx, max_chars=100_000), render_context(_build(lab), max_chars=100_000)
    assert a == b and "child idea" in a
    small = render_context(ctx, max_chars=600)
    assert len(small) <= 600 and "truncated" in small
    assert "child idea" in small          # the most important sections survive the cap


# --- the CLI -------------------------------------------------------------------


def _argv(lab):
    t = lab["tmp"]
    return ["--ideas-file", str(t / "ideas.json"), "--runs-dir", str(t / "runs"),
            "--ideas-dir", str(t / "ideas"), "--approvals-file", str(t / "approvals.json"),
            "--thesis-file", str(lab["thesis_path"])]


def test_hermes_pipeline_context_prints_the_pack(lab, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_IDEAS_DIR", str(lab["tmp"] / "ideas"))
    assert main(["pipeline", "context", lab["idea"].id, *_argv(lab), "--json"]) == EXIT_OK
    d = json.loads(capsys.readouterr().out)
    assert d["idea"]["title"] == "child idea" and "plan" in d


def test_reading_another_ideas_context_is_logged_as_a_read_of_that_idea(lab, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_IDEAS_DIR", str(lab["tmp"] / "ideas"))
    monkeypatch.setenv("HERMES_IDEA", lab["parent"].id)
    main(["pipeline", "context", lab["idea"].id, *_argv(lab), "--json"])
    (read,) = lab["reads"].read(lab["parent"].id)
    assert (read["command"], read["target_idea"], read["kind"]) == (
        "pipeline context", lab["idea"].id, "targeted")


def test_max_chars_applies_to_the_text_output(lab, capsys):
    assert main(["pipeline", "context", lab["idea"].id, *_argv(lab), "--max-chars", "500"]) == EXIT_OK
    assert len(capsys.readouterr().out.rstrip("\n")) <= 500
