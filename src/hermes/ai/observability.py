"""LLM observability: per-call records and per-run aggregated metrics.

Every call that flows through AIAdvisor.evaluate() — whether served from the
DecisionCache or hitting the live API — is captured as an LLMCallRecord.  At
the end of a backtest the whole log is attached to BacktestResult so callers
can inspect or persist it.

Cache terminology used here:
  from_decision_cache   True when the Hermes DecisionCache answered the call;
                        no API request was made and all token / cost / latency
                        fields are None.
  cache_read_tokens     Input tokens served from the provider's prompt cache
                        (cheaper).  Only present on live calls.
  cache_creation_tokens Tokens written to Anthropic's prompt-cache.  Only
                        present on live calls.

``cost_usd`` comes from llmkit (``hermes.ai.llm``), the platform's only price table;
this log is the per-backtest view of the calls llmkit records across projects.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

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
    cache_read_tokens: int | None      # prompt-cache read tokens
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

    # True when the provider errored and the gate failed OPEN (approved without a
    # real decision). Defaulted so logs written before this field still load.
    is_error: bool = False

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
        # None when the provider reports no usage (stub / local / SDK omission).
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_creation_tokens: int | None,
        latency_ms: float | None,
        cost_usd: float | None,
        approved: bool,
        confidence: float,
        reason: str,
        system_prompt: str,
        user_prompt: str,
        is_error: bool = False,
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
            is_error=is_error,
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
    def fail_open_calls(self) -> int:
        """Calls where the provider errored and the trade was approved anyway.

        Non-zero means the AI gate was partially or wholly absent for this run:
        the metrics describe a *less gated* strategy than the one configured.
        """
        return sum(1 for c in self.calls if c.is_error)

    @property
    def fail_open_rate(self) -> float | None:
        if not self.calls:
            return None
        return self.fail_open_calls / len(self.calls)

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
            # Non-zero => the gate failed open; treat the run's metrics with suspicion.
            "fail_open_calls": self.fail_open_calls,
            "fail_open_rate": self.fail_open_rate,
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
