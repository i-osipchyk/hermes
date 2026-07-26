"""ClaudeProvider: the default AI Advisor backend (Anthropic SDK).

Uses tool-use / structured output for a reliable approve/veto decision, prompt
caching on the static system prompt to cut cost, and temperature 0 for stability
(the DecisionCache guarantees full reproducibility regardless). Import-guarded so
the core library does not hard-depend on ``anthropic``.
"""

from __future__ import annotations

from .provider import AdvisorDecision, AIProvider

# Default to the latest capable model; override via constructor.
DEFAULT_MODEL = "claude-opus-4-8"

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
    def __init__(self, model_id: str = DEFAULT_MODEL, max_tokens: int = 1024) -> None:
        self.model_id = model_id
        self.max_tokens = max_tokens
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
        client = self._get_client()
        response = client.messages.create(
            model=self.model_id,
            max_tokens=self.max_tokens,
            temperature=0,
            # Prompt-cache the static system prompt to cut cost across many calls.
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=[_DECISION_TOOL],
            tool_choice={"type": "tool", "name": "record_decision"},
            messages=[{"role": "user", "content": user_prompt}],
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_decision":
                data = block.input
                return AdvisorDecision(
                    approved=bool(data["approved"]),
                    confidence=float(data["confidence"]),
                    reason=str(data["reason"]),
                    model_id=self.model_id,
                )
        # Fail safe: if the model returned no structured decision, approve (the gate
        # only ever *blocks*; a malformed response should not silently kill trades).
        return AdvisorDecision(True, 0.0, "no structured decision returned", self.model_id)
