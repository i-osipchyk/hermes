"""Phase 3 step 4 (docs/phase-3-memory-and-state-plan.md, ADR-0013): the reads log and the
audit of ``informed_by``. What an idea *read* of another idea's results counts against it,
whether or not it declared so.

Seams: ``ReadsLog``, ``audit_informed_by``, ``TrialRegistry(..., reads=)``, and the CLI
commands that log a read when ``HERMES_IDEA`` is set."""

from __future__ import annotations

import json

import pytest

from hermes.cli import EXIT_OK, main
from hermes.research.artifacts import Validation
from hermes.research.ideas import IdeaBook
from hermes.research.ledger import RunLedger
from hermes.research.reads import ReadsLog, audit_informed_by
from hermes.research.trials import TrialRegistry
from tests.test_cli import STRATEGY
from tests.test_trials import _idea_with_trials, _returns, _save, _trades


@pytest.fixture
def log(tmp_path):
    return ReadsLog(tmp_path / "ideas")


@pytest.fixture
def book(tmp_path):
    return IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")


# --- the log -------------------------------------------------------------------


def test_a_read_is_one_strict_json_line_in_the_readers_file(log):
    log.record("a", command="show", target="run1", target_idea="b", role="quant")
    (line,) = (log.root / "a" / "reads.jsonl").read_text().splitlines()
    got = json.loads(line)
    assert (got["command"], got["target"], got["target_idea"], got["role"], got["kind"]) == (
        "show", "run1", "b", "quant", "targeted")
    assert got["at"]


def test_reads_accumulate_in_order_and_are_per_idea(log):
    log.record("a", command="show", target="r1", target_idea="b")
    log.record("a", command="trials", target="c", target_idea="c")
    log.record("z", command="show", target="r9", target_idea="b")
    assert [r["target"] for r in log.read("a")] == ["r1", "c"]
    assert [r["target"] for r in log.read("z")] == ["r9"]
    assert log.read("nobody") == []


def test_the_default_directory_follows_the_environment_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_IDEAS_DIR", str(tmp_path / "elsewhere"))
    ReadsLog().record("a", command="show", target="r", target_idea=None)
    assert (tmp_path / "elsewhere" / "a" / "reads.jsonl").exists()


# --- the audit -----------------------------------------------------------------


def test_the_audit_compares_what_was_declared_with_what_was_read(book, log):
    a, b, c, d = (book.add(t) for t in "abcd")
    book.update(a.id, informed_by=[b.id, c.id])
    log.record(a.id, command="show", target="r", target_idea=b.id)       # declared and read
    log.record(a.id, command="show", target="r", target_idea=d.id)       # read, not declared

    audit = audit_informed_by(book, log, a.id)

    assert audit == {"declared": [b.id, c.id], "read": [b.id, d.id],
                     "read_not_declared": [d.id], "declared_not_read": [c.id]}


def test_listings_own_and_untagged_reads_are_logged_but_never_counted(book, log):
    a, b = book.add("a"), book.add("b")
    log.record(a.id, command="runs", target="*", kind="listing", target_idea=b.id)
    log.record(a.id, command="show", target="mine", target_idea=a.id)
    log.record(a.id, command="show", target="untagged", target_idea=None)
    assert len(log.read(a.id)) == 3
    assert audit_informed_by(book, log, a.id)["read"] == []


# --- effective trials: declared ∪ read ----------------------------------------


def test_effective_trials_include_ideas_that_were_read_but_not_declared(tmp_path, book, log):
    ledger = RunLedger(tmp_path / "runs")
    other = _idea_with_trials(ledger, book, "other", 3, seed0=10)
    mine = _idea_with_trials(ledger, book, "mine", 2, seed0=50)

    assert TrialRegistry(ledger, book, log).effective_trials(mine.id).inherited == {}

    log.record(mine.id, command="show", target="r", target_idea=other.id)
    eff = TrialRegistry(ledger, book, log).effective_trials(mine.id)

    assert eff.own == 2 and eff.inherited == {other.id: 3} and eff.n_eff == 5


def test_declared_and_read_together_count_each_idea_once(tmp_path, book, log):
    ledger = RunLedger(tmp_path / "runs")
    other = _idea_with_trials(ledger, book, "other", 3, seed0=10)
    mine = _idea_with_trials(ledger, book, "mine", 1, seed0=50)
    book.update(mine.id, informed_by=[other.id])
    log.record(mine.id, command="show", target="r", target_idea=other.id)

    eff = TrialRegistry(ledger, book, log).effective_trials(mine.id)

    assert eff.inherited == {other.id: 3}


