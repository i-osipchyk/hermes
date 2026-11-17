"""The driver — one idea, from where it is to a verdict (phase 4 §1, ADR-0018).

A loop around the pure ``next_action``: ask what is next, do it, ask again. The driver keeps
**no state of its own** beyond ``run.jsonl`` (an audit trail, not a resume point), so a crash —
or a token limit — resumes by calling it again; finished steps left their artifacts and are not
repeated. Three things it will not do:

* **Trust an agent's "done".** A role step is verified against the files it should have
  produced (``_verify``); a clean exit that wrote an invalid spec is retried with the problem
  list, then the idea parks with a ``driver_stalled`` approval. Outputs are checked, not exit codes.
* **Wait.** An owner decision parks the idea and the driver stops (exit 0); answering resumes it.
* **Decide.** Transition steps (``mark_*``, ``reject_idea``, ``unlock_holdout``) are library calls
  made as ``HERMES_ROLE=driver`` once their preconditions hold; judgement is always a role's.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .approvals import ApprovalBook
from .artifacts import (
    ArtifactStore,
    validate_plan,
    validate_spec,
    validate_validation,
)
from .context import build_context, render_context
from .dev_flow import DevFlow, ExtensionLimit
from .ideas import IdeaBook, IllegalTransition
from .jsonsafe import encode_nonfinite
from .ledger import RunLedger
from .pipeline import Action, next_action, ran_variants
from .reads import ReadsLog
from .roles import STEP_ROLE, RoleRunner, RunResult
from .trials import TrialRegistry

EXIT_PAUSED = 3
DEFAULT_MAX_STEPS = 40
DEFAULT_MAX_ATTEMPTS = 3          # 1 try + 2 retries

# Steps the driver performs itself: a library call, no agent.
TRANSITION_STEPS = {"mark_quantified": "quantified", "mark_coded": "coded",
                    "mark_testing": "testing", "resume_from_infra": "quantified"}
_DRIVER_STEPS = (*TRANSITION_STEPS, "reject_idea", "unlock_holdout")


@contextmanager
def role_env(role: str, idea: str | None = None) -> Iterator[None]:
    """Set ``HERMES_ROLE`` (and ``HERMES_IDEA``) for a block, as the real runner does for a
    subprocess, so library guards see who is acting."""
    keys = {"HERMES_ROLE": role, **({"HERMES_IDEA": idea} if idea else {})}
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@dataclass
class DriverConfig:
    ideas_file: Path = Path("research/ideas.json")
    ideas_dir: Path = Path("research/ideas")
    approvals_file: Path = Path("research/approvals.json")
    thesis_file: Path = Path("research/thesis.md")
    runs_dir: Path | None = None
    strategies_dir: Path = Path("strategies")
    repo: Path = field(default_factory=Path.cwd)
    max_steps: int = DEFAULT_MAX_STEPS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


@dataclass
class RunSummary:
    idea: str
    stopped: str                     # done | parked | max_steps | until | stalled | paused | dry_run | owner
    steps: list[dict] = field(default_factory=list)
    detail: str = ""
    approval: str | None = None

    @property
    def exit_code(self) -> int:
        return {"paused": EXIT_PAUSED, "stalled": 2}.get(self.stopped, 0)

    def to_dict(self) -> dict:
        return {"idea": self.idea, "stopped": self.stopped, "detail": self.detail,
                "approval": self.approval, "steps": self.steps}


class ScriptedRunner:
    """A fake agent for tests: ``{(role, step): fn(idea, prompt)}``. The script runs with
    ``HERMES_ROLE`` set, so it writes through the real artifact and idea guards. A script may
    return a ``RunResult`` (to simulate a failure or the token limit); an exception propagates,
    which is how a test crashes the run."""

    def __init__(self, scripts: dict[tuple[str, str], Callable[[str, str], Any]]):
        self.scripts = scripts
        self.calls: list[dict] = []

    def run(self, role: str, idea: str, step: str, prompt: str, *,
            cwd: Path | None = None) -> RunResult:
        self.calls.append({"role": role, "idea": idea, "step": step, "prompt": prompt, "cwd": cwd})
        fn = self.scripts.get((role, step))
        if fn is None:
            return RunResult(ok=False, error=f"no script for {role}:{step}")
        with role_env(role, idea):
            out = fn(idea, prompt)
        return out if isinstance(out, RunResult) else RunResult(
            tokens=10, session_id="scripted", model="scripted")


class Driver:
    def __init__(self, cfg: DriverConfig, runner: RoleRunner, *,
                 unlocker: Callable[[str, str], None] | None = None,
                 filler: Callable[..., Any] | None = None,
                 devflow: DevFlow | None = None):
        self.cfg, self.runner = cfg, runner
        self.book = IdeaBook(cfg.ideas_file, holdout_dir=cfg.ideas_dir)
        self.store = ArtifactStore(cfg.ideas_dir)
        self.ledger = RunLedger(cfg.runs_dir)
        self.approvals = ApprovalBook(cfg.approvals_file, self.book)
        self.unlocker = unlocker or self._cli_unlock
        self.filler = filler
        self.devflow = devflow or DevFlow(cfg.repo, cfg.ideas_dir)

    # --- the loop ---------------------------------------------------------------------

    def next(self, idea_id: str) -> Action | None:
        idea = self.book.get(idea_id)
        return next_action(idea, self.store, ran_variants=ran_variants(self.ledger, idea.id))

    def run(self, idea_id: str, *, until: str | None = None, max_steps: int | None = None,
            dry_run: bool = False) -> RunSummary:
        idea_id = self.book.get(idea_id).id
        limit = max_steps if max_steps is not None else self.cfg.max_steps
        summary = RunSummary(idea_id, "max_steps")
        if dry_run:
            return self._preview(idea_id, summary)
        for _ in range(limit):
            idea = self.book.get(idea_id)
            if idea.status == "awaiting_approval":
                a = self.approvals.get(idea.approval_id) if idea.approval_id else None
                return self._stop(summary, "parked", f"waiting on approval {idea.approval_id}: "
                                  f"{a.question if a else ''}", idea.approval_id)
            act = self.next(idea_id)
            if act is None:
                return self._stop(summary, "done", f"nothing left to do at {idea.status}")
            if until and act.step == until:
                return self._stop(summary, "until", f"next step is {act.step}")
            if act.role == "owner":
                return self._stop(summary, "owner", f"{act.step}: waiting on the owner "
                                  f"(`hermes pipeline approvals`)")
            outcome = self._perform(idea_id, act)
            summary.steps.append(outcome)
            status = outcome["outcome"]
            if status in ("paused", "stalled", "parked"):
                return self._stop(summary, status, outcome.get("detail", ""),
                                  outcome.get("approval"))
        return self._stop(summary, "max_steps", f"stopped after {limit} steps; run again to resume")

    def step(self, idea_id: str) -> RunSummary:
        return self.run(idea_id, max_steps=1)

    @staticmethod
    def _stop(summary: RunSummary, why: str, detail: str = "",
              approval: str | None = None) -> RunSummary:
        summary.stopped, summary.detail, summary.approval = why, detail, approval
        return summary

    def _preview(self, idea_id: str, summary: RunSummary) -> RunSummary:
        """What the driver would do with the files as they are: no role runs, nothing changes.
        Consecutive driver transitions are previewed by advancing a copy of the idea."""
        idea = self.book.get(idea_id)
        ran = ran_variants(self.ledger, idea.id)
        for _ in range(10):
            act = next_action(idea, self.store, ran_variants=ran)
            if act is None or idea.status == "awaiting_approval":
                break
            summary.steps.append({"step": act.step, "role": act.role, "outcome": "would"})
            if act.step in TRANSITION_STEPS:
                idea = replace(idea, status=TRANSITION_STEPS[act.step])
                continue
            break
        return self._stop(summary, "dry_run", "no role was run and nothing changed")

    # --- one step ---------------------------------------------------------------------

    def _perform(self, idea_id: str, act: Action) -> dict:
        t0 = time.monotonic()
        if act.step in _DRIVER_STEPS:
            try:
                with role_env("driver", idea_id):
                    self._transition(idea_id, act)
            except (IllegalTransition, ValueError, RuntimeError, OSError, SystemExit) as e:
                return self._stall(idea_id, act, [f"{act.step}: {e}"], 1, t0)
            self._log(idea_id, step=act.step, role="driver", attempt=1, outcome="done",
                      duration_s=time.monotonic() - t0)
            return {"step": act.step, "role": "driver", "outcome": "done"}
        return self._role_step(idea_id, act, t0)

    def _transition(self, idea_id: str, act: Action) -> None:
        if act.step in TRANSITION_STEPS:
            self.book.update(idea_id, status=TRANSITION_STEPS[act.step],
                             reason=f"driver: {act.step}")
        elif act.step == "reject_idea":
            v = self.store.read(idea_id, "validation")
            self.book.update(idea_id, status="rejected", verdict=v.rationale, failure=v.failure,
                             reason="validator verdict: reject")
        elif act.step == "unlock_holdout":
            v = self.store.read(idea_id, "validation")
            self.unlocker(idea_id, v.candidate_run)
            if self.book.get(idea_id).status not in ("validated", "rejected"):
                raise RuntimeError("the holdout unlock left the idea undecided")

    def _role_step(self, idea_id: str, act: Action, t0: float) -> dict:
        role, step, cwd, problems = act.role, act.step, None, []
        pre = self._precheck(idea_id, act)
        if pre:                                    # refused mechanically, before the Validator
            role, step = "quant", "write_plan"
            problems = [f"the plan cannot be reviewed yet: {p}" for p in pre]
        if step == "validate":
            try:
                self._fill(idea_id)
            except (ValueError, OSError, KeyError) as e:
                return self._stall(idea_id, act, [f"could not compute the validation: {e}"], 1, t0)
        if step == "build_infra":
            try:
                cwd = Path(self.devflow.start(idea_id).worktree)
            except ExtensionLimit as e:
                return self._park_infra(idea_id, str(e))
            except RuntimeError as e:
                return self._stall(idea_id, act, [str(e)], 1, t0)

        for attempt in range(1, self.cfg.max_attempts + 1):
            prompt = self._prompt(idea_id, role, step, act, problems, cwd)
            started = time.monotonic()
            result = self.runner.run(role, idea_id, step, prompt, cwd=cwd)
            took = time.monotonic() - started
            if result.paused:
                self._log(idea_id, step=step, role=role, model=result.model, attempt=attempt,
                          outcome="paused", duration_s=took, tokens=result.tokens,
                          session_id=result.session_id, reset_at=result.reset_at)
                return {"step": step, "role": role, "outcome": "paused",
                        "detail": f"the subscription limit was hit"
                                  f"{f' (resets {result.reset_at})' if result.reset_at else ''}; "
                                  "run again to resume"}
            problems = [result.error or "the role reported failure"] if not result.ok \
                else self._verify(idea_id, step)
            approval = None
            if not problems and step == "build_infra":
                try:
                    approval = self.devflow.submit(idea_id, self.approvals)
                except Exception as e:  # noqa: BLE001 — a gate failure or a missing record → retry
                    problems = [getattr(e, "output", None) or str(e)]
            self._log(idea_id, step=step, role=role, model=result.model, attempt=attempt,
                      outcome="verified" if not problems else "failed: " + "; ".join(problems)[:200],
                      duration_s=took, tokens=result.tokens, session_id=result.session_id)
            if not problems:
                if step == "review_plan" and self._locked(idea_id):
                    self.store.seal(idea_id, "plan")
                if step == "validate":
                    self.store.seal(idea_id, "validation")
                if approval is not None:
                    return {"step": step, "role": role, "outcome": "parked",
                            "approval": approval.id,
                            "detail": f"{approval.kind} approval {approval.id}: {approval.question}"}
                return {"step": step, "role": role, "outcome": "done", "attempts": attempt}
        return self._stall(idea_id, act, problems, self.cfg.max_attempts, t0)

    # --- verification: outputs, not exit codes -----------------------------------------

    def _locked(self, idea_id: str) -> bool:
        plan = self.store.read(idea_id, "plan")
        from .pipeline import _locked
        return _locked(plan)

    def _precheck(self, idea_id: str, act: Action) -> list[str]:
        if act.step != "review_plan":
            return []
        plan = self.store.read(idea_id, "plan")
        return validate_plan(plan) if plan is not None else ["there is no plan.json"]

    def _verify(self, idea_id: str, step: str) -> list[str]:
        s = self.store
        if step == "write_spec":
            spec = s.read(idea_id, "spec")
            return ["spec.json was not written"] if spec is None else validate_spec(spec)
        if step == "write_plan":
            plan = s.read(idea_id, "plan")
            return ["plan.json was not written"] if plan is None else validate_plan(plan)
        if step == "review_plan":
            plan = s.read(idea_id, "plan")
            if plan is None:
                return ["plan.json is missing"]
            if self._locked(idea_id) or plan.review.get("objections"):
                return []                          # approved, or sent back with objections
            return ["the plan was neither approved nor given objections"]
        if step == "write_strategy":
            name = self.book.get(idea_id).strategy
            if not name:
                return ["the idea has no strategy (`hermes ideas set <idea> --strategy <name>`)"]
            if not list(Path(self.cfg.strategies_dir).rglob(f"{name}.py")):
                return [f"strategy file {name}.py is not under {self.cfg.strategies_dir}"]
            return []
        if step == "run_variants":
            plan = s.read(idea_id, "plan")
            missing = [v["id"] for v in (plan.variants if plan else [])
                       if v["id"] not in ran_variants(self.ledger, idea_id)]
            return [f"variants not run: {', '.join(missing)}"] if missing else []
        if step == "validate":
            v = s.read(idea_id, "validation")
            return ["validation.json was not written"] if v is None else validate_validation(v)
        if step == "amend_plan":
            plan = s.read(idea_id, "plan")
            if plan is None or self._locked(idea_id):
                return ["the plan was not amended (`hermes pipeline amend-plan`)"]
            return validate_plan(plan)
        if step == "reflect":
            return [] if s.read(idea_id, "lessons") is not None else ["lessons.json was not written"]
        if step == "build_infra":
            return []                              # verified by DevFlow.submit
        return []

    # --- parking and logging -----------------------------------------------------------

    def _stall(self, idea_id: str, act: Action, problems: list[str], attempts: int,
               t0: float) -> dict:
        why = "; ".join(problems)[:300]
        self._log(idea_id, step=act.step, role=act.role, attempt=attempts, outcome="stalled",
                  duration_s=time.monotonic() - t0, detail=why)
        try:
            with role_env("driver", idea_id):
                a = self.approvals.park(
                    idea_id, "other", f"driver_stalled at {act.step} after {attempts} "
                    f"attempt(s): {why}",
                    "read run.jsonl and the artifacts; fix or deny to park",
                    evidence=[str(self.cfg.ideas_dir / idea_id / "run.jsonl")])
            approval = a.id
        except (ValueError, IllegalTransition):
            approval = None
        return {"step": act.step, "role": act.role, "outcome": "stalled", "detail": why,
                "approval": approval}

    def _park_infra(self, idea_id: str, why: str) -> dict:
        with role_env("driver", idea_id):
            self.book.update(idea_id, status="parked", reason=why, failure={
                "kind": "infra", "assumption": "the capability can be built within limits",
                "detail": why})
        self._log(idea_id, step="build_infra", role="developer", attempt=1,
                  outcome="parked: " + why, duration_s=0.0)
        return {"step": "build_infra", "role": "developer", "outcome": "parked", "detail": why}

    def _log(self, idea_id: str, **fields: Any) -> None:
        path = self.cfg.ideas_dir / idea_id / "run.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(UTC).isoformat(), **fields}
        with path.open("a") as f:
            f.write(json.dumps(encode_nonfinite(row), allow_nan=False, default=str) + "\n")

    # --- prompt, validation fill, unlock ------------------------------------------------

    def context(self, idea_id: str) -> dict:
        reads = ReadsLog(self.cfg.ideas_dir)
        return build_context(idea_id, book=self.book, store=self.store, ledger=self.ledger,
                             registry=TrialRegistry(self.ledger, self.book, reads),
                             approvals=self.approvals, reads=reads,
                             thesis_path=self.cfg.thesis_file)

    def _prompt(self, idea_id: str, role: str, step: str, act: Action, problems: list[str],
                cwd: Path | None) -> str:
        parts = [f"# Step `{step}` as {role} — idea {idea_id}",
                 f"It should produce: {', '.join(act.produces) or '(see your role)'}."]
        if cwd:
            parts.append(f"Work only in the git worktree {cwd}.")
        if problems:
            parts += ["", "The previous attempt failed verification. Fix exactly this:"]
            parts += [f"- {p}" for p in problems]
        parts += ["", render_context(self.context(idea_id))]
        return "\n".join(parts)

    def _fill(self, idea_id: str) -> None:
        if self.filler is None:
            from .validation_fill import fill_validation
            registry = TrialRegistry(self.ledger, self.book, ReadsLog(self.cfg.ideas_dir))
            validation = fill_validation(idea_id, book=self.book, store=self.store,
                                         ledger=self.ledger, registry=registry,
                                         reads=ReadsLog(self.cfg.ideas_dir))
        else:
            validation = self.filler(idea_id, self)
        self.store.write_mechanical(idea_id, validation, role="validator")

    def _cli_unlock(self, idea_id: str, run_key: str) -> None:
        from ..cli import EXIT_EMPTY, EXIT_OK, main
        argv = ["holdout", "unlock", idea_id, "--run-key", run_key, "--json",
                "--ideas-file", str(self.cfg.ideas_file), "--out-dir", str(self.cfg.ideas_dir),
                "--dir", str(self.cfg.strategies_dir)]
        if self.cfg.runs_dir:
            argv += ["--runs-dir", str(self.cfg.runs_dir)]
        code = main(argv)
        if code not in (EXIT_OK, EXIT_EMPTY):         # EXIT_EMPTY is a failed holdout: a result
            raise RuntimeError(f"holdout unlock failed (exit {code})")


__all__ = ["Driver", "DriverConfig", "RunSummary", "ScriptedRunner", "role_env", "EXIT_PAUSED",
           "STEP_ROLE"]
