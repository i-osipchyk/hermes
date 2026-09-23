"""Random baseline simulator for AI veto edge validation.

Runs N backtests where each signal is approved with probability p (instead of
using the AI advisor), then returns the distribution of metrics for comparison.
Both single-symbol and universe (portfolio) runs are supported, with optional
parallel execution via ProcessPoolExecutor.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from typing import Callable

from hermes.ai.random_advisor import RandomAdvisor


# ---------------------------------------------------------------------------
# Worker functions — must be top-level so ProcessPoolExecutor can pickle them
# ---------------------------------------------------------------------------

def _single_sim_worker(args: tuple) -> dict:
    """Run one single-symbol simulation. Called in a worker process."""
    build_backtest_fn, p, seed, overrides = args
    from hermes.ai.random_advisor import RandomAdvisor
    advisor = RandomAdvisor(p=p, seed=seed)
    bt = build_backtest_fn(advisor=advisor, **overrides)
    result = bt.run()
    row = asdict(result.metrics)
    row["equity_curve"] = [(ts.isoformat(), eq) for ts, eq in result.equity_curve]
    return row


def _universe_sim_worker(args: tuple) -> dict:
    """Run one universe simulation. Called in a worker process."""
    from hermes.backtest.universe import UniverseBacktest
    from hermes.ai.random_advisor import RandomAdvisor

    strategy_factory, source, calendar, timeframes, start, end, \
        starting_cash, sizer, unconstrained, p, seed = args

    advisor = RandomAdvisor(p=p, seed=seed)
    ub = UniverseBacktest(
        strategy_factory=strategy_factory,
        source=source,
        calendar=calendar,
        timeframes=timeframes,
        start=start,
        end=end,
        starting_cash=starting_cash,
        sizer=sizer,
        unconstrained=unconstrained,
        advisor=advisor,
    )
    ur = ub.run()
    result = ur.portfolio_result.result
    row = asdict(result.metrics)
    row["equity_curve"] = [(ts.isoformat(), eq) for ts, eq in result.equity_curve]
    return row


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_random_simulations(
    build_backtest_fn: Callable,
    n: int = 200,
    p: float = 0.05,
    seed: int = 42,
    workers: int = 1,
    progress_cb: Callable[[int, int], None] | None = None,
    **backtest_overrides,
) -> list[dict]:
    """Run N single-symbol backtests with a random p% approval rate.

    Args:
        build_backtest_fn: Factory (e.g. ``strategies.ema_crossover_ai.build_backtest``)
            that accepts ``advisor=`` and arbitrary keyword overrides.
        n: Number of simulations.
        p: Signal approval probability.
        seed: Base seed; simulation i uses seed+i.
        workers: Number of parallel worker processes (default 1 = sequential).
            Set to ``os.cpu_count()`` or similar for full parallelism.
        progress_cb: Optional ``(i, n)`` callback invoked after each completed sim.
        **backtest_overrides: Forwarded to build_backtest_fn.

    Returns:
        List of N dicts with metrics fields + ``equity_curve``.
    """
    task_args = [
        (build_backtest_fn, p, seed + i, backtest_overrides)
        for i in range(n)
    ]

    if workers <= 1:
        results = []
        for i, args in enumerate(task_args):
            results.append(_single_sim_worker(args))
            if progress_cb:
                progress_cb(i + 1, n)
        return results

    results = [None] * n
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_single_sim_worker, args): i for i, args in enumerate(task_args)}
        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()
            done += 1
            if progress_cb:
                progress_cb(done, n)
    return results


def run_universe_random_simulations(
    strategy_factory: Callable,
    source,
    calendar,
    timeframes: list,
    start,
    end,
    starting_cash: float = 100_000,
    sizer=None,
    unconstrained: bool = False,
    n: int = 200,
    p: float = 0.05,
    seed: int = 42,
    workers: int = 1,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """Run N universe backtests with a random p% approval rate.

    Args:
        strategy_factory: Zero-arg callable returning a fresh Strategy instance.
        source: DataSource instance.
        calendar: ConstituentCalendar instance.
        timeframes: List of Timeframe objects.
        start, end: datetime (UTC) for the backtest window.
        starting_cash: Total portfolio capital.
        sizer: Position sizer instance.
        unconstrained: Whether to skip capital checks.
        n: Number of simulations.
        p: Signal approval probability.
        seed: Base seed; simulation i uses seed+i.
        workers: Number of parallel worker processes (default 1 = sequential).
        progress_cb: Optional ``(i, n)`` callback.

    Returns:
        List of N dicts with metrics fields + ``equity_curve``.
    """
    task_args = [
        (strategy_factory, source, calendar, timeframes, start, end,
         starting_cash, sizer, unconstrained, p, seed + i)
        for i in range(n)
    ]

    if workers <= 1:
        results = []
        for i, args in enumerate(task_args):
            results.append(_universe_sim_worker(args))
            if progress_cb:
                progress_cb(i + 1, n)
        return results

    results = [None] * n
    # Run sim 0 sequentially first to fully populate the data cache.
    # This prevents parallel workers from simultaneously racing to fetch
    # the same yfinance data, which triggers rate-limit errors.
    results[0] = _universe_sim_worker(task_args[0])
    if progress_cb:
        progress_cb(1, n)

    if n > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_universe_sim_worker, args): i + 1
                       for i, args in enumerate(task_args[1:])}
            done = 1
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
                done += 1
                if progress_cb:
                    progress_cb(done, n)
    return results
