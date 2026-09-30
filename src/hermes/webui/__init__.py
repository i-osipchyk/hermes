"""Local Streamlit web UI for running and reviewing backtests (ADR-0008).

``app`` is the Streamlit page and ``launch`` the ``hermes-ui`` entry point. The
UI-free machinery it used to own — strategy discovery, the run ledger, the Claude
review, the source registry, universes — now lives in :mod:`hermes.research`, where
the CLI and any agent can reach it too (ADR-0010).

The names below are re-exported so ``from hermes.webui import discovery`` and
``from hermes.webui import run_cache`` keep working; new code should import from
``hermes.research``.
"""

from __future__ import annotations

from ..research import discovery, review, sources, universes
from ..research import ledger as run_cache

__all__ = ["discovery", "ledger", "review", "run_cache", "sources", "universes"]

# Both spellings resolve to the same module.
ledger = run_cache
