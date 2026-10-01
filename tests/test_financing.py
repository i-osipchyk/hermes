"""Overnight financing: direction-dependent, can be received, weekday-weighted, dated.

Expected values are worked by hand from the bars below, not recomputed with engine code.
All trades are on a USD-quoted pair (no FX conversion) at price 1.0, 1 lot = 100,000
units, so notional is 100,000 USD and 3.65%/yr is exactly 10 USD per night.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from hermes import Backtest, Cfd, Strategy, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.data.sources.ctrader_source import _CFD_SESSION
from hermes.execution import (
    FX_NIGHT_WEIGHTS,
    CostModel,
    FinancingModel,
    PercentCommission,
    RateChange,
    SlippageModel,
    SpreadModel,
)

H1 = Timeframe.parse("1h")
# Monday 2023-01-02 12:00 UTC = 07:00 New York: bar i is local-dated Mon 2 Jan for i < 17.
MON = datetime(2023, 1, 2, 12, tzinfo=UTC)


class OneTrade(Strategy):
    def __init__(self, short: bool) -> None:
        super().__init__()
        self.short = short

    def setup(self) -> None:
        self.done = False

    def on_bar(self, bar) -> None:
        if not self.done:
            (self.sell if self.short else self.buy)(1.0)
            self.done = True


def _eurusd() -> Cfd:
    return Cfd(Symbol("EURUSD", "pepperstone"), session=_CFD_SESSION, tick_size=1e-5,
               lot_size=100_000, leverage=30)


def _cash_after(financing: FinancingModel, *, short: bool, start: datetime = MON, hours: int = 30):
    """Final equity of one flat 1-lot trade held for ``hours`` hourly bars."""
    bars = [Bar(start + timedelta(hours=i), H1, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(hours)]
    costs = CostModel(PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), financing)
    result = Backtest(
        strategy=OneTrade(short), source=InMemorySource(_eurusd(), {H1: bars}),
        symbol=_eurusd().symbol, timeframes=[H1], start=bars[0].timestamp,
        end=bars[-1].timestamp, starting_cash=10_000, cost_model=costs, unconstrained=True,
    ).run()
    return result.equity_curve[-1][1]


def test_a_short_with_a_negative_rate_is_paid_carry_while_a_long_pays():
    # 30 hourly bars from Mon 07:00 NY cross one local midnight (Mon -> Tue): one night.
    # Long pays 3.65% -> -10 USD; short is paid 1.46% (-1.46% rate) -> +4 USD.
    financing = FinancingModel(0.0365, short_rate=-0.0146)

    assert _cash_after(financing, short=False) == pytest.approx(10_000 - 10.0)
    assert _cash_after(financing, short=True) == pytest.approx(10_000 + 4.0)


def test_wednesday_night_is_charged_three_times_under_fx_night_weights():
    # Wed 2023-01-04 07:00 NY, 30 hourly bars: crosses the Wed -> Thu midnight only.
    # That night carries weight 3: 3 x 10 USD = 30 USD (a plain night would be 10).
    wed = datetime(2023, 1, 4, 12, tzinfo=UTC)
    financing = FinancingModel(0.0365, night_weights=FX_NIGHT_WEIGHTS)

    assert _cash_after(financing, short=False, start=wed) == pytest.approx(10_000 - 30.0)


def test_a_weekend_hold_is_one_night_under_fx_night_weights():
    # Fri 2023-01-06 07:00 NY for 78 hourly bars ends Mon 13:00 NY: nights starting Fri, Sat,
    # Sun. Only Friday is weighted (1): 10 USD, not the 30 a calendar-night count would bill.
    fri = datetime(2023, 1, 6, 12, tzinfo=UTC)
    financing = FinancingModel(0.0365, night_weights=FX_NIGHT_WEIGHTS)

    assert _cash_after(financing, short=False, start=fri, hours=78) == pytest.approx(10_000 - 10.0)


def test_the_rate_in_force_on_each_night_comes_from_the_dated_schedule():
    # Mon 07:00 NY for 54 bars crosses the Mon and Tue midnights. 3.65% (10 USD/night) holds
    # until the rate is raised to 7.30% effective Tue 3 Jan: Mon night 10 + Tue night 20 = 30.
    financing = FinancingModel(0.0365, schedule=(RateChange(date(2023, 1, 3), 0.0730),))

    assert _cash_after(financing, short=False, hours=54) == pytest.approx(10_000 - 30.0)


def test_scaling_costs_scales_every_rate_and_keeps_the_weights_and_schedule():
    base = FinancingModel(
        0.0365, short_rate=-0.0146, night_weights=FX_NIGHT_WEIGHTS,
        schedule=(RateChange(date(2023, 1, 3), 0.0730, -0.0292),),
    )
    costs = CostModel(PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), base)

    scaled = costs.scaled(2.0).financing

    assert scaled == FinancingModel(
        0.0730, short_rate=-0.0292, night_weights=FX_NIGHT_WEIGHTS,
        schedule=(RateChange(date(2023, 1, 3), 0.1460, -0.0584),),
    )
