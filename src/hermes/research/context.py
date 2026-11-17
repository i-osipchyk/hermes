"""The context pack — what one iteration needs, in one command.

A fresh session starts from this instead of reading every idea record: the idea, its spec,
the plan joined to the runs that executed it, the trial clusters and where each count came
from, the validator's view and the holdout state, the lessons of its parent and of the ideas
that informed it, the part of the thesis that concerns its tags, and its open approvals.
Deterministic (no clock, sorted keys) and size-capped (``render_context``).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .data_check import read_data_check
from .thesis import DEFAULT_THESIS_PATH, read_thesis

HISTORY_TAIL = 5
_METRICS = ("sharpe", "num_trades", "total_return", "max_drawdown", "win_rate")


def build_context(idea_id: str, *, book: Any, store: Any, ledger: Any, registry: Any,
                  approvals: Any, reads: Any,
                  thesis_path: Path = DEFAULT_THESIS_PATH) -> dict:
    idea = book.get(idea_id)
    record = idea.to_dict()
    record["history"] = record["history"][-HISTORY_TAIL:]
    spec = store.read(idea.id, "spec")
    plan = store.read(idea.id, "plan")
    validation = store.read(idea.id, "validation")

    runs = [e for e in ledger.entries() if e.meta.idea == idea.id]
    plan_section = None
    if plan is not None:
        variants = []
        for v in plan.variants:
            keys = sorted(e.key for e in runs if e.meta.variant == v.get("id"))
            variants.append({**v, "ran": bool(keys),
                             "runs": [{"key": k, "note": next(
                                 e.meta.note for e in runs if e.key == k)} for k in keys]})
        plan_section = {"variants": variants, "budget": plan.budget, "review": plan.review,
                        "locked": bool(plan.locked_hash)}

    eff = registry.effective_trials(idea.id)
    clusters = []
    for c in registry.clusters(idea.id):
        rec = ledger.load(c.representative)
        metrics = {k: rec.metrics.get(k) for k in _METRICS if rec and k in rec.metrics}
        clusters.append({"representative": c.representative, "keys": c.keys,
                         "metrics": metrics})
    declared = set(book.get(idea.id).informed_by)
    trials = {
        "own": eff.own, "inherited": dict(eff.inherited), "batch_floor": eff.batch_floor,
        "n_eff": eff.n_eff, "clusters": clusters,
        "inherited_from": {i: ("declared" if i in declared else "read")
                           for i in eff.inherited},
    }

    holdout_path = store.root / idea.id / "holdout.json"
    holdout: dict = {"state": "none"}
    if holdout_path.exists():
        h = json.loads(holdout_path.read_text())
        holdout = {k: h.get(k) for k in ("state", "passed", "window", "required_psr")
                   if k in h}

    sources = [idea.parent] if idea.parent else []
    sources += [i for i in registry.informing(idea.id) if i not in sources]
    lessons = []
    for other in sources:
        les = store.read(other, "lessons")
        if les is not None:
            title = book.get(other).title
            lessons.append({"idea": other, "title": title, **{
                k: getattr(les, k) for k in ("outcome", "failure", "would_change_my_mind",
                                             "next_hypotheses", "surprises", "infra_gaps")}})

    return {
        "idea": record,
        "spec": asdict(spec) if spec else None,
        "data": read_data_check(store.root, idea.id),
        "plan": plan_section,
        "trials": trials,
        "validation": asdict(validation) if validation else None,
        "holdout": holdout,
        "lessons": lessons,
        "approvals": [asdict(a) for a in approvals.all(pending_only=True, idea=idea.id)],
        "thesis": read_thesis(thesis_path).excerpt(idea.tags),
    }


def render_context(ctx: dict, max_chars: int = 12_000) -> str:
    """Sections in priority order; the lowest-priority ones are clipped first."""
    parts: list[str] = []
    for name, body in ctx.items():
        text = body if isinstance(body, str) else json.dumps(body, indent=2, sort_keys=True,
                                                            default=str)
        parts.append(f"## {name}\n{text if text and text != 'null' else '(none)'}\n")
    out = ""
    marker = "\n[…truncated]"
    for part in parts:
        if len(out) + len(part) <= max_chars:
            out += part + "\n"
            continue
        room = max_chars - len(out) - len(marker)
        if room > 40:
            out += part[:room] + marker
        elif out:
            out = out[:max_chars - len(marker)] + marker
        break
    return out.rstrip("\n")[:max_chars]
