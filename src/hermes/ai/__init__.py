"""AI Advisor: an optional, cached confirm/veto gate on candidate trades."""

from .advisor import AIAdvisor
from .cache import DecisionCache
from .claude import ClaudeProvider
from .enrichers import ContextEnricher, EDGARFilingEnricher, PolygonNewsEnricher, YFinanceFundamentalsEnricher
from .provider import AdvisorDecision, AIProvider

__all__ = [
    "AIAdvisor",
    "AIProvider",
    "AdvisorDecision",
    "ClaudeProvider",
    "DecisionCache",
    "ContextEnricher",
    "YFinanceFundamentalsEnricher",
    "EDGARFilingEnricher",
    "PolygonNewsEnricher",
]
