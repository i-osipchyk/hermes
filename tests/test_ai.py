from datetime import UTC, datetime, timedelta

import pytest

from hermes import Backtest, CryptoPair, Strategy, Symbol, Timeframe
from hermes.ai import AdvisorDecision, AIAdvisor, AIProvider, DecisionCache
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.execution import (
    CostModel,
    FinancingModel,
    PercentCommission,
    SlippageModel,
    SpreadModel,
)

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


class StubProvider(AIProvider):
    model_id = "stub-1"

    def __init__(self, approved: bool):
        self.calls = 0
        self._approved = approved

    def decide(self, system_prompt: str, user_prompt: str) -> AdvisorDecision:
        self.calls += 1
        return AdvisorDecision(self._approved, 1.0, "stub", self.model_id)


def _btc():
    return CryptoPair(
        Symbol("BTCUSDT", "binance"), base_asset="BTC", quote_currency="USDT", tick_size=0.01
    )


def _zero_costs():
    return CostModel(PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), FinancingModel(0.0))


def _bar(i, o, h, l, c):
    return Bar(T0 + timedelta(hours=i), H1, o, h, l, c, 1.0)


class GatedBuyOnce(Strategy):
    def setup(self):
        self.done = False

    def on_bar(self, bar):
        if not self.done and self.venue.position().is_flat:
            order = self.buy(1, stop_loss=self.price - 10, take_profit=self.price + 10)
            self.done = True
            if not self.confirm_with_ai(order, "Confirm this long."):
                self.venue.cancel(order)


def _run(advisor):
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 100, 115, 100, 105), _bar(3, 105, 105, 105, 105)]
    bt = Backtest(
        strategy=GatedBuyOnce(),
        source=InMemorySource(_btc(), {H1: bars}),
        symbol=Symbol("BTCUSDT", "binance"),
        timeframes=[H1],
        start=bars[0].timestamp,
        end=bars[-1].timestamp,
        cost_model=_zero_costs(),
        advisor=advisor,
    )
    return bt.run()


def test_ai_veto_blocks_trade(tmp_path):
    provider = StubProvider(approved=False)
    advisor = AIAdvisor(provider, cache=DecisionCache(tmp_path))
    result = _run(advisor)
    assert len(result.trades) == 0
    assert provider.calls == 1
    assert len(result.vetoed_signals) == 1
    vetoed = result.vetoed_signals[0]
    assert vetoed.ai_decision is not None
    assert vetoed.ai_decision.approved is False


def test_ai_approve_allows_trade(tmp_path):
    provider = StubProvider(approved=True)
    advisor = AIAdvisor(provider, cache=DecisionCache(tmp_path))
    result = _run(advisor)
    assert len(result.trades) == 1
    assert len(result.vetoed_signals) == 0


def test_decision_cache_roundtrip_and_reuse(tmp_path):
    provider = StubProvider(approved=True)
    advisor = AIAdvisor(provider, cache=DecisionCache(tmp_path))
    key = advisor.cache.key("stub-1", "sys", "user")
    d = AdvisorDecision(True, 0.9, "why", "stub-1")
    advisor.cache.put(key, d)
    got = advisor.cache.get(key)
    assert got == d


# --- llmkit: usage, pricing and reporting ------------------------------------

class ListSink:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def flush(self):
        pass


def _tracker():
    from llmkit import Tracker

    sink = ListSink()
    return Tracker(project="hermes", env="test", sinks=[sink]), sink


# Saturday: DeepSeek's off-peak rate applies all day.
OFF_PEAK = datetime(2026, 10, 10, 12, tzinfo=UTC)


class MeteredProvider(AIProvider):
    """A stub that reports usage like a real API backend does."""

    llm_provider = "deepseek"
    model_id = "deepseek-flash"

    def __init__(self, is_error: bool = False):
        self._is_error = is_error

    def decide(self, system_prompt: str, user_prompt: str) -> AdvisorDecision:
        from hermes.ai.provider import LLMUsage

        if self._is_error:
            return AdvisorDecision(True, 0.0, "boom", self.model_id, is_error=True)
        usage = LLMUsage(input_tokens=200, output_tokens=50, cache_read_tokens=800,
                         cache_creation_tokens=0, latency_ms=12.0, started_at=OFF_PEAK)
        return AdvisorDecision(True, 1.0, "ok", self.model_id, usage=usage)


