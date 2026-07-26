"""AIProvider: the pluggable model backend (ADR-0005).

Providers map an assembled prompt to a structured :class:`AdvisorDecision`. Claude
is the default (:mod:`hermes.ai.claude`); the interface stays small so OpenAI/local
backends can be added without touching the Advisor or Strategy code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AdvisorDecision:
    """Structured result of an AI Advisor call, recorded per Trade for audit."""

    approved: bool
    confidence: float          # 0..1
    reason: str
    model_id: str              # recorded so backtest/live divergence is detectable


class AIProvider(ABC):
    #: Stable identifier folded into the cache key and recorded on decisions.
    model_id: str

    @abstractmethod
    def decide(self, system_prompt: str, user_prompt: str) -> AdvisorDecision:
        """Return an approve/veto decision. Implementations should request
        structured output and temperature 0 for stability."""
