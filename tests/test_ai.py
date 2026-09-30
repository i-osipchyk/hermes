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


# --- pricing table ---------------------------------------------------------

def test_pricing_uses_longest_prefix_not_first_match():
    """``claude-opus-4-8`` must not be priced by the ``claude-opus-4`` row.

    The old first-match lookup billed Opus 4.8 and Opus 5 at Opus 4's
    $15/$75 — a 3x overstatement on every AI-gated run.
    """
    from hermes.ai.observability import compute_cost

    one_m = 1_000_000
    assert compute_cost("claude-opus-5", one_m, one_m) == pytest.approx(30.0)
    assert compute_cost("claude-opus-4-8", one_m, one_m) == pytest.approx(30.0)
    assert compute_cost("claude-sonnet-5", one_m, one_m) == pytest.approx(12.0)
    assert compute_cost("claude-haiku-4-5", one_m, one_m) == pytest.approx(6.0)
    # the older generation still prices at its own (higher) rates
    assert compute_cost("claude-opus-4", one_m, one_m) == pytest.approx(90.0)


def test_pricing_resolves_dated_snapshots():
    from hermes.ai.observability import compute_cost

    assert compute_cost("claude-opus-5-20260401", 1_000_000, 0) == pytest.approx(5.0)


def test_unknown_model_is_unpriced_rather_than_guessed():
    """No bare ``claude-*`` catch-all: an unknown id must return None so callers
    can show 'unpriced' instead of silently billing another generation's rates."""
    from hermes.ai.observability import compute_cost

    assert compute_cost("claude-something-unreleased", 1_000, 1_000) is None
    assert compute_cost("gpt-4", 1_000, 1_000) is None


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
