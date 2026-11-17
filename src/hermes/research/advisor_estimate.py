"""The AI Advisor's cost, estimated before it is spent (phase 4 §5).

Computed, never asked of an LLM: the number of signals the advisor would see (from an
advisor-off run of the same variant), the mean and p90 tokens per call from past billed calls
for that model, llmkit's price for the model (the only price table), minus signals the decision
cache already answers, as a low/high range multiplied across the planned advisor variants.

An estimate that cannot be a number says so in ``blocked`` — an unpriced model (the fix is a row
in llmkit's ``pricing.py``, a platform PR) or no token history — and ``ApprovalBook`` refuses to
approve it. Honest limit: a live 5-call sample for a never-used model is not implemented; the
caller supplies tokens per call instead, and the estimate says ``source: given``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from llmkit import Usage, cost

PLATFORM_FIX = ("add the model to llmkit's pricing.py (a PR in ~/dev/platform plus a tag bump "
                "in this project, CLAUDE.md)")
HEADROOM = 1.2


def history_tokens(ledger: Any, model: str) -> list[tuple[int, int]]:
    """(input, output) tokens of every past billed advisor call for ``model``."""
    rows = []
    for entry in ledger.entries():
        rec = ledger.load(entry.key)
        log = getattr(rec, "llm_log", None)
        for call in getattr(log, "calls", []) or []:
            if (call.model_id == model and not call.from_decision_cache and not call.is_error
                    and call.input_tokens is not None and call.output_tokens is not None):
                rows.append((int(call.input_tokens), int(call.output_tokens)))
    return rows


def _p90(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]


def _price(model: str, tokens_in: int, tokens_out: int, at: datetime) -> float | None:
    c = cost(model, Usage(input_tokens=tokens_in, output_tokens=tokens_out), at)
    return None if c is None else c.total


def estimate_advisor(*, model: str, signals: int, cached: int = 0, variants: int = 1,
                     history: list[tuple[int, int]] | None = None,
                     given_tokens: tuple[int, int] | None = None,
                     at: datetime | None = None) -> dict:
    """The estimate as a JSON-able dict (it becomes ``Approval.estimate``)."""
    at = at or datetime.now(UTC)
    calls = max(0, signals - cached)
    est: dict[str, Any] = {"model": model, "signals": signals, "cached": cached, "calls": calls,
                           "variants": variants, "source": "", "tokens_in": None,
                           "tokens_out": None, "low": None, "high": None, "blocked": ""}
    if history:
        mean_in = round(sum(i for i, _ in history) / len(history))
        mean_out = round(sum(o for _, o in history) / len(history))
        hi_in, hi_out = _p90([i for i, _ in history]), _p90([o for _, o in history])
        est.update(source=f"history ({len(history)} billed calls)")
    elif given_tokens:
        mean_in, mean_out = hi_in, hi_out = given_tokens
        est.update(source="given")
    else:
        est["source"] = "none"
        est["blocked"] = (f"no billed advisor calls on record for {model} and no tokens per call "
                          "given: pass --tokens-in/--tokens-out (or run one billed variant)")
        return est
    est.update(tokens_in=mean_in, tokens_out=mean_out)
    low_each, high_each = _price(model, mean_in, mean_out, at), _price(model, hi_in, hi_out, at)
    if low_each is None or high_each is None:
        est["blocked"] = f"{model} is unpriced in llmkit: {PLATFORM_FIX}"
        return est
    est["low"] = round(calls * low_each * variants, 4)
    est["high"] = round(calls * high_each * variants * HEADROOM, 4)
    return est


def spent_for_idea(ledger: Any, idea: str, model: str | None = None) -> float:
    """USD already billed to advisor calls on ``idea``'s runs (llmkit's price, as recorded)."""
    total = 0.0
    for entry in ledger.entries():
        if entry.meta.idea != idea:
            continue
        rec = ledger.load(entry.key)
        for call in getattr(getattr(rec, "llm_log", None), "calls", []) or []:
            if call.cost_usd and (model is None or call.model_id == model):
                total += float(call.cost_usd)
    return total


class AdvisorRefused(RuntimeError):
    """A run with an advisor lacks an approval that covers it."""


def check_advisor_run(approval: Any, *, idea: str, model: str, spent: float) -> None:
    """Raise ``AdvisorRefused`` unless ``approval`` is an approved advisor approval for this
    idea and model whose estimated ``high`` has not been exceeded by what is already spent."""
    if approval is None or approval.kind != "advisor":
        raise AdvisorRefused("an advisor run needs --advisor-approval <id> of an advisor approval")
    if approval.idea != idea:
        raise AdvisorRefused(f"approval {approval.id} is for idea {approval.idea}, not {idea}")
    if approval.status != "approved":
        raise AdvisorRefused(f"approval {approval.id} is {approval.status}, not approved")
    est = approval.estimate or {}
    if est.get("model") != model:
        raise AdvisorRefused(f"approval {approval.id} covers {est.get('model')!r}, this run "
                             f"uses {model!r}: a different model needs a new approval")
    if est.get("high") is not None and spent >= est["high"]:
        raise AdvisorRefused(f"${spent:.4f} is already spent against approval {approval.id} "
                             f"(estimated high ${est['high']:.4f}): the budget is used")


def reestimate(approval: Any, spent: float) -> dict:
    """The figure for a fresh approval when a run has used up the old one: what is already
    spent plus another budget of the same size, for the owner to accept or refuse."""
    old = dict(approval.estimate or {})
    for k in ("low", "high"):
        if old.get(k) is not None:
            old[k] = round(spent + old[k], 4)
    old["spent_before"] = round(spent, 4)
    old["supersedes"] = approval.id
    return old
