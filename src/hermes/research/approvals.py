"""Approvals — the owner's decisions, and the idea parked on each.

A role that reaches something only the owner may decide (spending on the AI Advisor, a
change of instrument, a change to engine semantics, merging a Developer branch, forward
testing a validated idea) *parks* the idea: one library call creates the approval and moves
the idea to ``awaiting_approval``, remembering where to resume. The owner answers, in a batch
if they like, by reading ``hermes pipeline approvals``; the answer moves the idea on.

Only the owner answers: ``HERMES_ROLE`` must be unset or ``owner``. Same honest limit as the
artifact roles (ADR-0012) — an env var — made visible, not impossible, by ``history``.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

from .ideas import IdeaBook
from .jsonsafe import decode_nonfinite, encode_nonfinite

DEFAULT_APPROVALS_PATH = Path("research/approvals.json")
KINDS = ("advisor", "instrument_change", "engine_semantics", "dev_merge", "forward_test", "other")
# Denying these sends the idea back to where it was: the pipeline keeps moving without the
# advisor, and a validated idea stays validated.
_RESUME_ON_DENY = ("advisor", "forward_test")


class ApprovalPermission(PermissionError):
    """An agent role tried to answer an approval."""


@dataclass
class Approval:
    id: str
    idea: str
    kind: str
    question: str
    recommendation: str
    evidence: list[str] = field(default_factory=list)       # paths
    estimate: dict | None = None                            # phase 4 fills it for `advisor`
    branch: str | None = None                               # for dev_merge
    status: str = "pending"                                 # pending | approved | denied
    created_at: str = ""
    answered_at: str | None = None
    answered_by: str | None = None
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> Approval:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in decode_nonfinite(d).items() if k in known})


@dataclass
class ApprovalBook:
    path: Path
    ideas: IdeaBook

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    # --- storage -------------------------------------------------------------

    def _load(self) -> list[Approval]:
        if not self.path.exists():
            return []
        return [Approval.from_dict(d) for d in json.loads(self.path.read_text() or "[]")]

    def _save(self, items: list[Approval]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(encode_nonfinite([asdict(a) for a in items]), indent=2,
                          allow_nan=False) + "\n"
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, self.path)

    def all(self, *, pending_only: bool = False, idea: str | None = None) -> list[Approval]:
        out = self._load()
        if pending_only:
            out = [a for a in out if a.status == "pending"]
        if idea:
            out = [a for a in out if a.idea == idea]
        return out

    def get(self, id_or_prefix: str) -> Approval:
        items = self._load()
        hits = [a for a in items if a.id == id_or_prefix] or [
            a for a in items if a.id.startswith(id_or_prefix)]
        if not hits:
            raise KeyError(f"no approval matching {id_or_prefix!r}")
        if len(hits) > 1:
            raise LookupError(f"{id_or_prefix!r} matches {len(hits)} approvals")
        return hits[0]

    # --- the two operations ----------------------------------------------------

    def park(self, idea: str, kind: str, question: str, recommendation: str, *,
             evidence: list[str] | None = None, estimate: dict | None = None,
             branch: str | None = None) -> Approval:
        """Create the approval and park the idea on it (one history entry)."""
        if kind not in KINDS:
            raise ValueError(f"unknown approval kind {kind!r}. Known: {', '.join(KINDS)}")
        target = self.ideas.get(idea)
        if target.status == "awaiting_approval":
            raise ValueError(f"idea {target.id} is already awaiting approval {target.approval_id}")
        approval = Approval(
            id=uuid.uuid4().hex[:8], idea=target.id, kind=kind, question=question,
            recommendation=recommendation, evidence=list(evidence or []), estimate=estimate,
            branch=branch, created_at=datetime.now(UTC).isoformat())
        # park first: if the idea cannot be parked, no approval is left behind
        self.ideas.update(target.id, status="awaiting_approval", approval_id=approval.id,
                          reason=f"{kind}: {question}")
        self._save([*self._load(), approval])
        return approval

    def answer(self, id_or_prefix: str, decision: str, note: str = "", *,
               deny_to: str | None = None, by: str | None = None) -> Approval:
        """The owner's decision. ``approved`` resumes the idea (``forward_test`` makes it
        ``ready_to_forward_test``); ``denied`` returns it (advisor, forward_test) or parks it,
        or rejects it when ``deny_to="rejected"``."""
        role = os.environ.get("HERMES_ROLE")
        if role not in (None, "", "owner"):
            raise ApprovalPermission(f"role {role!r} cannot answer an approval; only the owner can")
        if decision not in ("approved", "denied"):
            raise ValueError(f"unknown decision {decision!r}: approved or denied")
        if deny_to not in (None, "parked", "rejected"):
            raise ValueError("deny_to is parked or rejected")
        items = self._load()
        approval = self.get(id_or_prefix)
        if approval.status != "pending":
            raise ValueError(f"approval {approval.id} was already {approval.status}")
        idea = self.ideas.get(approval.idea)
        why = f"{approval.kind} {decision}: {note}".rstrip(": ")

        if decision == "approved" and approval.kind == "advisor" and (
                approval.estimate is None or approval.estimate.get("blocked")
                or approval.estimate.get("high") is None):
            why = (approval.estimate or {}).get("blocked") or "it has no estimate"
            raise ValueError(f"approval {approval.id} cannot be approved on a number: {why}")
        if decision == "approved":
            if approval.kind == "forward_test":
                approval.status = "approved"
                self.ideas.update(idea.id, status="ready_to_forward_test", reason=why,
                                  approval=approval)
            else:
                self.ideas.update(idea.id, status=idea.resume_status or "parked", reason=why)
        elif deny_to == "rejected":
            self.ideas.update(idea.id, status="rejected", reason=why, verdict=note or why,
                              failure={"kind": "other", "assumption": "owner decision",
                                       "detail": note})
        elif deny_to == "parked" or approval.kind not in _RESUME_ON_DENY:
            self.ideas.update(idea.id, status="parked", reason=why)
        else:
            self.ideas.update(idea.id, status=idea.resume_status or "parked", reason=why)

        approval.status = decision
        approval.answered_at = datetime.now(UTC).isoformat()
        approval.answered_by = by or "owner"
        approval.note = note
        self._save([approval if a.id == approval.id else a for a in items])
        return approval


