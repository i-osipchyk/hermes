"""Strategy Parameter: a declared, tunable input (CONTEXT.md).

First-class so a backtest run is a pure function of (Parameters, data) — the basis
for reproducibility and (deferred) Optimization. Declaring Parameters on a Strategy
is what lets a future optimizer vary them without touching strategy code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    default: Any
    # Optional search space for the deferred optimizer (grid/walk-forward).
    choices: tuple[Any, ...] | None = None
    bounds: tuple[float, float] | None = None
    description: str = ""
