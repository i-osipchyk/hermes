"""LLM observability: per-call records and per-run aggregated metrics.

Every call that flows through AIAdvisor.evaluate() — whether served from the
DecisionCache or hitting the live API — is captured as an LLMCallRecord.  At
the end of a backtest the whole log is attached to BacktestResult so callers
can inspect or persist it.

Cache terminology used here:
  from_decision_cache   True when the Hermes DecisionCache answered the call;
                        no API request was made and all token / cost / latency
                        fields are None.
  cache_read_tokens     Tokens served from Anthropic's prompt-cache (cheaper
                        input tokens).  Only present on live calls.
  cache_creation_tokens Tokens written to Anthropic's prompt-cache.  Only
                        present on live calls.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# Pricing table — USD per million tokens (MTok).
# Add rows for new models; the longest prefix match wins.
# ---------------------------------------------------------------------------
_PRICING: list[tuple[str, dict[str, float]]] = [
    ("claude-opus-4",      {"input": 15.00, "output": 75.00, "cache_read": 1.50,  "cache_write": 18.75}),
    ("claude-sonnet-4",    {"input":  3.00, "output": 15.00, "cache_read": 0.30,  "cache_write":  3.75}),
    ("claude-haiku-4",     {"input":  0.80, "output":  4.00, "cache_read": 0.08,  "cache_write":  1.00}),
    # Legacy / fallback Claude
    ("claude-opus",        {"input": 15.00, "output": 75.00, "cache_read": 1.50,  "cache_write": 18.75}),
    ("claude-sonnet",      {"input":  3.00, "output": 15.00, "cache_read": 0.30,  "cache_write":  3.75}),
    ("claude-haiku",       {"input":  0.80, "output":  4.00, "cache_read": 0.08,  "cache_write":  1.00}),
    # DeepSeek
    ("deepseek-reasoner",  {"input":  0.55, "output":  2.19, "cache_read": 0.14,  "cache_write":  0.55}),
    ("deepseek-chat",      {"input":  0.27, "output":  1.10, "cache_read": 0.07,  "cache_write":  0.27}),
    ("deepseek",           {"input":  0.27, "output":  1.10, "cache_read": 0.07,  "cache_write":  0.27}),
]


def _price_for(model_id: str) -> dict[str, float] | None:
    lower = model_id.lower()
    for prefix, rates in _PRICING:
        if lower.startswith(prefix):
            return rates
    return None


def compute_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float | None:
    """Return estimated USD cost, or None if the model is not in the pricing table."""
    rates = _price_for(model_id)
    if rates is None:
        return None
    per = 1_000_000
    return (
        input_tokens         * rates["input"]        / per
        + output_tokens      * rates["output"]       / per
        + cache_read_tokens  * rates["cache_read"]   / per
        + cache_creation_tokens * rates["cache_write"] / per
    )


# ---------------------------------------------------------------------------
# Per-call record
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class LLMCallRecord:
    """One row in the observability log — either a live API call or a cache hit."""

    request_time: datetime         # UTC wall-clock when evaluate() was called
    model_id: str
    from_decision_cache: bool      # True → Hermes DecisionCache hit; no API call

    # Populated only for live API calls (None for cache hits).
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None      # Anthropic prompt-cache read tokens
    cache_creation_tokens: int | None  # Anthropic prompt-cache write tokens
    latency_ms: float | None
    cost_usd: float | None

    # Decision outcome
    approved: bool
    confidence: float
    reason: str

    # Prompts (full text)
    system_prompt: str
    user_prompt: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["request_time"] = self.request_time.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> LLMCallRecord:
        d = dict(d)
        d["request_time"] = datetime.fromisoformat(d["request_time"])
        return cls(**d)


# ---------------------------------------------------------------------------
# Aggregated log for one backtest run
# ---------------------------------------------------------------------------

@dataclass
class LLMObservabilityLog:
    """Collects LLMCallRecords during a run and computes aggregate metrics."""

    calls: list[LLMCallRecord] = field(default_factory=list)

    # --- append helpers -------------------------------------------------------

    def record_cache_hit(
        self,
        *,
        request_time: datetime,
        model_id: str,
        approved: bool,
        confidence: float,
        reason: str,
        system_prompt: str,
        user_prompt: str,
    ) -> None:
        self.calls.append(LLMCallRecord(
            request_time=request_time,
            model_id=model_id,
            from_decision_cache=True,
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_creation_tokens=None,
            latency_ms=None,
            cost_usd=None,
            approved=approved,
            confidence=confidence,
            reason=reason,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        ))

    def record_live_call(
        self,
        *,
        request_time: datetime,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_creation_tokens: int,
        latency_ms: float,
        cost_usd: float | None,
        approved: bool,
        confidence: float,
        reason: str,
        system_prompt: str,
        user_prompt: str,
    ) -> None:
        self.calls.append(LLMCallRecord(
            request_time=request_time,
            model_id=model_id,
            from_decision_cache=False,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            approved=approved,
            confidence=confidence,
            reason=reason,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        ))

    # --- aggregate properties -------------------------------------------------

    @property
    def total_calls(self) -> int:
        return len(self.calls)

    @property
    def decision_cache_hits(self) -> int:
        return sum(1 for c in self.calls if c.from_decision_cache)

    @property
    def live_calls(self) -> int:
        return sum(1 for c in self.calls if not c.from_decision_cache)

    @property
    def total_input_tokens(self) -> int:
        return sum(c.input_tokens or 0 for c in self.calls)

    @property
    def total_output_tokens(self) -> int:
        return sum(c.output_tokens or 0 for c in self.calls)

    @property
    def total_cache_read_tokens(self) -> int:
        return sum(c.cache_read_tokens or 0 for c in self.calls)

    @property
    def total_cache_creation_tokens(self) -> int:
        return sum(c.cache_creation_tokens or 0 for c in self.calls)

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd or 0.0 for c in self.calls)

    @property
    def avg_latency_ms(self) -> float | None:
        live = [c.latency_ms for c in self.calls if c.latency_ms is not None]
        return sum(live) / len(live) if live else None

    @property
    def approval_rate(self) -> float | None:
        if not self.calls:
            return None
        return sum(1 for c in self.calls if c.approved) / len(self.calls)

    def summary(self) -> dict:
        """Aggregated metrics dict — suitable for JSON serialisation."""
        return {
            "total_calls": self.total_calls,
            "decision_cache_hits": self.decision_cache_hits,
            "live_calls": self.live_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cache_creation_tokens": self.total_cache_creation_tokens,
            "total_cost_usd": self.total_cost_usd,
            "avg_latency_ms": self.avg_latency_ms,
            "approval_rate": self.approval_rate,
        }

    # --- persistence ----------------------------------------------------------

    def save(self, path: Path) -> None:
        """Write every call as a JSON line to *path*."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            for rec in self.calls:
                fh.write(json.dumps(rec.to_dict()) + "\n")

    @classmethod
    def load(cls, path: Path) -> LLMObservabilityLog:
        """Restore from a JSONL file written by :meth:`save`."""
        log = cls()
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    log.calls.append(LLMCallRecord.from_dict(json.loads(line)))
        return log

    def iter_records(self) -> Iterator[LLMCallRecord]:
        return iter(self.calls)
