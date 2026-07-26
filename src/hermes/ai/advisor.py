"""AIAdvisor: the confirm/veto gate a Strategy can consult (ADR-0005).

It can only *block* an already-formed candidate trade — never invent, size, or
adjust one. Responsibilities:
  * assemble look-ahead-safe TEXT context (candlestick windows per Timeframe,
    indicator values, trade params, Instrument metadata) into the author's prompt;
  * route through the DecisionCache so backtests are reproducible;
  * call the pluggable AIProvider (Claude by default) on cache miss.
"""

from __future__ import annotations

from .cache import DecisionCache
from .provider import AdvisorDecision, AIProvider


class AIAdvisor:
    def __init__(
        self,
        provider: AIProvider,
        *,
        system_prompt: str = "You are a disciplined trading risk filter.",
        cache: DecisionCache | None = None,
    ) -> None:
        self.provider = provider
        self.system_prompt = system_prompt
        self.cache = cache or DecisionCache()

    def evaluate(self, strategy, order, prompt: str) -> AdvisorDecision:
        """Confirm/veto ``order`` for ``strategy``. ``prompt`` is the author's
        template; context is appended from the strategy's current view."""
        user_prompt = self._assemble_context(strategy, order, prompt)
        key = self.cache.key(self.provider.model_id, self.system_prompt, user_prompt)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        decision = self.provider.decide(self.system_prompt, user_prompt)
        self.cache.put(key, decision)
        return decision

    def _assemble_context(self, strategy, order, prompt: str, *, window: int = 30) -> str:
        """Serialise the strategy's current (look-ahead-safe) view + candidate trade
        into a compact text prompt. Only closed history + the current Forming Bar are
        included — never future data."""
        inst = strategy.instrument
        lines = [prompt.strip(), ""]
        lines.append(f"Instrument: {inst.symbol} ({inst.asset_class.value})")
        lines.append(
            f"Quote: {inst.quote_currency}  tick_size: {inst.tick_size}  "
            f"price_basis: {inst.price_basis.value}"
        )
        lines.append("")

        side = order.side.value.upper()
        lines.append("Candidate trade:")
        lines.append(
            f"  side={side} size={order.size} type={order.type.value} "
            f"entry_ref={strategy.price} stop_loss={order.stop_loss} "
            f"take_profit={order.take_profit}"
        )
        if order.stop_loss and order.take_profit:
            risk = abs(strategy.price - order.stop_loss)
            reward = abs(order.take_profit - strategy.price)
            if risk:
                lines.append(f"  reward:risk = {reward / risk:.2f}")
        lines.append("")

        view = strategy._view
        for tf in sorted(view.series.keys()):
            series = view[tf]
            bars = series.bars_for_compute()[-window:]
            lines.append(f"Timeframe {tf} (last {len(bars)} bars, O,H,L,C,V):")
            for b in bars:
                flag = "" if b.is_closed else "  <forming>"
                lines.append(
                    f"  {b.timestamp:%Y-%m-%d %H:%M} "
                    f"{b.open:.4f},{b.high:.4f},{b.low:.4f},{b.close:.4f},{b.volume:g}{flag}"
                )
            lines.append("")

        if strategy.registered_indicators:
            lines.append("Indicators (current):")
            for ind in strategy.registered_indicators:
                vals = strategy.indicator_value(ind)
                pretty = ", ".join(f"{k}={v}" for k, v in vals.items())
                lines.append(f"  {type(ind).__name__}@{ind.timeframe}: {pretty}")
        return "\n".join(lines)
