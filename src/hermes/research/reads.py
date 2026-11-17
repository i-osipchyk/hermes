"""The reads log — what an idea looked at, so what it learned from can be audited.

An agent that reads another idea's results has been informed by them, whether or not it
says so, and that has to count against it (ADR-0013: effective trials are the union of the
*declared* ``informed_by`` and what the log shows it *read*). When ``HERMES_IDEA`` is set,
the research commands that read results append a line to
``research/ideas/<HERMES_IDEA>/reads.jsonl``.

* A **targeted** read (``show``, ``analyze``, ``trials``) names a run or idea. If it belongs
  to another idea, that idea's trial clusters are inherited.
* A **listing** (``runs``, ``ideas`` with no id) is logged and shown to the Validator but
  never counted: looking at a table of contents would otherwise deflate everything.
  Revisit if agents learn to read results through listings.

Honest limit: this logs the Hermes CLI. An agent that reads ``research/output`` or the
ledger directly is not seen; the driver (phase 4) narrows what a role can reach.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .jsonsafe import encode_nonfinite

DEFAULT_IDEAS_DIR = Path("research/ideas")


def default_ideas_dir() -> Path:
    """``HERMES_IDEAS_DIR`` if set (read at call time), else ``research/ideas``."""
    return Path(os.environ.get("HERMES_IDEAS_DIR") or DEFAULT_IDEAS_DIR)


@dataclass
class ReadsLog:
    root: Path | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root) if self.root is not None else default_ideas_dir()

    def path(self, idea: str) -> Path:
        assert self.root is not None
        return self.root / idea / "reads.jsonl"

    def record(self, reader: str, *, command: str, target: str, target_idea: str | None,
               kind: str = "targeted", role: str | None = None) -> None:
        """Append one read by ``reader`` (an idea id)."""
        line = {"at": datetime.now(UTC).isoformat(),
                "role": role if role is not None else os.environ.get("HERMES_ROLE") or "owner",
                "command": command, "target": target, "target_idea": target_idea, "kind": kind}
        path = self.path(reader)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(encode_nonfinite(line), allow_nan=False) + "\n")

    def read(self, idea: str) -> list[dict]:
        path = self.path(idea)
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]

    def read_ideas(self, idea: str) -> list[str]:
        """Distinct other ideas whose results ``idea`` targeted, in first-read order."""
        seen: list[str] = []
        for r in self.read(idea):
            t = r.get("target_idea")
            if r.get("kind") == "targeted" and t and t != idea and t not in seen:
                seen.append(t)
        return seen


def audit_informed_by(book: Any, log: ReadsLog, idea: str) -> dict[str, list[str]]:
    """Declared vs read. ``read_not_declared`` is what the Validator is shown."""
    declared = [i for i in book.get(idea).informed_by if i != idea]
    read = log.read_ideas(idea)
    return {"declared": declared, "read": read,
            "read_not_declared": [i for i in read if i not in declared],
            "declared_not_read": [i for i in declared if i not in read]}
