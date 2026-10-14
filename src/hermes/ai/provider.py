"""AIProvider: the pluggable model backend (ADR-0005).

Providers map an assembled prompt to a structured :class:`AdvisorDecision`. Claude
is the default (:mod:`hermes.ai.claude`); the interface stays small so OpenAI/local
backends can be added without touching the Advisor or Strategy code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token counts and timing from a live API response.

    Split the way llmkit prices them: ``input_tokens`` is *uncached* input only, so
    a provider whose prompt count includes cache hits (DeepSeek, OpenAI) must
    subtract them — ``llmkit.Usage.from_openai`` does.
    """

    input_tokens: int            # uncached input
    output_tokens: int
    cache_read_tokens: int       # input served from the provider's prompt cache
    cache_creation_tokens: int   # input written to the prompt cache (Anthropic)
    latency_ms: float
    # When the request started (UTC). DeepSeek prices by time of day, so cost
    # depends on it; None falls back to when the advisor was asked.
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AdvisorDecision:
    """Structured result of an AI Advisor call, recorded per Trade for audit."""

    approved: bool
    confidence: float          # 0..1
    reason: str
    model_id: str              # recorded so backtest/live divergence is detectable
    is_error: bool = False     # True when the decision is a fail-open due to a provider error; never cached
    prompt: str = ""           # assembled user prompt (set after cache lookup; not stored in cache)
    from_cache: bool = False   # True when served from the decision cache; False for a live API call
    usage: LLMUsage | None = None  # populated only for live API calls


class AIProvider(ABC):
    #: Stable identifier folded into the cache key and recorded on decisions.
    model_id: str
    #: Billing backend name reported to llmkit ("anthropic", "deepseek"). None for
    #: stubs and local models, whose calls cost nothing and are not reported.
    llm_provider: str | None = None

    @abstractmethod
    def decide(self, system_prompt: str, user_prompt: str) -> AdvisorDecision:
        """Return an approve/veto decision. Implementations should request
        structured output and temperature 0 for stability."""
