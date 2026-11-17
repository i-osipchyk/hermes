"""Per-idea artifacts — what an iteration needs to know about an idea lives in files.

``research/ideas/<id>/`` holds ``spec.json``, ``plan.json``, ``validation.json`` and
``lessons.json`` (``trials.json`` and ``holdout.json`` are written by the CLI, ``reads.jsonl``
by the reads log). They are committed like ``ideas.json``: work product, not cache.

Three rules make them more than a notebook:

* **Role permissions.** ``validation.json`` is the Validator's alone, so the agent that wants
  the verdict cannot write it. The role is ``HERMES_ROLE``. Honest limit (as ADR-0012): it is
  an env var, so a determined agent with a shell can bypass it; the driver launches each role
  with its own value and ``history`` makes a bypass visible, not impossible.
* **The plan lock.** Once the Validator approves ``plan.json``, its variants are frozen by hash;
  adding a variant is an *amendment* that needs review again. No variant runs unplanned.
* **Structural validators.** ``validate_spec`` / ``validate_plan`` check shape only — every
  number named, a falsifier present, no parameter nudge passed off as an axis. Whether the
  plan is *good* is the Validator's judgement.

Every file is strict JSON (``jsonsafe``) and written atomically (temp file, then rename).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .jsonsafe import decode_nonfinite, encode_nonfinite

DEFAULT_IDEAS_DIR = Path("research/ideas")
SCHEMA_VERSION = 1

# What a plan variant may change. A parameter nudge (fast 10 → 12) is deliberately not here:
# it is the search the plan exists to prevent.
PLAN_AXES = ("entry", "exit", "regime_filter", "instrument", "timeframe", "horizon")
_NUDGE_AXES = ("param", "params", "parameter", "parameters", "threshold", "nudge", "tuning")
MAX_EXTENSIONS = 3
MAX_PARAMETERS = 3

ROLES = ("researcher", "quant", "validator", "developer", "strategist")
_NO_ROLE = (None, "", "owner")           # the human: not restricted


class ArtifactPermission(PermissionError):
    """A role wrote an artifact it does not own."""


class PlanLocked(ValueError):
    """The variants of a reviewed plan changed without an amendment."""


# --- the artifacts ---------------------------------------------------------------


@dataclass
class Spec:
    """``spec.json`` — unambiguous rules, every invented number named."""

    claim: str = ""
    mechanism: str = ""
    instruments: list[str] = field(default_factory=list)
    timeframes: list[str] = field(default_factory=list)
    data_needs: list[str] = field(default_factory=list)
    entry: str = ""
    exit: str = ""
    sizing: str = ""
    parameters: list[dict] = field(default_factory=list)          # declared Parameters
    extra_parameters_justification: str = ""
    invented_numbers: list[dict] = field(default_factory=list)    # {name, value, why}
    lookahead: str = ""
    expected_sign: str = ""
    expected_size: str = ""
    falsifier: str = ""
    infra_needs: list[str] = field(default_factory=list)
    strategy_path: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass
class Plan:
    """``plan.json`` — the pre-registered test plan."""

    variants: list[dict] = field(default_factory=list)   # {id, axis, change, tests, decision_rule}
    budget: dict = field(default_factory=lambda: {"max_clusters": 5,
                                                  "max_extensions": MAX_EXTENSIONS})
    review: dict = field(default_factory=dict)           # {approved, objections, reviewer}
    locked_hash: str | None = None
    amendments: list[dict] = field(default_factory=list)  # {at, replaced_hash, by}
    schema_version: int = SCHEMA_VERSION


@dataclass
class Validation:
    """``validation.json`` — the Validator's verdict. Nobody else writes it."""

    verdict: str = ""        # reject | continue | ready_for_unlock | validated
    tier: str = ""
    n_eff: float | None = None
    dsr: float | None = None
    null_p: float | None = None
    holdout_state: str = ""
    axes: dict = field(default_factory=dict)             # axis -> pointer into analysis.json
    not_measured: list[dict] = field(default_factory=list)   # {axis, command}
    objections: list[str] = field(default_factory=list)
    read_not_declared: list[str] = field(default_factory=list)   # ideas read but not declared
    schema_version: int = SCHEMA_VERSION


