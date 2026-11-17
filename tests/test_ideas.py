"""The idea backlog — step 1 of the framework (ADR-0011).

Idea generation is the stage with the least infrastructure, so what matters here is
that nothing silently stalls or is silently lost: provenance survives, status moves
only through known values, and a decided idea stays in the book as a real result.
"""

from __future__ import annotations

import json

import pytest

from hermes.research.ideas import SOURCES, STATUSES, Idea, IdeaBook
from tests.artifact_helpers import ready_for_testing


@pytest.fixture
def book(tmp_path):
    return IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")


FAIL = {"kind": "no_edge", "assumption": "it drifts"}


def _validate(book, idea_id):
    """Walk an idea to `validated` the only way there is: through a passed holdout."""
    ready_for_testing(book.ideas_dir, idea_id)
    for status in ("quantified", "coded", "testing"):
        book.update(idea_id, status=status)
    d = book.holdout_dir / idea_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "holdout.json").write_text('{"state": "done", "passed": true}')
    return book.update(idea_id, status="validated", verdict="survives costs and OOS")


def test_add_records_provenance_and_starts_raw(book):
    i = book.add("ORB on NQ", hypothesis="the first 30m range sets direction",
                 source="book", source_detail="Carver", tags=["futures", "intraday"])
    assert i.status == "raw"
    assert i.source == "book" and i.source_detail == "Carver"
    assert i.tags == ["futures", "intraday"]
    assert i.created_at and i.updated_at
    assert len(i.id) == 8


def test_an_unknown_source_is_rejected(book):
    """Provenance is the basis of the hit rate, so it cannot be free text."""
    with pytest.raises(ValueError, match="unknown source"):
        book.add("x", source="a_dream")


def test_the_backlog_persists_to_disk_as_readable_json(book):
    book.add("one", source="trader")
    book.add("two", source="data")
    assert [i.title for i in book.all()] == ["two", "one"]      # newest first

    raw = json.loads(book.path.read_text())
    assert {r["title"] for r in raw} == {"one", "two"}
    # a human (or a diff) must be able to read it
    assert book.path.read_text().startswith("[\n")


def test_a_fresh_book_is_empty_not_an_error(tmp_path):
    assert IdeaBook(tmp_path / "nope.json").all() == []


def test_update_moves_an_idea_through_the_pipeline(book):
    i = book.add("ema cross", source="trader")
    ready_for_testing(book.ideas_dir, i.id)
    book.update(i.id, status="quantified", hypothesis="fast over slow continues")
    book.update(i.id, status="coded")
    book.update(i.id, status="testing", strategy="ema_crossover")
    d = book.holdout_dir / i.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "holdout.json").write_text('{"state": "done", "passed": true}')
    done = book.update(i.id, status="validated", verdict="survives costs and OOS")

    assert done.status == "validated"
    assert done.strategy == "ema_crossover"
    assert done.verdict == "survives costs and OOS"
    assert done.hypothesis == "fast over slow continues"


def test_run_keys_and_tags_accumulate_rather_than_replace(book):
    """An idea's evidence is additive — a second run must not erase the first."""
    i = book.add("x", source="data")
    book.update(i.id, run_keys=["aaa"], tags=["fx"])
    got = book.update(i.id, run_keys=["bbb"], tags=["carry"])
    assert got.run_keys == ["aaa", "bbb"]
    assert got.tags == ["fx", "carry"]

    again = book.update(i.id, run_keys=["aaa"])      # idempotent
    assert again.run_keys == ["aaa", "bbb"]


def test_an_unknown_status_is_rejected(book):
    i = book.add("x", source="data")
    with pytest.raises(ValueError, match="unknown status"):
        book.update(i.id, status="probably_fine")


def test_lookup_by_id_prefix_and_title(book):
    i = book.add("Opening range breakout", source="book")
    assert book.get(i.id).id == i.id
    assert book.get(i.id[:4]).id == i.id
    assert book.get("Opening range breakout").id == i.id


