"""Phase 3 step 2 (docs/phase-3-memory-and-state-plan.md): dedupe without an LLM.

Seams: ``IdeaBook.similar(text, k)``, ``IdeaBook.add`` (records near matches, refuses a
near-duplicate of a rejected idea unless ``parent`` is given) and ``hermes ideas add``.
Thresholds were read off the real backlog, where the closest sibling pair scores 0.28."""

from __future__ import annotations

import json

import pytest

from hermes.cli import EXIT_ERR, EXIT_OK, main
from hermes.research.ideas import SIMILAR_REFUSE, SIMILAR_WARN, DuplicateIdea, IdeaBook

FAIL = {"kind": "no_edge", "assumption": "momentum persists"}
TEXT = "Gold breakout of the London range at the New York open"


@pytest.fixture
def book(tmp_path):
    return IdeaBook(tmp_path / "ideas.json", holdout_dir=tmp_path / "ideas")


def _rejected(book, title=TEXT, **kw):
    idea = book.add(title, **kw)
    book.update(idea.id, status="rejected", verdict="no edge", failure=FAIL)
    return idea


def test_thresholds_are_ordered():
    assert 0 < SIMILAR_WARN < SIMILAR_REFUSE <= 1


def test_similar_ranks_by_token_overlap_and_ignores_case_and_stopwords(book):
    same = book.add(TEXT)
    close = book.add("London range breakout on gold", hypothesis="NY open continuation")
    book.add("Bond carry in emerging markets")

    ranked = book.similar("the GOLD breakout of a London range at New York open")

    assert [i.id for i, _ in ranked][:2] == [same.id, close.id]
    assert ranked[0][1] == 1.0 and 0 < ranked[1][1] < 1.0
    assert all(score > 0 for _, score in ranked)       # unrelated ideas are not listed


def test_similar_respects_k_and_can_exclude_an_idea(book):
    ideas = [book.add(f"{TEXT} variant {n}") for n in range(4)]
    assert len(book.similar(TEXT, k=2)) == 2
    assert ideas[0].id not in [i.id for i, _ in book.similar(TEXT, exclude=ideas[0].id)]


def test_tags_count_towards_similarity(book):
    book.add("Opening drive", tags=["gold", "london"])
    assert book.similar("gold london")[0][1] == 0.5      # 2 shared of 4 distinct tokens


def test_a_near_match_is_recorded_not_refused(book):
    first = book.add(TEXT)                              # raw, not rejected
    second = book.add("Gold breakout of the London range at the NY open")
    assert first.id in second.similar_to


def test_an_unrelated_idea_records_no_matches(book):
    book.add(TEXT)
    assert book.add("Bond carry in emerging markets").similar_to == []


def test_a_near_duplicate_of_a_rejected_idea_is_refused(book):
    old = _rejected(book)
    with pytest.raises(DuplicateIdea, match=old.id):
        book.add(TEXT)
    assert len(book.all()) == 1                         # nothing was written


def test_naming_a_parent_lets_a_rejected_idea_be_revisited(book):
    old = _rejected(book)
    new = book.add(TEXT, hypothesis="with a volatility filter", parent=old.id)
    assert new.parent == old.id and old.id in new.similar_to


def test_a_parent_must_exist(book):
    with pytest.raises(KeyError):
        book.add("anything", parent="nope")


def test_a_weak_match_with_a_rejected_idea_is_only_a_warning(book):
    old = _rejected(book)
    idea = book.add("Gold London range", hypothesis="fade the first hour")
    assert idea.similar_to in ([old.id], [])           # allowed either way, never refused


# --- the CLI -------------------------------------------------------------------


@pytest.fixture
def argv(tmp_path):
    return ["--ideas-file", str(tmp_path / "ideas.json")]


def _json(capsys):
    return json.loads(capsys.readouterr().out)


def test_ideas_add_prints_the_matches_it_found(argv, capsys):
    assert main(["ideas", "add", TEXT, *argv, "--json"]) == EXIT_OK
    first = _json(capsys)["idea"]["id"]

    assert main(["ideas", "add", "Gold breakout of the London range at the NY open",
                 *argv, "--json"]) == EXIT_OK
    d = _json(capsys)

    assert [m["id"] for m in d["similar"]] == [first]
    assert d["similar"][0]["score"] >= SIMILAR_WARN
    assert d["idea"]["similar_to"] == [first]


def test_ideas_add_refuses_a_rejected_duplicate_unless_a_parent_is_named(argv, capsys):
    main(["ideas", "add", TEXT, *argv, "--json"])
    old = _json(capsys)["idea"]["id"]
    main(["ideas", "set", old, "--status", "rejected", "--verdict", "no",
          "--failure-kind", "no_edge", "--failure-assumption", "it drifts", *argv, "--json"])
    capsys.readouterr()

    assert main(["ideas", "add", TEXT, *argv, "--json"]) == EXIT_ERR
    assert old in capsys.readouterr().err

    assert main(["ideas", "add", TEXT, "--parent", old, *argv, "--json"]) == EXIT_OK
    assert _json(capsys)["idea"]["parent"] == old
