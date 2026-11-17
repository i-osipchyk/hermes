"""The Validator's mechanical half (phase 4 §3): everything in ``validation.json`` that can be
computed is computed here, so the Validator interprets and does not calculate.

Reads the stored ``analysis.json`` of the best trial cluster, the trial registry, the holdout
record and the reads audit. An axis that did not run is listed with the command that would
measure it — never presented as clean (CLAUDE.md). The **recommendation** is the in-sample gate
(``unlock.evaluate_gate``'s thresholds) applied to those numbers; the Validator may only be as
cautious as it or more (``ArtifactStore`` enforces that), so a thin model cannot talk itself
into ``ready_for_unlock`` against the numbers.
"""

from __future__ import annotations

import json
from typing import Any

from .analysis import ALL_AXES
from .artifacts import Validation
from .pipeline import ran_variants
from .reads import audit_informed_by
from .unlock import MAX_NULL_P, MIN_DSR, PASSING_TIERS

REQUIRED_AXES = ("validation", "deflation", "null")


def _holdout_state(store: Any, idea_id: str) -> str:
    path = store.root / idea_id / "holdout.json"
    if not path.exists():
        return "locked"
    rec = json.loads(path.read_text())
    if rec.get("state") == "done":
        return "unlocked:passed" if rec.get("passed") else "unlocked:failed"
    return "unlocked:started"


def _plan_check(idea_id: str, plan: Any, ledger: Any, registry: Any) -> dict:
    planned = [v["id"] for v in plan.variants] if plan else []
    ran = ran_variants(ledger, idea_id)
    variant_of = {e.key: e.meta.variant for e in ledger.entries() if e.meta.idea == idea_id}
    shared = []
    for c in registry.clusters(idea_id):
        vs = sorted({variant_of.get(k) for k in c.keys if variant_of.get(k)})
        if len(vs) > 1:
            shared.append(vs)
    return {"planned_not_run": [v for v in planned if v not in ran],
            "run_not_planned": sorted(ran - set(planned)),
            "variants_sharing_a_cluster": shared}


def fill_validation(idea_id: str, *, book: Any, store: Any, ledger: Any, registry: Any,
                    reads: Any) -> Validation:
    idea = book.get(idea_id)
    plan = store.read(idea.id, "plan")
    eff = registry.effective_trials(idea.id)
    clusters = registry.clusters(idea.id)
    audit = audit_informed_by(book, reads, idea.id)

    best_key, best_sharpe = "", None
    for c in clusters:
        rec = ledger.load(c.representative)
        sharpe = (rec.metrics.get("sharpe") if rec else None)
        if sharpe is not None and (best_sharpe is None or sharpe > best_sharpe):
            best_key, best_sharpe = c.representative, sharpe

    analysis: dict = {}
    if best_key:
        path = ledger.path / best_key / "analysis.json"
        if path.exists():
            analysis = json.loads(path.read_text())
    axes = {a["axis"]: a for a in analysis.get("axes", [])}
    pointers = {name: {"run": best_key, "ran": a.get("ran", False), "note": a.get("note", ""),
                       "error": a.get("error")} for name, a in axes.items()}
    not_measured = [{"axis": name, "command": f"hermes analyze {best_key or '<run-key>'} "
                                              f"--axes {name} --idea {idea.id}"}
                    for name in ALL_AXES if not axes.get(name, {}).get("ran")]

    def data(name: str) -> dict:
        a = axes.get(name, {})
        return a.get("data", {}) if a.get("ran") else {}

    tier = data("validation").get("sample_quality", {}).get("tier", "")
    dsr, null_p = data("deflation").get("dsr"), data("null").get("p_value")
    holdout = _holdout_state(store, idea.id)

    rec_, why = _recommend(clusters, best_sharpe, axes, tier, dsr, null_p, audit,
                           (plan.budget.get("max_clusters", 5) if plan else 5), idea.id,
                           best_key, holdout)
    return Validation(
        recommendation=rec_, recommendation_reason=why, candidate_run=best_key, tier=tier,
        n_eff=float(eff.n_eff), dsr=dsr, null_p=null_p, holdout_state=holdout, axes=pointers,
        not_measured=not_measured, read_not_declared=list(audit["read_not_declared"]),
        plan_check=_plan_check(idea.id, plan, ledger, registry))


def _recommend(clusters: list, best_sharpe: float | None, axes: dict, tier: str,
               dsr: float | None, null_p: float | None, audit: dict, max_clusters: int,
               idea_id: str, key: str, holdout: str) -> tuple[str, str]:
    if holdout != "locked":
        return "reject" if holdout == "unlocked:failed" else "continue", \
            f"the holdout is already {holdout}"
    if not clusters or best_sharpe is None:
        return "continue", "no trials have been recorded for this idea"
    missing = [a for a in REQUIRED_AXES if not axes.get(a, {}).get("ran")]
    if missing:
        cmds = "; ".join(f"hermes analyze {key} --axes {a} --idea {idea_id}" for a in missing)
        return "continue", f"not measured: {', '.join(missing)} ({cmds})"
    failed = []
    if tier not in PASSING_TIERS:
        failed.append(f"tier {tier!r} (need one of {PASSING_TIERS})")
    if dsr is None or dsr < MIN_DSR:
        failed.append(f"DSR {dsr} (need >= {MIN_DSR})")
    if null_p is None or null_p >= MAX_NULL_P:
        failed.append(f"null p {null_p} (need < {MAX_NULL_P})")
    if not failed:
        if audit["read_not_declared"]:
            return "continue", ("passes the gate, but the idea read results it did not declare "
                                f"informed_by: {', '.join(audit['read_not_declared'])}")
        return "ready_for_unlock", "the in-sample gate passes"
    if best_sharpe <= 0:
        return "reject", f"no cluster has a positive Sharpe (best {best_sharpe:.2f}); " + \
            "; ".join(failed)
    if len(clusters) >= max_clusters:
        return "reject", f"the cluster budget ({max_clusters}) is spent; " + "; ".join(failed)
    return "continue", "the gate fails: " + "; ".join(failed)