def test_an_ambiguous_prefix_raises(book, monkeypatch):
    a = book.add("a", source="data")
    b = book.add("b", source="data")
    shared = a.id[:1]
    if not b.id.startswith(shared):
        pytest.skip("random ids did not collide on the first character")
    with pytest.raises(LookupError):
        book.get(shared)


def test_a_missing_idea_raises(book):
    with pytest.raises(KeyError):
        book.get("zzzzzzzz")


def test_remove_deletes_only_the_target(book):
    a = book.add("keep", source="data")
    b = book.add("drop", source="data")
    book.remove(b.id)
    assert [i.id for i in book.all()] == [a.id]


# --- the views that drive the loop -----------------------------------------

def test_filters_narrow_the_backlog(book):
    book.add("a", source="book", tags=["fx"])
    q = book.add("b", source="trader", tags=["eq"])
    ready_for_testing(book.ideas_dir, q.id)
    book.update(q.id, status="quantified")

    assert [i.title for i in book.list(status="raw")] == ["a"]
    assert [i.title for i in book.list(source="trader")] == ["b"]
    assert [i.title for i in book.list(tag="fx")] == ["a"]


def test_open_only_hides_decided_ideas(book):
    book.add("still working", source="data")
    done = book.add("finished", source="data")
    book.update(done.id, status="rejected", verdict="no edge", failure=FAIL)

    assert [i.title for i in book.list(open_only=True)] == ["still working"]
    # but a rejected idea is a real result and stays in the book
    assert len(book.all()) == 2


def test_pipeline_counts_every_status_in_order(book):
    book.add("a", source="data")
    q = book.add("b", source="data")
    _validate(book, q.id)
    p = book.pipeline()
    assert list(p) == list(STATUSES)
    assert p["raw"] == 1 and p["validated"] == 1 and p["testing"] == 0


def test_hit_rate_counts_only_decided_ideas(book):
    """An untested idea says nothing about whether its source is worth your time."""
    for _ in range(3):
        book.add("from a book", source="book")
    good = book.add("worked", source="trader")
    bad = book.add("did not", source="trader")
    _validate(book, good.id)
    book.update(bad.id, status="rejected", verdict="no", failure=FAIL)

    hr = book.hit_rate()
    assert hr["book"]["total"] == 3
    assert hr["book"]["decided"] == 0
    assert hr["book"]["rate"] is None, "undecided ideas must not score their source"
    assert hr["trader"] == {"total": 2, "decided": 2, "validated": 1, "rate": 0.5,
                            "validated_without_holdout": 0}


def test_every_source_and_status_appears_in_the_vocabulary():
    assert "book" in SOURCES and "discretionary" in SOURCES
    assert "validated" in STATUSES and "rejected" in STATUSES
    assert Idea(id="x", title="t").status in STATUSES


def test_an_idea_tolerates_unknown_fields_on_disk(book):
    """Hand-edited or future-version rows must still load."""
    book.path.parent.mkdir(parents=True, exist_ok=True)
    book.path.write_text(json.dumps([
        {"id": "abc", "title": "hand written", "from_the_future": True}
    ]))
    assert book.all()[0].title == "hand written"


def test_a_validation_that_never_saw_a_holdout_is_reported_separately(book):
    """ADR-0012: ideas validated before the holdout existed stay validated, but a hit rate
    that mixes them with earned ones would flatter the source."""
    earned = book.add("earned", source="book")
    _validate(book, earned.id)
    legacy = book.add("legacy", source="book")
    ideas = book._load()
    for i in ideas:
        if i.id == legacy.id:
            i.status = "validated"
    book._save(ideas)

    hr = book.hit_rate()["book"]

    assert hr["validated"] == 2 and hr["validated_without_holdout"] == 1
    assert book.get(earned.id).validation_basis == "holdout"


def test_the_two_backlog_ideas_validated_before_the_holdout_are_labelled():
    import json
    from pathlib import Path

    rows = json.loads(Path("research/ideas.json").read_text())
    validated = [r for r in rows if r["status"] == "validated"]
    assert validated and all(r.get("validation_basis") in ("holdout", "none (pre-holdout)")
                             for r in validated)
