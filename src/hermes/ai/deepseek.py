"""DeepSeekProvider: AI Advisor backend using DeepSeek's OpenAI-compatible API.

Regular models (deepseek-chat, deepseek-v3) use function calling for structured
output. Reasoning/thinking models (deepseek-reasoner, deepseek-r1, deepseek-flash)
don't support forced tool_choice, so they use JSON mode with the schema embedded
in the system prompt instead.
"""

from __future__ import annotations

import os

from .provider import AdvisorDecision, AIProvider, LLMUsage

DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
_BASE_URL = "https://api.deepseek.com"

# Models whose names match these substrings are reasoning models that don't
# support forced tool_choice.
_REASONING_MODEL_PATTERNS = ("reasoner", "r1", "flash")

_DECISION_TOOL = {
    "type": "function",
    "function": {
        "name": "record_decision",
        "description": "Record whether to approve or veto the proposed trade.",
        "parameters": {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
            "required": ["approved", "confidence", "reason"],
        },
    },
}

_JSON_SCHEMA_SUFFIX = """

Respond with a JSON object and nothing else:
{"approved": <true|false>, "confidence": <0.0-1.0>, "reason": "<one sentence>"}"""


def _strip_code_fence(text: str) -> str:
    """Remove markdown ```json ... ``` or ``` ... ``` wrappers if present."""
    import re
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)```$", text)
    return m.group(1).strip() if m else text


def _is_reasoning_model(model_id: str) -> bool:
    lower = model_id.lower()
    return any(p in lower for p in _REASONING_MODEL_PATTERNS)


class DeepSeekProvider(AIProvider):
    def __init__(self, model_id: str = DEFAULT_MODEL, max_tokens: int | None = None) -> None:
        self.model_id = model_id
        self.max_tokens = max_tokens or int(os.getenv("MAX_TOKENS", "1024"))
        self._client = None  # lazy: import openai on first use

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "DeepSeekProvider needs the 'openai' package. Install with "
                    "pip install openai."
                ) from e
            api_key = os.getenv("DEEPSEEK_API_KEY", "")
            if not api_key:
                raise ValueError(
                    "DeepSeekProvider requires DEEPSEEK_API_KEY to be set in the environment."
                )
            self._client = OpenAI(api_key=api_key, base_url=_BASE_URL)
        return self._client

    def decide(self, system_prompt: str, user_prompt: str) -> AdvisorDecision:
        if _is_reasoning_model(self.model_id):
            return self._decide_json(system_prompt, user_prompt)
        return self._decide_tools(system_prompt, user_prompt)

    def _decide_tools(self, system_prompt: str, user_prompt: str) -> AdvisorDecision:
        """Function-calling path for standard (non-reasoning) models."""
        import json
        import time

        client = self._get_client()
        t0 = time.perf_counter()
        try:
            response = client.chat.completions.create(
                model=self.model_id,
                max_tokens=self.max_tokens,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tools=[_DECISION_TOOL],
                tool_choice={"type": "function", "function": {"name": "record_decision"}},
            )
        except Exception as e:
            return self._error(e)

        latency_ms = (time.perf_counter() - t0) * 1000
        usage = self._usage(response, latency_ms)

        tool_calls = getattr(response.choices[0].message, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                if tc.function.name == "record_decision":
                    data = json.loads(tc.function.arguments)
                    return AdvisorDecision(
                        approved=bool(data["approved"]),
                        confidence=float(data["confidence"]),
                        reason=str(data["reason"]),
                        model_id=self.model_id,
                        usage=usage,
                    )
        return AdvisorDecision(True, 0.0, "no structured decision returned", self.model_id, usage=usage)

    def _decide_json(self, system_prompt: str, user_prompt: str) -> AdvisorDecision:
        """JSON-mode path for reasoning models that reject forced tool_choice.

        Reasoning models spend tokens on chain-of-thought before producing output,
        so we use a higher token ceiling and skip response_format (which can prevent
        the model from emitting a final content block).
        """
        import json
        import time

        client = self._get_client()
        t0 = time.perf_counter()
        # Reasoning models need budget for both thinking + output tokens.
        max_tokens = max(self.max_tokens, 4096)
        try:
            response = client.chat.completions.create(
                model=self.model_id,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt + _JSON_SCHEMA_SUFFIX},
                    {"role": "user", "content": user_prompt},
                ],
                # No response_format: it conflicts with reasoning mode and can
                # cause content to be empty.
            )
        except Exception as e:
            return self._error(e)

        latency_ms = (time.perf_counter() - t0) * 1000
        usage = self._usage(response, latency_ms)

        content = _strip_code_fence(response.choices[0].message.content or "")
        try:
            data = json.loads(content)
            return AdvisorDecision(
                approved=bool(data["approved"]),
                confidence=float(data["confidence"]),
                reason=str(data["reason"]),
                model_id=self.model_id,
                usage=usage,
            )
        except (json.JSONDecodeError, KeyError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "DeepSeek JSON parse failed (%s): %r", exc, content[:200]
            )
            return AdvisorDecision(True, 0.0, f"unparseable response: {content[:120]}", self.model_id, usage=usage)

    def _usage(self, response, latency_ms: float) -> LLMUsage:
        return LLMUsage(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            latency_ms=latency_ms,
        )

    def _error(self, exc: Exception) -> AdvisorDecision:
        import warnings
        warnings.warn(
            f"DeepSeekProvider: API error — approving trade by default. {exc}",
            RuntimeWarning,
            stacklevel=3,
        )
        return AdvisorDecision(True, 0.0, f"API error: {exc}", self.model_id, is_error=True)
