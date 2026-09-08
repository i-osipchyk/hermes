"""Cost sensitivity analysis: run the same backtest across scaled cost models."""

from __future__ import annotations

import dataclasses


def cost_sensitivity(
    backtest,
    multipliers: tuple[float, ...] = (0.0, 1.0, 2.0),
) -> dict[float, object]:
    """Run the backtest with costs scaled by each multiplier.

    Parameters
    ----------
    backtest:    A Backtest instance.
    multipliers: Cost scaling factors. 0.0 = zero costs, 1.0 = base, 2.0 = double.

    Returns
    -------
    dict mapping each multiplier to its BacktestResult.
    """
    from ..execution import CostModel

    results = {}
    for m in multipliers:
        base_cost = backtest.cost_model
        if base_cost is None:
            source = backtest.source
            instrument = source.get_instrument(backtest.symbol)
            base_cost = CostModel.default_for(instrument)
        scaled = base_cost.scaled(m)
        bt = dataclasses.replace(backtest, cost_model=scaled)
        results[m] = bt.run()
    return results
