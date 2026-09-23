"""RandomAdvisor: a duck-typed drop-in for AIAdvisor that approves with probability p.

Used by the random baseline simulator to test whether the AI's specific signal
selection adds edge, or whether any random p% filter would produce equivalent returns.
"""

from __future__ import annotations

import random

from .observability import LLMObservabilityLog
from .provider import AdvisorDecision


class RandomAdvisor:
    """Approves each signal with probability p, using a seeded RNG for reproducibility."""

    min_confidence: float = 0.0

    def __init__(self, p: float = 0.05, seed: int | None = None):
        self._p = p
        self._rng = random.Random(seed)
        self.obs_log = LLMObservabilityLog()

    def evaluate(self, strategy, order, prompt: str) -> AdvisorDecision:  # noqa: ARG002
        approved = self._rng.random() < self._p
        return AdvisorDecision(
            approved=approved,
            confidence=self._p if approved else 1.0 - self._p,
            reason=f"random baseline (p={self._p:.3f})",
            model_id=f"random-p{self._p:.3f}",
        )
