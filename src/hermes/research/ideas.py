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
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_IDEAS_PATH = Path("research/ideas.json")

# Where an idea came from. Kept close to the framework's own list so the hit rate per
# source is measurable: books, your own discretionary screen time, other traders and
# courses, or something you noticed in the data.
SOURCES = ("book", "discretionary", "trader", "course", "data", "other")

# The pipeline. An idea moves forward only on evidence; `parked` is for ideas that are
# blocked (missing data, needs an instrument Hermes can't reach) rather than judged.
STATUSES = (
    "raw",            # captured, not yet quantified
    "quantified",     # unambiguous rules exist — testable
    "testing",        # runs exist, verdict not reached
    "validated",      # survived the rubric; candidate for a portfolio
    "rejected",       # tested and did not survive — keep it, it is a real result
    "parked",         # blocked on something external, not on evidence
)


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
    created_at: str = ""
    updated_at: str = ""

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
        return self.status not in ("validated", "rejected")


@dataclass
class IdeaBook:
    """The backlog, persisted as one JSON file."""

    path: Path = DEFAULT_IDEAS_PATH

    def __post_init__(self) -> None:
        self.path = Path(self.path)

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

    # --- writing -----------------------------------------------------------

    def add(
        self,
        title: str,
        *,
        hypothesis: str = "",
        source: str = "other",
        source_detail: str = "",
        tags: list[str] | None = None,
    ) -> Idea:
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}. Known: {', '.join(SOURCES)}")
        now = datetime.now(UTC).isoformat()
        idea = Idea(
            id=uuid.uuid4().hex[:8],
            title=title,
            hypothesis=hypothesis,
            source=source,
            source_detail=source_detail,
            tags=list(tags or []),
            created_at=now,
            updated_at=now,
        )
        ideas = self._load()
        ideas.append(idea)
        self._save(ideas)
        return idea

    def update(self, id_or_prefix: str, **changes: object) -> Idea:
        """Change fields on an idea. ``run_keys`` and ``tags`` are appended, not replaced."""
        target = self.get(id_or_prefix)
        if "status" in changes and changes["status"] not in STATUSES:
            raise ValueError(
                f"unknown status {changes['status']!r}. Known: {', '.join(STATUSES)}"
            )
        ideas = self._load()
        for idea in ideas:
            if idea.id != target.id:
                continue
            for k, v in changes.items():
                if v is None:
                    continue
                if k in ("run_keys", "tags"):
                    existing = getattr(idea, k)
                    for item in (v if isinstance(v, list) else [v]):
                        if item not in existing:
                            existing.append(item)
                elif hasattr(idea, k):
                    setattr(idea, k, v)
            idea.updated_at = datetime.now(UTC).isoformat()
            self._save(ideas)
            return idea
        raise KeyError(target.id)

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
            decided = [i for i in group if i.status in ("validated", "rejected")]
            validated = [i for i in decided if i.status == "validated"]
            out[src] = {
                "total": len(group),
                "decided": len(decided),
                "validated": len(validated),
                "rate": round(len(validated) / len(decided), 3) if decided else None,
            }
        return out
