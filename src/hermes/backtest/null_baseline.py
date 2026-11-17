"""Null test for rule-based strategies (audit G1, phase-2 plan step 4).

The random baseline in :mod:`hermes.backtest.random_baseline` only randomises an AI veto, so
a rule-based strategy had no control at all. This one asks: *would the same trades, taken at
an unrelated time, have done as well?*

Each null sample replays the observed trades — same sides, sizes, holding periods in bars and
spacing — shifted by **one random circular offset**, through the real engine. A common offset
keeps the clustering of entries and breaks only their link to market state. Costs, fills,
margin and sizing are the engine's own, nothing is re-implemented here. Stops and targets are
not replayed (a trade is held for the same number of bars instead), and a trade that would run
past the end of the data is closed there.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from ..core import Bar
from ..execution import Side
from ..strategy import Strategy
from .engine import Backtest
from .result import BacktestResult

# (side, native size, bars the position is held) per signal step
_Plan = dict[int, list[tuple[Side, float, int | None]]]


@dataclass(frozen=True)
class NullResult:
    observed_sharpe: float | None
    null_sharpes: list[float]
    null_trade_counts: list[int]
    p_value: float | None            # one-sided: P(null Sharpe >= observed)
    n: int
    n_trades: int                    # trades in the observed run


class _Replay(Strategy):
    """Places the scheduled entries and closes them after their scheduled holding period."""

    def __init__(self, step_of: dict[datetime, int], entries: _Plan, exits_after: dict[int, int]):
        super().__init__()
        self._step_of = step_of
        self._entries = entries
        self._exits_after = exits_after          # unused placeholder to keep the plan explicit

    def setup(self) -> None:
        self._known: set[int] = set()
        self._awaiting: list[tuple[int, int | None]] = []   # (signal step, exit request step)
        self._exit_at: dict[int, int | None] = {}

    def on_bar(self, bar: Bar) -> None:
        step = self._step_of.get(bar.timestamp)
        if step is None:
            return
        # attribute trades filled at this bar's open to their signals (FIFO); a signal whose
        # order was rejected never fills and is dropped rather than mis-assigned later
        self._awaiting = [a for a in self._awaiting if a[0] >= step - 1]
        for trade in self.venue.open_trades():
            if id(trade) not in self._known:
                self._known.add(id(trade))
                self._exit_at[id(trade)] = self._awaiting.pop(0)[1] if self._awaiting else None
        for trade in self.venue.open_trades():
            if self._exit_at.get(id(trade)) == step:
                self.close(trade)
        for side, size, exit_step in self._entries.get(step, ()):
            (self.buy if side is Side.BUY else self.sell)(size)
            self._awaiting.append((step, exit_step))


def _observed_plan(result: BacktestResult) -> tuple[dict[datetime, int], list[tuple[int, int, Side, float]]]:
    """Step index of every base bar, and each observed trade as (signal step, exit request
    step, side, size). A fill at step j was signalled at j-1; an exit filled at e was
    requested at e-1."""
    step_of = {ts: i for i, (ts, _) in enumerate(result.equity_curve)}
    plan = []
    for t in result.trades:
        j, e = step_of.get(t.entry_time), step_of.get(t.exit_time)
        if j is None or e is None or j < 1 or e < j:
            continue
        plan.append((j - 1, e - 1, t.side, t.size))
    return step_of, plan


def _shifted(plan: list[tuple[int, int, Side, float]], offset: int, n_steps: int) -> _Plan:
    entries: _Plan = {}
    for sig, ext, side, size in plan:
        a = (sig + offset) % n_steps
        b = a + (ext - sig)
        entries.setdefault(a, []).append((side, size, b if b < n_steps else None))
    return entries


def run_null_test(
    backtest: Backtest, result: BacktestResult, n: int = 200, seed: int = 42
) -> NullResult:
    """Rank the observed Sharpe against ``n`` time-shifted replays of the same trades."""
    step_of, plan = _observed_plan(result)
    n_steps = len(result.equity_curve)
    observed = result.metrics.sharpe
    if not plan or n_steps < 2:
        return NullResult(observed, [], [], None, 0, len(plan))

    start = result.effective_start or result.equity_curve[0][0]
    sharpes: list[float] = []
    counts: list[int] = []
    for i in range(n):
        offset = random.Random(seed + i).randrange(1, n_steps)
        replay = _Replay(step_of, _shifted(plan, offset, n_steps), {})
        sim: Any = replace(backtest, strategy=replay, advisor=None, params={}, sizer=None,
                           start=start, validate=False, progress_callback=None)
        out = sim.run()
        sharpes.append(out.metrics.sharpe if out.metrics.sharpe is not None else 0.0)
        counts.append(len(out.trades))
    p = None if observed is None else (1 + sum(s >= observed for s in sharpes)) / (n + 1)
    return NullResult(observed, sharpes, counts, p, n, len(plan))
