from datetime import UTC, datetime, timedelta

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


def test_ai_approve_allows_trade(tmp_path):
    provider = StubProvider(approved=True)
    advisor = AIAdvisor(provider, cache=DecisionCache(tmp_path))
    result = _run(advisor)
    assert len(result.trades) == 1


def test_decision_cache_roundtrip_and_reuse(tmp_path):
    provider = StubProvider(approved=True)
    advisor = AIAdvisor(provider, cache=DecisionCache(tmp_path))
    key = advisor.cache.key("stub-1", "sys", "user")
    d = AdvisorDecision(True, 0.9, "why", "stub-1")
    advisor.cache.put(key, d)
    got = advisor.cache.get(key)
    assert got == d
