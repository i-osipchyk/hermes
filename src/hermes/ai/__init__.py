"""AI Advisor: an optional, cached confirm/veto gate on candidate trades."""

from .advisor import AIAdvisor
from .cache import DecisionCache
from .claude import ClaudeProvider
from .deepseek import DeepSeekProvider
from .enrichers import ContextEnricher, EDGARFilingEnricher, PolygonNewsEnricher, YFinanceFundamentalsEnricher, YFinanceFundamentalsScreen
from .observability import LLMCallRecord, LLMObservabilityLog
from .provider import AdvisorDecision, AIProvider
from .random_advisor import RandomAdvisor

__all__ = [
    "AIAdvisor",
    "AIProvider",
    "AdvisorDecision",
    "ClaudeProvider",
    "DeepSeekProvider",
    "DecisionCache",
    "ContextEnricher",
    "LLMCallRecord",
    "LLMObservabilityLog",
    "RandomAdvisor",
    "YFinanceFundamentalsEnricher",
    "YFinanceFundamentalsScreen",
    "EDGARFilingEnricher",
    "PolygonNewsEnricher",
]