@dataclass
class Lessons:
    """``lessons.json`` — the Researcher's reflection. Its presence means 'reflected'."""

    tested: dict = field(default_factory=dict)           # cluster ids, run keys
    outcome: str = ""
    failure: dict | None = None
    would_change_my_mind: str = ""
    next_hypotheses: list[dict] = field(default_factory=list)  # {title, hypothesis, informed_by}
    infra_gaps: list[str] = field(default_factory=list)
    surprises: list[str] = field(default_factory=list)
    written_at: str = ""                                 # set by the store when written
    schema_version: int = SCHEMA_VERSION


_TYPES: dict[str, type] = {"spec": Spec, "plan": Plan, "validation": Validation,
                           "lessons": Lessons}
_WRITERS: dict[str, tuple[str, ...]] = {
    "spec": ("researcher", "quant"),
    "plan": ("quant", "validator"),
    "validation": ("validator",),
    "lessons": ("researcher",),
}


def _load_as(cls: type, d: dict) -> Any:
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in decode_nonfinite(d).items() if k in known})


def artifact_from_dict(name: str, d: dict) -> Any:
    """A typed artifact from parsed JSON (unknown keys ignored, missing ones defaulted)."""
    return _load_as(ArtifactStore._cls(name), d)


def variants_hash(variants: list[dict]) -> str:
    return hashlib.sha256(json.dumps(variants, sort_keys=True).encode()).hexdigest()[:16]


def _role(role: str | None) -> str | None:
    r = role if role is not None else os.environ.get("HERMES_ROLE")
    return None if r in _NO_ROLE else r


# --- the store -------------------------------------------------------------------