def test_deepseek_usage_splits_cache_hits_from_uncached_input():
    """DeepSeek's prompt_tokens includes cache hits. Counting them all as uncached
    input billed every cached token at the full rate."""
    from types import SimpleNamespace as NS

    from hermes.ai.deepseek import DeepSeekProvider

    tool_call = NS(function=NS(name="record_decision",
                               arguments='{"approved": true, "confidence": 0.9, "reason": "r"}'))
    response = NS(
        choices=[NS(message=NS(tool_calls=[tool_call]))],
        usage=NS(prompt_tokens=1000, completion_tokens=50,
                 prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200),
    )
    provider = DeepSeekProvider("deepseek-chat")
    provider._client = NS(chat=NS(completions=NS(create=lambda **_: response)))

    usage = provider.decide("sys", "user").usage
    assert usage is not None
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (200, 800, 50)
    assert usage.started_at is not None


def test_live_call_is_reported_to_llmkit_and_priced_by_it(tmp_path):
    from llmkit import Usage, cost

    tracker, sink = _tracker()
    advisor = AIAdvisor(MeteredProvider(), cache=DecisionCache(tmp_path), tracker=tracker)
    _run(advisor)

    [rec] = sink.records
    assert (rec.project, rec.component, rec.provider, rec.model) == (
        "hermes", "advisor", "deepseek", "deepseek-flash")
    assert rec.metadata == {"strategy": "GatedBuyOnce", "symbol": "BTCUSDT"}
    expected = cost("deepseek-flash", Usage(200, 50, 800), OFF_PEAK)
    assert expected is not None
    assert rec.cost_usd == pytest.approx(expected.total)
    # the per-backtest log carries the same number llmkit recorded
    [call] = advisor.obs_log.calls
    assert call.cost_usd == rec.cost_usd


def test_decision_cache_hits_are_not_reported_to_llmkit(tmp_path):
    tracker, sink = _tracker()
    cache = DecisionCache(tmp_path)
    _run(AIAdvisor(MeteredProvider(), cache=cache, tracker=tracker))
    _run(AIAdvisor(MeteredProvider(), cache=cache, tracker=tracker))
    assert len(sink.records) == 1


def test_provider_error_is_reported_to_llmkit_as_an_error(tmp_path):
    tracker, sink = _tracker()
    _run(AIAdvisor(MeteredProvider(is_error=True), cache=DecisionCache(tmp_path), tracker=tracker))
    [rec] = sink.records
    assert rec.error == "boom"
    assert rec.input_tokens == 0


def test_providers_without_a_billing_backend_are_not_reported(tmp_path):
    """Stubs and local models have no ``llm_provider``; they cost nothing and
    must not pollute the cross-project spend log."""
    tracker, sink = _tracker()
    _run(AIAdvisor(StubProvider(approved=True), cache=DecisionCache(tmp_path), tracker=tracker))
    assert sink.records == []


def test_unknown_model_is_unpriced_rather_than_guessed(tmp_path):
    class Unreleased(MeteredProvider):
        llm_provider = "anthropic"
        model_id = "claude-something-unreleased"

    tracker, _ = _tracker()
    advisor = AIAdvisor(Unreleased(), cache=DecisionCache(tmp_path), tracker=tracker)
    _run(advisor)
    assert advisor.obs_log.calls[0].cost_usd is None


# --- fail-open visibility --------------------------------------------------

class ErroringProvider(AIProvider):
    model_id = "stub-err"

    def decide(self, system_prompt: str, user_prompt: str) -> AdvisorDecision:
        return AdvisorDecision(True, 0.0, "boom", self.model_id, is_error=True)


def test_provider_error_fails_open_but_is_counted(tmp_path):
    """A provider outage approves the trade (documented fail-open) — and the run
    must say so, or an ungated run is indistinguishable from a gated one."""
    advisor = AIAdvisor(ErroringProvider(), cache=DecisionCache(tmp_path))
    result = _run(advisor)

    assert len(result.trades) == 1, "fail-open should let the trade through"
    summary = result.to_dict()["llm_summary"]
    assert summary["fail_open_calls"] == 1
    assert summary["fail_open_rate"] == pytest.approx(1.0)


def test_clean_run_reports_zero_fail_open(tmp_path):
    advisor = AIAdvisor(StubProvider(approved=True), cache=DecisionCache(tmp_path))
    summary = _run(advisor).to_dict()["llm_summary"]
    assert summary["fail_open_calls"] == 0
    assert summary["fail_open_rate"] == pytest.approx(0.0)


def test_error_decisions_are_never_cached(tmp_path):
    """An error must not poison the DecisionCache — the next run has to retry."""
    cache = DecisionCache(tmp_path)
    _run(AIAdvisor(ErroringProvider(), cache=cache))
    assert list(tmp_path.rglob("*.json")) == []
