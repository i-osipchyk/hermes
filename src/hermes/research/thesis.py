"""The thesis — what the whole backlog has taught, kept in one place.

Only the Strategist writes ``research/thesis.md`` (phase 6); this module ships the template
with fixed headings (so it can be read as context and checked), a reader for a heading's text
and for the part relevant to an idea's tags, and ``due``: whether it is time to rewrite it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_THESIS_PATH = Path("research/thesis.md")
HEADINGS = ("Survived", "Failed", "Recurring mechanisms and regimes", "Open questions",
            "Next hypotheses")
DUE_THRESHOLD = 5
# Ideas that are still work in progress: when none are left the queue is empty.
_WORKING = ("raw", "literature_done", "quantified", "coded", "testing", "needs_infra")

TEMPLATE = """---
updated: ""
ideas_included: []
---
# Thesis

## Survived
<!-- id · evidence tier · N_eff · DSR · holdout · one line -->

## Failed
<!-- grouped by failure.kind -->

## Recurring mechanisms and regimes

## Open questions

## Next hypotheses
<!-- each with informed_by -->
"""


@dataclass
class Thesis:
    front: dict[str, Any] = field(default_factory=lambda: {"updated": "", "ideas_included": []})
    sections: dict[str, str] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> Thesis:
        return cls()

    def section(self, heading: str) -> str:
        return self.sections.get(heading, "").strip()

    def excerpt(self, tags: list[str]) -> str:
        """The lines that mention any of ``tags``, under their headings. Deterministic."""
        wanted = [t.lower() for t in tags if t]
        out: list[str] = []
        for heading, body in self.sections.items():
            hits = [ln for ln in body.splitlines()
                    if ln.strip() and any(t in ln.lower() for t in wanted)]
            if hits:
                out += [f"## {heading}", *hits]
        return "\n".join(out)


def ensure_template(path: Path = DEFAULT_THESIS_PATH) -> Path:
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE)
    return path


def read_thesis(path: Path = DEFAULT_THESIS_PATH) -> Thesis:
    path = Path(path)
    if not path.exists():
        return Thesis.empty()
    text = path.read_text()
    front: dict[str, Any] = {"updated": "", "ideas_included": []}
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            value = value.strip()
            front[key.strip()] = (json.loads(value) if value.startswith("[")
                                  else value.strip('"'))
        text = text[m.end():]
    sections: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return Thesis(front, sections)


def due(book: Any, store: Any, path: Path = DEFAULT_THESIS_PATH,
        threshold: int = DUE_THRESHOLD) -> bool:
    """Time to rewrite: ``threshold`` or more ideas have lessons newer than the thesis, or
    nothing is left in the queue to work on."""
    updated = read_thesis(path).front.get("updated") or ""
    fresh = 0
    for idea in book.all():
        lessons = store.read(idea.id, "lessons")
        if lessons is not None and (lessons.written_at or "") > updated:
            fresh += 1
    queue_empty = not any(i.status in _WORKING for i in book.all())
    return fresh >= threshold or queue_empty
