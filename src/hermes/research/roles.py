"""Role definitions and the agent boundary (phase 4 §1–2).

A **role** is a Markdown file in ``research/roles/`` — frontmatter (model, tool allow/deny
lists, skills, what it may write) and a standing prompt — versioned with the code, not kept in
anyone's ``~/.claude``. A **RoleRunner** is the one seam between the driver and an agent:
``ClaudeCodeRunner`` launches ``claude -p`` (headless, per ADR-0008/0010), ``ScriptedRunner``
(in ``driver``) stands in for it in tests, so no test starts an agent.

Boundaries live in what the runner sets, not in the prompt: ``HERMES_ROLE`` (artifact
permissions), ``HERMES_IDEA`` (the reads log), and the tool allow/deny lists.
Honest limit (as ADR-0012): an agent with a shell and a determined intent can step around env
vars; ``history``, ``reads.jsonl`` and ``run.jsonl`` make that visible after the fact.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

ROLES_DIR = Path(__file__).parent / "roles"
ROLE_NAMES = ("researcher", "quant", "validator", "developer", "mechanical")
# Which role performs which step (the driver's own transitions are not here).
STEP_ROLE = {
    "write_spec": "researcher", "add_source": "researcher", "reflect": "researcher",
    "write_plan": "quant", "write_strategy": "quant", "run_variants": "quant",
    "amend_plan": "quant", "review_plan": "validator", "validate": "validator",
    "build_infra": "developer",
}
# What a stopped-by-the-subscription message looks like. The headless CLI's exact behaviour at
# the limit could not be provoked in a spike, so detection is by message; a miss is a failed
# step (retried, then parked), never silent success.
_LIMIT = re.compile(r"usage limit|limit reached|rate.?limit|quota|resets? (at|in)|try again later",
                    re.I)
_RESET = re.compile(r"resets? (?:at|in)\s+([^.\n]+)", re.I)


@dataclass(frozen=True)
class RoleDef:
    role: str
    model: str
    tools_allow: tuple[str, ...] = ()
    tools_deny: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    prompt: str = ""


@dataclass(frozen=True)
class RunResult:
    ok: bool = True
    paused: bool = False                 # the subscription limit was hit: resumable, not failed
    reset_at: str | None = None
    tokens: int | None = None
    session_id: str | None = None
    error: str = ""
    model: str = ""


class RoleRunner(Protocol):
    def run(self, role: str, idea: str, step: str, prompt: str, *,
            cwd: Path | None = None) -> RunResult: ...


def load_role(role: str, roles_dir: Path = ROLES_DIR) -> RoleDef:
    path = Path(roles_dir) / f"{role}.md"
    if not path.exists():
        raise KeyError(f"no role definition {path}")
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not m:
        raise ValueError(f"{path}: missing frontmatter")
    meta: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        meta[key.strip()] = json.loads(value) if value.startswith("[") else value
    if meta.get("role") != role:
        raise ValueError(f"{path}: frontmatter role {meta.get('role')!r} != {role!r}")
    return RoleDef(role=role, model=meta["model"], tools_allow=tuple(meta.get("tools_allow", ())),
                   tools_deny=tuple(meta.get("tools_deny", ())), skills=tuple(meta.get("skills", ())),
                   writes=tuple(meta.get("writes", ())), prompt=m.group(2).strip())


def parse_result(stdout: str, stderr: str, returncode: int, model: str = "") -> RunResult:
    """Read ``claude -p --output-format json``: ``result``, ``is_error``, ``session_id``,
    ``usage``. A subscription-limit message is a *pause*, not a failure."""
    data: dict[str, Any] = {}
    try:
        data = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        data = {}
    text = " ".join(str(x) for x in (data.get("result"), stderr, stdout if not data else "") if x)
    usage = data.get("usage") or {}
    tokens = None
    if usage:
        tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                       "cache_creation_input_tokens",
                                                       "cache_read_input_tokens"))
    sid = data.get("session_id")
    failed = bool(returncode) or bool(data.get("is_error"))
    if failed and _LIMIT.search(text):
        reset = _RESET.search(text)
        return RunResult(ok=False, paused=True, reset_at=reset.group(1).strip() if reset else None,
                         tokens=tokens, session_id=sid, error=text[:300], model=model)
    return RunResult(ok=not failed, tokens=tokens, session_id=sid, model=model,
                     error=text[:300] if failed else "")


@dataclass
class ClaudeCodeRunner:
    """Launch one role step as a fresh headless ``claude -p`` session in the repo."""

    repo: Path = field(default_factory=Path.cwd)
    roles_dir: Path = ROLES_DIR
    timeout_s: float = 0.0               # 0 → HERMES_ROLE_TIMEOUT (default 1800)
    runner: Any = None                   # subprocess.run stand-in, for tests

    def command(self, rd: RoleDef, prompt: str, session_id: str) -> list[str]:
        cmd = ["claude", "-p", prompt, "--model", rd.model, "--output-format", "json",
               "--permission-mode", "acceptEdits", "--session-id", session_id,
               "--append-system-prompt", rd.prompt]
        if rd.tools_allow:
            cmd += ["--allowedTools", *rd.tools_allow]
        if rd.tools_deny:
            cmd += ["--disallowedTools", *rd.tools_deny]
        return cmd

    def env(self, role: str, idea: str) -> dict[str, str]:
        return {**os.environ, "HERMES_ROLE": role, "HERMES_IDEA": idea, "HERMES_RESEARCH": "1"}

    def run(self, role: str, idea: str, step: str, prompt: str, *,
            cwd: Path | None = None) -> RunResult:
        rd = load_role(role, self.roles_dir)
        sid = str(uuid.uuid4())
        timeout = self.timeout_s or float(os.environ.get("HERMES_ROLE_TIMEOUT") or 1800)
        run = self.runner or subprocess.run
        try:
            done = run(self.command(rd, prompt, sid), cwd=str(cwd or self.repo),
                       env=self.env(role, idea), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return RunResult(ok=False, session_id=sid, model=rd.model,
                             error=f"timed out after {timeout:.0f}s")
        except FileNotFoundError:
            return RunResult(ok=False, session_id=sid, model=rd.model,
                             error="the `claude` CLI is not installed")
        out = parse_result(done.stdout, done.stderr, done.returncode, rd.model)
        return out if out.session_id else RunResult(**{**out.__dict__, "session_id": sid})