@dataclass
class ArtifactStore:
    root: Path = DEFAULT_IDEAS_DIR

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def path(self, idea_id: str, name: str) -> Path:
        return self.root / idea_id / f"{name}.json"

    def read(self, idea_id: str, name: str) -> Any:
        cls = self._cls(name)
        path = self.path(idea_id, name)
        if not path.exists():
            return None
        return _load_as(cls, json.loads(path.read_text()))

    def write(self, idea_id: str, name: str, artifact: Any, *, role: str | None = None) -> Any:
        self._cls(name)
        self._allow(name, role)
        if name == "plan":
            artifact = self._guard_plan(idea_id, artifact, _role(role))
        if name == "lessons":
            artifact.written_at = datetime.now(UTC).isoformat()
        self._dump(idea_id, name, artifact)
        return artifact

    # --- the plan: review, lock, amendment -----------------------------------------

    def approve_plan(self, idea_id: str, *, reviewer: str, role: str | None = None) -> Plan:
        """The Validator's approval: freeze the variants by hash. Refuses a structurally
        invalid plan."""
        if _role(role) not in (None, "validator"):
            raise ArtifactPermission(f"role {_role(role)!r} cannot approve a plan; "
                                     "only the validator can")
        plan = self.read(idea_id, "plan")
        if plan is None:
            raise ValueError(f"{idea_id}: no plan.json to approve")
        problems = validate_plan(plan)
        if problems:
            raise ValueError(f"{idea_id}: the plan is not valid: " + "; ".join(problems))
        plan.review = {"approved": True, "objections": [], "reviewer": reviewer}
        plan.locked_hash = variants_hash(plan.variants)
        self._dump(idea_id, "plan", plan)
        return plan

    def amend_plan(self, idea_id: str, new: Plan, *, role: str | None = None) -> Plan:
        """Replace the variants of a (possibly locked) plan. The approval is withdrawn."""
        self._allow("plan", role)
        old = self.read(idea_id, "plan")
        new.amendments = list(old.amendments if old else []) + [{
            "at": datetime.now(UTC).isoformat(),
            "replaced_hash": old.locked_hash if old else None,
            "by": _role(role) or "owner"}]
        new.locked_hash = None
        new.review = {"approved": False, "objections": [], "reviewer": ""}
        self._dump(idea_id, "plan", new)
        return new

    def _guard_plan(self, idea_id: str, plan: Plan, role: str | None) -> Plan:
        old = self.read(idea_id, "plan")
        if old is not None and old.locked_hash:
            if variants_hash(plan.variants) != old.locked_hash:
                raise PlanLocked(
                    f"{idea_id}: the plan is reviewed and locked; changing its variants is "
                    "an amendment (amend_plan) and needs review again")
            plan.locked_hash, plan.review, plan.amendments = old.locked_hash, old.review, old.amendments
            return plan
        plan.locked_hash = None                          # a writer cannot grant itself the lock
        objections = plan.review.get("objections", []) if role == "validator" else []
        plan.review = {"approved": False, "objections": objections,
                       "reviewer": plan.review.get("reviewer", "") if role == "validator" else ""}
        return plan

    # --- internals -----------------------------------------------------------------

    @staticmethod
    def _cls(name: str) -> type:
        if name not in _TYPES:
            raise ValueError(f"unknown artifact {name!r}. Known: {', '.join(_TYPES)}")
        return _TYPES[name]

    @staticmethod
    def _allow(name: str, role: str | None) -> None:
        r = _role(role)
        if r is not None and r not in _WRITERS[name]:
            raise ArtifactPermission(
                f"role {r!r} may not write {name}.json (writers: {', '.join(_WRITERS[name])})")

    def _dump(self, idea_id: str, name: str, artifact: Any) -> None:
        path = self.path(idea_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(encode_nonfinite(asdict(artifact)), indent=2, allow_nan=False) + "\n"
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# --- structural validators ------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w])")


def _norm(x: object) -> str:
    try:
        return f"{float(x):g}"          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(x)


def validate_spec(spec: Spec) -> list[str]:
    """Problems with a spec's *shape* (empty = valid)."""
    out = []
    for name in ("claim", "entry", "exit", "falsifier"):
        if not getattr(spec, name).strip():
            out.append(f"{name} is missing")
    if not spec.lookahead.strip():
        out.append("look-ahead statement is missing (what information each decision uses)")
    named = {_norm(n.get("value")) for n in spec.invented_numbers} | {
        _norm(p.get("value")) for p in spec.parameters}
    for name in ("entry", "exit", "sizing"):
        for num in _NUMBER.findall(getattr(spec, name)):
            if _norm(num) not in named:
                out.append(f"{name}: the number {num} is not named in invented_numbers "
                           "or parameters")
    if len(spec.parameters) > MAX_PARAMETERS and not spec.extra_parameters_justification.strip():
        out.append(f"{len(spec.parameters)} parameters declared (target ≤ {MAX_PARAMETERS}): "
                   "justify them in extra_parameters_justification or drop some")
    return out


def validate_plan(plan: Plan) -> list[str]:
    """Problems with a plan's *shape* (empty = valid)."""
    out = []
    if not plan.variants:
        out.append("the plan has no variants")
    seen: set[str] = set()
    for v in plan.variants:
        vid = v.get("id", "")
        if not vid:
            out.append("a variant has no id")
        elif vid in seen:
            out.append(f"duplicate variant id {vid!r}")
        seen.add(vid)
        axis = v.get("axis", "")
        if axis in _NUDGE_AXES:
            out.append(f"variant {vid}: {axis!r} is a parameter nudge, not an axis — "
                       f"vary one of: {', '.join(PLAN_AXES)}")
        elif axis not in PLAN_AXES:
            out.append(f"variant {vid}: axis {axis!r} is not one of {', '.join(PLAN_AXES)}")
        if not str(v.get("decision_rule", "")).strip():
            out.append(f"variant {vid}: decision_rule is missing")
    if plan.budget.get("max_extensions", 0) > MAX_EXTENSIONS:
        out.append(f"budget: max_extensions {plan.budget['max_extensions']} exceeds "
                   f"{MAX_EXTENSIONS}")
    return out
