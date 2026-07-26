"""Deterministic, content-addressed cache for Advisor Decisions (ADR-0005).

Keyed on a hash of (model id, system prompt, user prompt). The first backtest makes
one real call per unique candidate; re-runs read the cache — preserving the pure
`(params, data) -> result` contract despite LLM non-determinism, and doubling as a
record/replay fixture for tests. Invalidation is automatic: changing the model id,
prompt template, or input assembly changes the key.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .provider import AdvisorDecision

DEFAULT_CACHE_DIR = Path(".hermes_cache/ai")


class DecisionCache:
    def __init__(self, root: Path = DEFAULT_CACHE_DIR) -> None:
        self.root = root

    @staticmethod
    def key(model_id: str, system_prompt: str, user_prompt: str) -> str:
        payload = json.dumps([model_id, system_prompt, user_prompt], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> AdvisorDecision | None:
        path = self._path(key)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return AdvisorDecision(
            approved=data["approved"],
            confidence=data["confidence"],
            reason=data["reason"],
            model_id=data["model_id"],
        )

    def put(self, key: str, decision: AdvisorDecision) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "approved": decision.approved,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "model_id": decision.model_id,
        }
        tmp = self._path(key + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self._path(key))