def test_the_trial_sharpes_include_the_ideas_that_were_read(tmp_path, book, log):
    ledger = RunLedger(tmp_path / "runs")
    other = book.add("other")
    mine = book.add("mine")
    for idea, seed in ((other, 1), (mine, 2)):
        trades = [{**t, "net_pnl": (-1) ** i * (10 + i)}           # measurable: ≥10 trades
                  for i, t in enumerate(_trades(range(0, 100, 5)))]
        _save(ledger, f"{idea.id}-t", _returns(seed), trades, idea=idea.id, minute=seed)
    base = len(TrialRegistry(ledger, book, log).representative_sharpes(mine.id))
    log.record(mine.id, command="show", target="r", target_idea=other.id)
    assert len(TrialRegistry(ledger, book, log).representative_sharpes(mine.id)) == base + 1


def test_an_idea_never_inherits_from_itself(tmp_path, book, log):
    ledger = RunLedger(tmp_path / "runs")
    mine = _idea_with_trials(ledger, book, "mine", 2, seed0=50)
    log.record(mine.id, command="trials", target=mine.id, target_idea=mine.id)
    assert TrialRegistry(ledger, book, log).effective_trials(mine.id).inherited == {}


def test_the_validation_artifact_can_carry_the_flag():
    assert Validation(read_not_declared=["x"]).read_not_declared == ["x"]


# --- the CLI -------------------------------------------------------------------


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_IDEAS_DIR", str(tmp_path / "ideas"))
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "buyhold.py").write_text(STRATEGY)
    return {"common": ["--dir", str(sdir), "--runs-dir", str(tmp_path / "runs")],
            "ideas": ["--ideas-file", str(tmp_path / "ideas.json")], "tmp": tmp_path}


def _reads(lab, idea):
    path = lab["tmp"] / "ideas" / idea / "reads.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _run_for(lab, capsys, idea):
    assert main(["run", "buyhold", *lab["common"], "--idea", idea, "--json",
                 "--note", "t"]) == EXIT_OK
    return json.loads(capsys.readouterr().out)["run_key"]


def test_nothing_is_logged_without_an_idea_in_the_environment(lab, capsys, monkeypatch):
    monkeypatch.delenv("HERMES_IDEA", raising=False)
    key = _run_for(lab, capsys, "b")
    main(["show", key, *lab["common"], "--json"])
    assert not (lab["tmp"] / "ideas").exists()


def test_show_logs_a_targeted_read_of_the_runs_idea(lab, capsys, monkeypatch):
    key = _run_for(lab, capsys, "bbb")
    monkeypatch.setenv("HERMES_IDEA", "aaa")
    monkeypatch.setenv("HERMES_ROLE", "quant")
    assert main(["show", key, *lab["common"], "--json"]) == EXIT_OK
    (read,) = _reads(lab, "aaa")
    assert (read["command"], read["target"], read["target_idea"], read["kind"], read["role"]) == (
        "show", key, "bbb", "targeted", "quant")


def test_analyze_logs_the_read_too(lab, capsys, monkeypatch):
    key = _run_for(lab, capsys, "bbb")
    monkeypatch.setenv("HERMES_IDEA", "aaa")
    main(["analyze", key, *lab["common"], "--axes", "costs", "--json"])
    assert [(r["command"], r["target_idea"]) for r in _reads(lab, "aaa")] == [("analyze", "bbb")]


def test_runs_is_a_listing(lab, capsys, monkeypatch):
    _run_for(lab, capsys, "bbb")
    monkeypatch.setenv("HERMES_IDEA", "aaa")
    main(["runs", *lab["common"], "--json"])
    (read,) = _reads(lab, "aaa")
    assert read["command"] == "runs" and read["kind"] == "listing"


def test_trials_of_another_idea_is_a_targeted_read(lab, capsys, monkeypatch):
    main(["ideas", "add", "other", *lab["ideas"], "--json"])
    other = json.loads(capsys.readouterr().out)["idea"]["id"]
    _run_for(lab, capsys, other)
    monkeypatch.setenv("HERMES_IDEA", "aaa")
    main(["trials", other, *lab["common"], *lab["ideas"], "--out-dir", str(lab["tmp"] / "t"),
          "--json"])
    assert [(r["command"], r["target_idea"], r["kind"]) for r in _reads(lab, "aaa")] == [
        ("trials", other, "targeted")]


def test_reading_another_ideas_run_inflates_the_trial_count_without_a_declaration(
    lab, capsys, monkeypatch
):
    main(["ideas", "add", "other", *lab["ideas"], "--json"])
    other = json.loads(capsys.readouterr().out)["idea"]["id"]
    main(["ideas", "add", "mine", *lab["ideas"], "--json"])
    mine = json.loads(capsys.readouterr().out)["idea"]["id"]
    other_key = _run_for(lab, capsys, other)
    _run_for(lab, capsys, mine)

    monkeypatch.setenv("HERMES_IDEA", mine)
    main(["show", other_key, *lab["common"], "--json"])
    capsys.readouterr()
    main(["trials", mine, *lab["common"], *lab["ideas"], "--out-dir", str(lab["tmp"] / "t"),
          "--json"])
    got = json.loads(capsys.readouterr().out)

    assert got["inherited"] == {other: 1}
