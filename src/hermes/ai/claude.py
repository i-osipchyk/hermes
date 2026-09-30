"""ClaudeProvider: the default AI Advisor backend (Anthropic SDK).

Uses tool-use / structured output for a reliable approve/veto decision and prompt
caching on the static system prompt to cut cost. Sampling params are left at the
API default (a forced ``tool_choice`` disallows ``temperature``); reproducibility
comes from the DecisionCache, not from sampling. Import-guarded so the core library
does not hard-depend on ``anthropic``.
"""

from __future__ import annotations

import os

from .provider import AdvisorDecision, AIProvider, LLMUsage

# Default to the latest capable model. Override per-instance via the constructor,
# or globally via HERMES_AI_MODEL. (Read at call time, not import time, so setting
# the variable after `import hermes` still takes effect.)
DEFAULT_MODEL = "claude-opus-5"


def _default_model() -> str:
    return os.getenv("HERMES_AI_MODEL") or DEFAULT_MODEL


def _default_max_tokens() -> int:
    return int(os.getenv("HERMES_AI_MAX_TOKENS", "1024"))


_DECISION_TOOL = {
    "name": "record_decision",
    "description": "Record whether to approve or veto the proposed trade.",
    "input_schema": {
        "type": "object",
        "properties": {
            "approved": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
        "required": ["approved", "confidence", "reason"],
    },
}


class ClaudeProvider(AIProvider):
    def __init__(self, model_id: str | None = None, max_tokens: int | None = None) -> None:
        self.model_id = model_id or _default_model()
        self.max_tokens = max_tokens or _default_max_tokens()
        self._client = None  # lazy: import anthropic on first use

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "ClaudeProvider needs the 'anthropic' package. Install with "
                    "pip install 'hermes[ai]'."
                ) from e
            self._client = anthropic.Anthropic()
        return self._client

    def decide(self, system_prompt: str, user_prompt: str) -> AdvisorDecision:
        import time

        import anthropic
        client = self._get_client()
        t0 = time.perf_counter()
        try:
            response = client.messages.create(
                model=self.model_id,
                max_tokens=self.max_tokens,
                # temperature omitted: not allowed when tool_choice forces a specific tool.
                # Reproducibility is guaranteed by the DecisionCache regardless.
                # Prompt-cache the static system prompt to cut cost across many calls.
                system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
                tools=[_DECISION_TOOL],
                tool_choice={"type": "tool", "name": "record_decision"},
                messages=[{"role": "user", "content": user_prompt}],
            )
        except anthropic.APIStatusError as e:
            import warnings
            warnings.warn(
                f"ClaudeProvider: API error {e.status_code} — approving trade by default. {e.message}",
                RuntimeWarning,
                stacklevel=2,
            )
            return AdvisorDecision(
                True, 0.0, f"API error {e.status_code}: {e.message}",
                self.model_id, is_error=True,
            )
        except anthropic.APIConnectionError as e:
            import warnings
            warnings.warn(
                f"ClaudeProvider: connection error — approving trade by default. {e}",
                RuntimeWarning,
                stacklevel=2,
            )
            return AdvisorDecision(True, 0.0, f"connection error: {e}", self.model_id, is_error=True)
        latency_ms = (time.perf_counter() - t0) * 1000
        usage = LLMUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            latency_ms=latency_ms,
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_decision":
                data = block.input
                return AdvisorDecision(
                    approved=bool(data["approved"]),
                    confidence=float(data["confidence"]),
                    reason=str(data["reason"]),
                    model_id=self.model_id,
                    usage=usage,
                )
        # Fail safe: if the model returned no structured decision, approve (the gate
        # only ever *blocks*; a malformed response should not silently kill trades).
        return AdvisorDecision(True, 0.0, "no structured decision returned", self.model_id, usage=usage)
