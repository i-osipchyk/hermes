"""The one place Hermes meets llmkit, the platform's LLM usage and cost tracker.

Every live AI Advisor call is reported here with ``component="advisor"``, so it lands
in the cross-project spend log (``llmkit report``, Langfuse) next to every other
~/dev project, and is priced by llmkit's table — the only price table. See
``~/dev/platform/docs/llm-observability.md``.

The providers keep their own SDK clients (structured output differs per backend);
they hand back an :class:`LLMUsage`, and :class:`AIAdvisor` reports it because only
it knows the strategy and symbol the call was for.
"""

from __future__ import annotations

import os
from datetime import datetime

from llmkit import Tracker, Usage, cost

from .provider import LLMUsage

COMPONENT = "advisor"

_tracker: Tracker | None = None


def tracker() -> Tracker:
    """The process-wide Tracker, built on first use so env vars set after import count.

    ``LLMKIT_PROJECT`` defaults to ``hermes``; sinks follow llmkit's defaults
    (``~/.llmkit/usage.jsonl`` locally, plus Langfuse when its keys are set).
    """
    global _tracker
    if _tracker is None:
        _tracker = Tracker(project=os.getenv("LLMKIT_PROJECT") or "hermes")
    return _tracker


def usage_from(usage: Usage, *, started_at: datetime, latency_ms: float) -> LLMUsage:
    return LLMUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_write_tokens,
        latency_ms=latency_ms,
        started_at=started_at,
    )


def to_llmkit(usage: LLMUsage | None) -> Usage:
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_creation_tokens,
    )


def price(model_id: str, usage: LLMUsage, at: datetime) -> float | None:
    """USD cost by llmkit's table, or None when the model is not in it."""
    c = cost(model_id, to_llmkit(usage), usage.started_at or at)
    return None if c is None else c.total
