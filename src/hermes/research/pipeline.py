"""The state machine, as a pure function of what is on disk.

``next_action(idea, store)`` says which role does what next, from the idea's status and
which artifacts exist. It reads, never writes, so calling it twice changes nothing and a
crashed run resumes by calling it again — the property phase 4's driver relies on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import validate_spec, variants_hash
from .dev_flow import read_extensions
from .thesis import DEFAULT_THESIS_PATH, due


@dataclass(frozen=True)
class Action:
    role: str                      # researcher | quant | validator | developer | driver | owner
    step: str
    requires: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"role": self.role, "step": self.step, "requires": list(self.requires),
                "produces": list(self.produces)}


def _locked(plan: Any) -> bool:
    return bool(plan and plan.review.get("approved") is True
                and plan.locked_hash == variants_hash(plan.variants))


def _sent_back(plan: Any) -> bool:
    """The Validator wrote objections instead of approving: the Quant revises."""
    return bool(plan and not _locked(plan) and plan.review.get("objections"))


def next_action(idea: Any, store: Any, *, ran_variants: set[str] | None = None) -> Action | None:
    """The next step for ``idea``, or None when nothing is waiting on any role."""
    s = idea.status
    spec = store.read(idea.id, "spec")
    plan = store.read(idea.id, "plan")
    lessons = store.read(idea.id, "lessons")
    validation = store.read(idea.id, "validation")

    if s in ("raw", "literature_done"):
        if spec is None or validate_spec(spec):
            return Action("researcher", "write_spec", (), ("spec.json",))
        return Action("researcher", "mark_quantified", ("spec.json",), ())
    if s in ("quantified", "coded"):
        if plan is None or _sent_back(plan):
            return Action("quant", "write_plan", ("spec.json",), ("plan.json",))
        if not _locked(plan):
            return Action("validator", "review_plan", ("plan.json",), ("plan.json",))
        if s == "quantified":
            if not idea.strategy:
                return Action("quant", "write_strategy", ("spec.json", "plan.json"), ())
            return Action("quant", "mark_coded", (), ())
        return Action("quant", "mark_testing", ("plan.json",), ())
    if s == "needs_infra":
        exts = read_extensions(store.root, idea.id)
        if exts and exts[-1]["status"] == "merged":
            return Action("driver", "resume_from_infra", ("extension.json",), ())
        return Action("developer", "build_infra", (), ("extension.json",))
    if s == "testing":
        if plan is not None and _sent_back(plan):
            return Action("quant", "write_plan", ("spec.json",), ("plan.json",))
        if plan is not None and not _locked(plan):      # amended: reviewed again before any run
            return Action("validator", "review_plan", ("plan.json",), ("plan.json",))
        planned = [v.get("id") for v in plan.variants] if plan else []
        unrun = [v for v in planned if v not in (ran_variants or set())]
        verdict = validation.verdict if validation else ""
        if verdict == "reject":
            return Action("driver", "reject_idea", ("validation.json",), ())
        if verdict == "ready_for_unlock":
            return Action("driver", "unlock_holdout", ("validation.json",), ("holdout.json",))
        if unrun:
            return Action("quant", "run_variants", ("plan.json",), ("trials.json",))
        if verdict == "continue":
            return Action("quant", "amend_plan", ("validation.json",), ("plan.json",))
        return Action("validator", "validate", ("trials.json",), ("validation.json",))
    if s == "awaiting_approval":
        return Action("owner", "answer_approval", (), ())
    if s in ("validated", "rejected"):
        if lessons is None:
            return Action("researcher", "reflect", (), ("lessons.json",))
        if s == "validated":
            return Action("owner", "approve_forward_test", ("lessons.json",), ())
    return None


def ran_variants(ledger: Any, idea_id: str) -> set[str]:
    return {e.meta.variant for e in ledger.entries()
            if e.meta.idea == idea_id and e.meta.variant}


def status_report(book: Any, store: Any, approvals: Any, ledger: Any,
                  thesis_path: Path = DEFAULT_THESIS_PATH) -> dict:
    """The backlog by status, what waits on the owner, what needs reflecting, the thesis, and
    each open idea's next action."""
    ideas = book.all()
    decided = ("validated", "rejected", "ready_to_forward_test")
    open_rows = []
    for idea in sorted(ideas, key=lambda i: (i.priority is None, i.priority or 0,
                                             i.created_at or "", i.id)):
        if not idea.is_open and idea.status != "validated":
            continue
        act = next_action(idea, store, ran_variants=ran_variants(ledger, idea.id))
        if act is not None:
            open_rows.append({"idea": idea.id, "title": idea.title, "status": idea.status,
                              "next_action": act.to_dict()})
    return {
        "by_status": book.pipeline(),
        "pending_approvals": [
            {"id": a.id, "idea": a.idea, "kind": a.kind, "question": a.question,
             "recommendation": a.recommendation}
            for a in approvals.all(pending_only=True)],
        "awaiting_reflection": [i.id for i in ideas if i.status in decided
                                and store.read(i.id, "lessons") is None],
        "thesis_due": due(book, store, thesis_path),
        "open": open_rows,
    }
