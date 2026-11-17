"""The Developer flow (phase 4 §6): a missing capability becomes a reviewed branch.

``needs_infra`` → a git **worktree** on ``dev/<idea>-<n>`` so the Developer never touches the
tree the other roles read → the Developer builds test-first → ``submit`` classifies the branch
**mechanically** from its diff (the Developer cannot downgrade it) and parks a ``dev_merge`` or
``engine_semantics`` approval → on approval ``merge`` re-runs both gates on the branch and
merges to the local base branch.

Nothing here pushes. ``extension.json`` (per idea) is the trail: one item per extension.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .jsonsafe import encode_nonfinite

MAX_EXTENSIONS = 3

# Paths whose change alters what a backtest *means* (fills, costs, bars, the forming bar,
# look-ahead) or the pipeline's own guards. A diff touching any of them is `engine_semantics`
# whatever the Developer says (ADR-0001/0002/0004/0015, CLAUDE.md "Look-ahead is the cardinal sin").
ENGINE_PATHS = (
    "src/hermes/backtest/", "src/hermes/execution/", "src/hermes/core/",
    "src/hermes/indicators/", "src/hermes/research/holdout.py", "src/hermes/research/unlock.py",
    "src/hermes/research/ideas.py", "src/hermes/research/artifacts.py",
    "src/hermes/research/approvals.py", "src/hermes/research/driver.py",
    "src/hermes/research/dev_flow.py",
)


def default_gates() -> list[list[str]]:
    return [[sys.executable, "-m", "pytest", "-q", "-x"], [sys.executable, "-m", "ruff", "check", "."]]


class ExtensionLimit(RuntimeError):
    """An idea already used its extensions."""


class GateFailed(RuntimeError):
    """A gate (tests or lint) failed on a Developer branch."""

    def __init__(self, output: str):
        super().__init__(output)
        self.output = output


@dataclass
class Extension:
    topic: str
    branch: str
    worktree: str
    status: str = "building"          # building | submitted | merged | refused
    classification: str = ""          # dev_merge | engine_semantics (computed, from the diff)
    adds: str = ""
    tests_added: int = 0
    files: list[str] = field(default_factory=list)
    gate_output: str = ""
    approval_id: str = ""
    created_at: str = ""
    merged_at: str = ""


def classify(files: list[str]) -> str:
    hit = any(f.startswith(p) or f == p for f in files for p in ENGINE_PATHS)
    return "engine_semantics" if hit else "dev_merge"


def _path(ideas_dir: Path, idea: str) -> Path:
    return Path(ideas_dir) / idea / "extension.json"


def read_extensions(ideas_dir: Path, idea: str) -> list[dict]:
    p = _path(ideas_dir, idea)
    return json.loads(p.read_text())["extensions"] if p.exists() else []


def _write(ideas_dir: Path, idea: str, items: list[dict]) -> None:
    p = _path(ideas_dir, idea)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(encode_nonfinite({"extensions": items}), indent=2,
                              allow_nan=False) + "\n")
    os.replace(tmp, p)


@dataclass
class DevFlow:
    repo: Path
    ideas_dir: Path
    gates: list[list[str]] = field(default_factory=default_gates)
    base: str = "master"
    worktrees: Path | None = None        # default: <repo>/../.hermes_worktrees/<repo name>

    def __post_init__(self) -> None:
        self.repo, self.ideas_dir = Path(self.repo), Path(self.ideas_dir)
        if self.worktrees is None:
            self.worktrees = self.repo.parent / ".hermes_worktrees" / self.repo.name

    # --- git --------------------------------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        done = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True,
                              text=True)
        if done.returncode:
            raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
        return done.stdout

    def _items(self, idea: str) -> list[dict]:
        return read_extensions(self.ideas_dir, idea)

    def _current(self, idea: str) -> dict:
        items = self._items(idea)
        if not items:
            raise ValueError(f"{idea}: no extension started")
        return items[-1]

    # --- the lifecycle ----------------------------------------------------------------

    def start(self, idea: str) -> Extension:
        """A worktree on a fresh branch for the Developer. The 4th extension is refused."""
        items = self._items(idea)
        if items and items[-1]["status"] == "building":
            return Extension(**items[-1])              # resume a crashed build in the same tree
        if len(items) >= MAX_EXTENSIONS:
            raise ExtensionLimit(f"{idea} has used its {MAX_EXTENSIONS} extensions")
        n = len(items) + 1
        topic = f"{idea}-{n}"
        branch, tree = f"dev/{topic}", Path(self.worktrees) / topic
        tree.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-b", branch, str(tree), self.base)
        ext = Extension(topic=topic, branch=branch, worktree=str(tree),
                        created_at=datetime.now(UTC).isoformat())
        _write(self.ideas_dir, idea, [*items, ext.__dict__])
        return ext

    def record(self, idea: str, *, adds: str, tests_added: int = 0) -> None:
        """What the Developer says it built (judgement; the files and class are computed)."""
        items = self._items(idea)
        if not items or items[-1]["status"] != "building":
            raise ValueError(f"{idea}: no extension is being built")
        items[-1].update(adds=adds, tests_added=tests_added)
        _write(self.ideas_dir, idea, items)

    def changed_files(self, branch: str) -> list[str]:
        out = self._git("diff", "--name-only", f"{self.base}...{branch}")
        return [ln for ln in out.splitlines() if ln]

    def _run_gates(self, cwd: Path) -> str:
        outputs = []
        for cmd in self.gates:
            done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-8:])
            outputs.append(f"$ {' '.join(cmd[-3:])}\n{tail}")
            if done.returncode:
                raise GateFailed("\n".join(outputs))
        return "\n".join(outputs)

    def submit(self, idea: str, approvals: Any) -> Any:
        """After the Developer: classify from the diff, run the gates, park the approval.
        Raises ``ValueError`` (nothing recorded) or ``GateFailed`` (nothing parked)."""
        items = self._items(idea)
        ext = items[-1] if items else None
        if not ext or ext["status"] != "building" or not ext["adds"].strip():
            raise ValueError(f"{idea}: the extension has no record of what it adds "
                             "(`hermes pipeline extension <idea> --adds …`)")
        files = self.changed_files(ext["branch"])
        if not files:
            raise ValueError(f"{idea}: branch {ext['branch']} has no changes against {self.base}")
        output = self._run_gates(Path(ext["worktree"]))
        kind = classify(files)
        stat = self._git("diff", "--stat", f"{self.base}...{ext['branch']}").strip()
        approval = approvals.park(
            idea, kind,
            f"merge {ext['branch']} ({len(files)} file(s)): {ext['adds']}",
            "approve if the diff is what the idea needs" if kind == "dev_merge" else
            "engine paths changed — read the diff before approving",
            evidence=[stat, output], branch=ext["branch"])
        ext.update(status="submitted", classification=kind, files=files, gate_output=output,
                   approval_id=approval.id)
        _write(self.ideas_dir, idea, items)
        return approval

    def merge(self, idea: str, branch: str) -> str:
        """On approval: re-run both gates on the branch, then merge to the local base branch.
        Refuses (``GateFailed``) with the output when a gate fails. Never pushes."""
        items = self._items(idea)
        ext = next((e for e in items if e["branch"] == branch), None)
        if ext is None or ext["status"] != "submitted":
            raise ValueError(f"{idea}: {branch} is not a submitted extension")
        on = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        if on != self.base:
            raise RuntimeError(f"merge from {self.base} (the repo is on {on})")
        output = self._run_gates(Path(ext["worktree"]))
        self._git("merge", "--no-ff", branch, "-m", f"Merge {branch}: {ext['adds']}")
        self._git("worktree", "remove", "--force", ext["worktree"])
        ext.update(status="merged", merged_at=datetime.now(UTC).isoformat())
        _write(self.ideas_dir, idea, items)
        return output

    def refuse(self, idea: str, branch: str) -> None:
        items = self._items(idea)
        for e in items:
            if e["branch"] == branch:
                e["status"] = "refused"
        _write(self.ideas_dir, idea, items)
