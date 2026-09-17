"""PIT (point-in-time) context enrichers for the AI Advisor.

An enricher is any object with an ``enrich(ticker, as_of) -> str`` method.
Pass a list of enrichers to ``AIAdvisor(enrichers=[...])``; the advisor appends
their text output to the assembled context before calling the LLM.

All built-in enrichers cache results to ``.cache/pit/`` so API calls are made
at most once per ticker/date combination.

    from hermes.ai.enrichers import YFinanceFundamentalsEnricher, EDGARFilingEnricher, PolygonNewsEnricher
"""

from __future__ import annotations

from typing import Protocol
from datetime import date


class ContextEnricher(Protocol):
    """Anything that can append PIT text to the AI advisor context."""

    def enrich(self, ticker: str, as_of: date) -> str:
        """Return a text block (may be empty string) to append to the prompt."""
        ...


from .edgar import EDGARFilingEnricher
from .fundamentals import YFinanceFundamentalsEnricher
from .news import PolygonNewsEnricher

__all__ = [
    "ContextEnricher",
    "YFinanceFundamentalsEnricher",
    "EDGARFilingEnricher",
    "PolygonNewsEnricher",
]
