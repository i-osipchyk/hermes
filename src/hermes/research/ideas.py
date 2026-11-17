"""The idea backlog — the front of the pipeline.

"Idea generation is the most crucial stage for any quant" (Simons), and it is the stage
with the least infrastructure anywhere: ideas arrive from a book, a discretionary
session, another trader, a course — and then live in someone's head or a notes app until
they are forgotten. A backlog that survives across sessions is what turns a scattering of
hunches into a pipeline.

What this records, and why each field earns its place:

* **provenance** — where it came from. The framework's warning about books ("most of the
  time you'll find they're somewhat worse than what they're stating") applies unevenly by
  source, and a hit rate per source is only computable if provenance is recorded.
* **hypothesis** — the claim, which must survive quantification before it can be tested.
* **status** — where it is in the pipeline, so nothing silently stalls.
* **run keys** — the link into the [[run-ledger]], so an idea's verdict is traceable to
  the runs that produced it.

Deliberately dumb storage: one JSON file, human-editable, in the repo rather than a cache,
because the backlog is *work product* and losing it loses the thinking. It is the one
Hermes artifact that should be committed.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_IDEAS_PATH = Path("research/ideas.json")
DEFAULT_IDEAS_DIR = Path("research/ideas")      # per-idea artifacts: <id>/holdout.json, …

# Where an idea came from. Kept close to the framework's own list so the hit rate per
# source is measurable: books, your own discretionary screen time, other traders and
# courses, or something you noticed in the data.
SOURCES = ("book", "discretionary", "trader", "course", "data", "other")

# The pipeline. An idea moves forward only on evidence, and only along `TRANSITIONS`;
# `parked` is for ideas that are blocked (missing data, needs an instrument Hermes can't
# reach) rather than judged. "Reflected" is deliberately not a status: it is the presence of
# lessons.json, so `validated`/`rejected` stay what `hit_rate` and `is_open` read.
STATUSES = (
    "raw",                    # captured, not yet quantified
    "literature_done",        # sources and a falsifiable claim exist
    "quantified",             # unambiguous rules exist — testable
    "coded",                  # a strategy file exists and runs
    "testing",                # runs exist, verdict not reached
    "needs_infra",            # blocked on a Developer extension
    "awaiting_approval",      # parked on an owner decision (resume_status, approval_id)
    "validated",              # survived the rubric — only via a passed holdout (ADR-0012)
    "ready_to_forward_test",  # validated and the owner approved the suggestion
    "rejected",               # tested and did not survive — keep it, it is a real result
    "parked",                 # blocked on something external, not on evidence
)

# Why an idea failed: a closed vocabulary so the Strategist can group failures.
FAILURE_KINDS = ("no_edge", "cost_killed", "regime_dependent", "overfit", "data_limit",
                 "holdout_failed", "duplicate", "infra", "other")

_ANYWHERE = frozenset({"awaiting_approval", "parked"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "raw": frozenset({"literature_done", "quantified", "rejected"}) | _ANYWHERE,
    "literature_done": frozenset({"quantified", "rejected"}) | _ANYWHERE,
    "quantified": frozenset({"coded", "needs_infra", "rejected"}) | _ANYWHERE,
    "needs_infra": frozenset({"quantified", "coded", "rejected"}) | _ANYWHERE,
    "coded": frozenset({"testing", "needs_infra", "rejected"}) | _ANYWHERE,
    "testing": frozenset({"validated", "rejected", "needs_infra"}) | _ANYWHERE,
    # → resume_status is added per idea
    "awaiting_approval": frozenset({"rejected", "parked"}),
    "validated": frozenset({"ready_to_forward_test"}) | _ANYWHERE,
    "ready_to_forward_test": frozenset(),
    "rejected": frozenset(),                      # terminal; `reopen` is the one way out
    # → statuses the idea had before; this is only the fallback for a legacy record
    "parked": frozenset({"raw", "quantified", "testing"}),
}

# Statuses whose entry is guarded by evidence outside this module.
LINK_FREE_SOURCES = ("discretionary", "data")


class IllegalTransition(ValueError):
    """A status change the pipeline does not allow."""


class DuplicateIdea(ValueError):
    """A new idea that restates one already rejected, with no ``parent`` to say why it is back."""


# Token overlap (Jaccard) over title + hypothesis + tags. Read off the real backlog, where
# the closest sibling pair scores 0.28 and nothing reaches 0.5: 0.25 surfaces siblings
# without noise. 0.6 for refusing is untested on real data (there are no real duplicates
# yet) — a starting value, to be calibrated.
SIMILAR_WARN = 0.25
SIMILAR_REFUSE = 0.6
_STOPWORDS = frozenset(
    "the an of to in on and or for with is are be by at as it its this that from when than then "
    "into over under after before".split())


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", text.lower())
                     if len(w) > 2 and w not in _STOPWORDS)


def _idea_text(idea: Idea) -> str:
    return " ".join([idea.title, idea.hypothesis, *idea.tags])


@dataclass
class Idea:
    """One trading idea and where it has got to."""

    id: str
    title: str
    hypothesis: str = ""
    source: str = "other"
    source_detail: str = ""        # the book, trader, course, or session
    status: str = "raw"
    strategy: str = ""             # strategies/<name>.py once written
    run_keys: list[str] = field(default_factory=list)
    verdict: str = ""              # why it was validated or rejected
    tags: list[str] = field(default_factory=list)
    # Ideas whose results this one actually drew on — each one's trial clusters inherit
    # into this idea's effective trials (ADR-0013). The agent's declaration.
    informed_by: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    # --- phase 3: state the pipeline reads (all additive; old records load unchanged) ---
    schema_version: int = 2
    parent: str | None = None              # the idea this one was spun out of
    failure: dict | None = None            # {kind, assumption, detail}; kind in FAILURE_KINDS
    links: list[str] = field(default_factory=list)       # source URLs
    resume_status: str | None = None       # where an awaiting_approval idea goes back to
    approval_id: str | None = None
    priority: int | None = None            # manual order; lower runs first
    validation_basis: str | None = None    # "holdout" or "none (pre-holdout)"
    history: list[dict] = field(default_factory=list)    # {at, from, to, by, reason}
    similar_to: list[str] = field(default_factory=list)  # near matches found when it was added

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Idea:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def label(self) -> str:
        return f"[{self.status}] {self.title}"

    @property
    def is_open(self) -> bool:
        return self.status not in ("validated", "ready_to_forward_test", "rejected")


def _targets(idea: Idea) -> frozenset[str]:
    """Edges out of ``idea.status``: the static graph plus the dynamic ones (resume, un-park)."""
    out = set(TRANSITIONS[idea.status]) - {idea.status}
    if idea.status == "awaiting_approval" and idea.resume_status:
        out.add(idea.resume_status)
    if idea.status == "parked":
        earlier = {h["from"] for h in idea.history} - {"parked", "awaiting_approval"}
        out = earlier if earlier else out
    if idea.status in ("parked", "ready_to_forward_test"):
        out -= {"awaiting_approval"}
    return frozenset(out)


@dataclass
class IdeaBook:
    """The backlog, persisted as one JSON file."""

    path: Path = DEFAULT_IDEAS_PATH
    # Where each idea's artifacts live; `validated` is checked against <id>/holdout.json here.
    holdout_dir: Path = DEFAULT_IDEAS_DIR

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.holdout_dir = Path(self.holdout_dir)

    # --- storage -----------------------------------------------------------

    def _load(self) -> list[Idea]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text() or "[]")
        return [Idea.from_dict(d) for d in raw]

    def _save(self, ideas: list[Idea]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([i.to_dict() for i in ideas], indent=2))

    # --- reading -----------------------------------------------------------

    def all(self) -> list[Idea]:
        """Newest first."""
        return sorted(self._load(), key=lambda i: i.created_at or "", reverse=True)

    def list(
        self,
        *,
        status: str | None = None,
        source: str | None = None,
        tag: str | None = None,
        open_only: bool = False,
    ) -> list[Idea]:
        out = self.all()
        if status:
            out = [i for i in out if i.status == status]
        if source:
            out = [i for i in out if i.source == source]
        if tag:
            out = [i for i in out if tag in i.tags]
        if open_only:
            out = [i for i in out if i.is_open]
        return out

    def get(self, id_or_prefix: str) -> Idea:
        """By id, a unique id prefix, or an exact title."""
        ideas = self._load()
        for i in ideas:
            if i.id == id_or_prefix or i.title == id_or_prefix:
                return i
        hits = [i for i in ideas if i.id.startswith(id_or_prefix)]
        if not hits:
            raise KeyError(f"no idea matching {id_or_prefix!r}")
        if len(hits) > 1:
            raise LookupError(
                f"{id_or_prefix!r} matches {len(hits)}: {', '.join(i.id for i in hits)}"
            )
        return hits[0]

    def similar(self, text: str, k: int = 5, *, exclude: str | None = None
                ) -> list[tuple[Idea, float]]:
        """The ``k`` ideas closest to ``text`` by token overlap, best first. No LLM: Hermes
        computes, the agent interprets. Ideas with nothing in common are not listed."""
        query = _tokens(text)
        scored = []
        for idea in self._load():
            if idea.id == exclude:
                continue
            other = _tokens(_idea_text(idea))
            union = query | other
            score = len(query & other) / len(union) if union else 0.0
            if score > 0:
                scored.append((idea, round(score, 3)))
        scored.sort(key=lambda p: (-p[1], p[0].created_at or "", p[0].id))
        return scored[:k]

    # --- writing -----------------------------------------------------------

    def add(
        self,
        title: str,
        *,
        hypothesis: str = "",
        source: str = "other",
        source_detail: str = "",
        tags: list[str] | None = None,
        parent: str | None = None,
    ) -> Idea:
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}. Known: {', '.join(SOURCES)}")
        parent_id = self.get(parent).id if parent else None
        text = " ".join([title, hypothesis, *(tags or [])])
        near = [(i, score) for i, score in self.similar(text) if score >= SIMILAR_WARN]
        if parent_id is None:
            for i, score in near:
                if i.status == "rejected" and score >= SIMILAR_REFUSE:
                    raise DuplicateIdea(
                        f"{title!r} restates rejected idea {i.id} ({i.title!r}, similarity "
                        f"{score:.2f}; its verdict: {i.verdict}). If it is a deliberate "
                        f"variant, add it with --parent {i.id}")
        now = datetime.now(UTC).isoformat()
        idea = Idea(
            id=uuid.uuid4().hex[:8],
            title=title,
            hypothesis=hypothesis,
            source=source,
            source_detail=source_detail,
            tags=list(tags or []),
            parent=parent_id,
            similar_to=[i.id for i, _ in near],
            created_at=now,
            updated_at=now,
        )
        ideas = self._load()
        ideas.append(idea)
        self._save(ideas)
        return idea

    def update(self, id_or_prefix: str, *, reason: str = "", **changes: object) -> Idea:
        """Change fields on an idea; ``run_keys``, ``tags``, ``informed_by`` and ``links`` are
        appended, not replaced.

        A status change must be an edge of ``TRANSITIONS`` and satisfy the rules attached to it,
        or ``IllegalTransition`` is raised and nothing is written. There is no force: an agent
        with a shell would use it. Every change is appended to ``history``.
        """
        target = self.get(id_or_prefix)
        new = changes.get("status")
        if new is not None and new not in STATUSES:
            raise ValueError(f"unknown status {new!r}. Known: {', '.join(STATUSES)}")
        failure = changes.get("failure")
        if failure is not None and failure.get("kind") not in FAILURE_KINDS:  # type: ignore[attr-defined]
            raise ValueError(f"unknown failure kind {failure.get('kind')!r}. "  # type: ignore[attr-defined]
                             f"Known: {', '.join(FAILURE_KINDS)}")
        ideas = self._load()
        for idea in ideas:
            if idea.id != target.id:
                continue
            before = idea.status
            for k, v in changes.items():
                if v is None:
                    continue
                if k in ("run_keys", "tags", "informed_by", "links"):
                    existing = getattr(idea, k)
                    for item in (v if isinstance(v, list) else [v]):
                        if item not in existing:
                            existing.append(item)
                elif hasattr(idea, k) and k != "history":
                    setattr(idea, k, v)
            now = datetime.now(UTC).isoformat()
            if idea.status != before:
                self._check(idea, before)
                idea.history.append({"at": now, "from": before, "to": idea.status,
                                     "by": os.environ.get("HERMES_ROLE") or "owner",
                                     "reason": reason})
                if idea.status == "validated":
                    idea.validation_basis = "holdout"
                if idea.status == "awaiting_approval":
                    idea.resume_status = before
            idea.updated_at = now
            self._save(ideas)
            return idea
        raise KeyError(target.id)

    def legal_targets(self, idea: Idea) -> frozenset[str]:
        """The statuses an idea may move to from where it is (edges only, not the rules)."""
        return _targets(idea)

    def _check(self, idea: Idea, before: str) -> None:
        """Raise if ``before → idea.status`` is not allowed. ``idea`` already carries the changes."""
        to = idea.status
        was = Idea(id=idea.id, title=idea.title, status=before, resume_status=idea.resume_status,
                   history=idea.history)
        if to == "ready_to_forward_test":
            raise IllegalTransition(
                f"{idea.id}: ready_to_forward_test is set only by an owner approval "
                "(`hermes pipeline answer`), never by a status update")
        if to not in _targets(was):
            raise IllegalTransition(f"{idea.id}: {before} → {to} is not a legal transition "
                                    f"(from {before}: {', '.join(sorted(_targets(was))) or 'none'})")
        if to == "validated" and not self._holdout_passed(idea.id):
            raise IllegalTransition(
                f"{idea.id}: validated is earned, not asserted — no passed holdout on record "
                f"({self.holdout_dir / idea.id / 'holdout.json'}). "
                "Earn it with `hermes holdout check` then `hermes holdout unlock`")
        if to == "literature_done" and not idea.links and idea.source not in LINK_FREE_SOURCES:
            raise IllegalTransition(
                f"{idea.id}: literature_done needs at least one source link (--link), "
                f"unless the source is {' or '.join(LINK_FREE_SOURCES)}")
        if to == "rejected":
            f = idea.failure or {}
            if not f.get("kind") or not f.get("assumption"):
                raise IllegalTransition(
                    f"{idea.id}: rejected needs a failure (kind and the assumption that failed)")
            if not idea.verdict:
                raise IllegalTransition(f"{idea.id}: rejected needs a verdict")
        if to == "needs_infra" and not (idea.failure or {}).get("detail"):
            raise IllegalTransition(
                f"{idea.id}: needs_infra must name the missing capability (failure.detail)")

    def _holdout_passed(self, idea_id: str) -> bool:
        path = self.holdout_dir / idea_id / "holdout.json"
        if not path.exists():
            return False
        rec = json.loads(path.read_text())
        return rec.get("state") == "done" and rec.get("passed") is True

    def remove(self, id_or_prefix: str) -> Idea:
        target = self.get(id_or_prefix)
        self._save([i for i in self._load() if i.id != target.id])
        return target

    # --- the pipeline view --------------------------------------------------

    def pipeline(self) -> dict[str, int]:
        """Count per status, in pipeline order — where the work actually is."""
        ideas = self._load()
        return {s: sum(1 for i in ideas if i.status == s) for s in STATUSES}

    def hit_rate(self) -> dict[str, dict]:
        """Validated vs rejected per provenance — which sources are worth your time.

        Only *decided* ideas count: an untested idea is not evidence about its source.
        This is the number that tells you whether the books are paying off.
        """
        out: dict[str, dict] = {}
        for src in SOURCES:
            group = [i for i in self._load() if i.source == src]
            decided = [i for i in group
                       if i.status in ("validated", "ready_to_forward_test", "rejected")]
            validated = [i for i in decided if i.status != "rejected"]
            out[src] = {
                "total": len(group),
                "decided": len(decided),
                "validated": len(validated),
                "rate": round(len(validated) / len(decided), 3) if decided else None,
            }
        return out
