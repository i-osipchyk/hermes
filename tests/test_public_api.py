"""Guards on the library's public surface.

The Claude Code skills tell an agent to verify a generated strategy's imports
against ``src/hermes/__init__.py``, so that file has to be a complete inventory,
not a subset. These tests fail loudly when a new capability is added to a
sub-package but never re-exported — the drift that previously hid
``WalkForward``, ``cost_sensitivity``, ``run_random_simulations`` and
``CostModel`` from anyone reading the top level.
"""

from __future__ import annotations

import importlib

import pytest

import hermes

SUBPACKAGES = ["core", "data", "indicators", "strategy", "execution", "ai", "backtest", "research"]


def test_every_exported_name_resolves():
    missing = [n for n in hermes.__all__ if not hasattr(hermes, n)]
    assert missing == [], f"hermes.__all__ names nothing: {missing}"


def test_all_is_sorted_into_no_duplicates():
    dupes = {n for n in hermes.__all__ if hermes.__all__.count(n) > 1}
    assert dupes == set(), f"duplicated in hermes.__all__: {sorted(dupes)}"


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_surface_is_reexported_at_top_level(name):
    """Anything a sub-package calls public must be importable from ``hermes``."""
    module = importlib.import_module(f"hermes.{name}")
    missing = sorted(set(getattr(module, "__all__", [])) - set(hermes.__all__))
    assert missing == [], (
        f"hermes.{name}.__all__ exports {missing} but `hermes` does not. "
        f"Add them to src/hermes/__init__.py — the skills treat that file as the "
        f"library's inventory."
    )


def test_research_primitives_are_importable_from_the_top_level():
    """The analyse-results rubric needs these; they were reachable only via
    ``hermes.backtest`` (or, for random baselines, only via the web UI)."""
    for name in (
        "WalkForward", "split_isoos", "cost_sensitivity", "regime_analysis",
        "run_batch", "run_random_simulations", "run_universe_random_simulations",
        "validate", "CostModel", "Metrics", "plot_equity",
        # the research loop (ADR-0010)
        "RunLedger", "analyze", "discover_all", "configured_backtest",
    ):
        assert hasattr(hermes, name), f"hermes.{name} is not exported"
