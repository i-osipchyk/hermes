"""Time-varying spread: a schedule of multipliers on the base spread.

Expected values are worked by hand. EURUSD bars are bid, so a buy pays ``bid + spread``;
base spread here is 0.0002 (2 pips), so a x10 window costs 0.0020.
Dates: Sun 2023-01-01, Mon 2023-01-02, and 2023-07-02 (Sunday in US summer time, EDT).
"""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from hermes import Cfd, Symbol
from hermes.data.sources.ctrader_source import _CFD_SESSION
from hermes.execution import (
    CostModel,
    FinancingModel,
    Liquidity,
    PercentCommission,
    Side,
    SlippageModel,
    SpreadModel,
    SpreadSchedule,
    SpreadWindow,
)

NY = ZoneInfo("America/New_York")
BASE = 0.0002


def _eurusd() -> Cfd:
    return Cfd(Symbol("EURUSD", "pepperstone"), session=_CFD_SESSION, tick_size=1e-5,
               lot_size=100_000, leverage=30)


def _buy(model: SpreadModel, ts: datetime | None, liquidity=Liquidity.TAKER) -> float:
    return model.adjust_fill(_eurusd(), Side.BUY, 1.1000, liquidity, ts)


def _sunday_open_schedule() -> SpreadSchedule:
    # Sunday 17:00-18:00 New York: spread x10.
    return SpreadSchedule(NY, (SpreadWindow(10.0, time(17, 0), time(18, 0), weekdays=(6,)),))


# --- slice 1: a window scales the spread inside it, not outside ------------

def test_spread_is_multiplied_inside_a_window_and_base_outside():
    model = SpreadModel(points=BASE, schedule=_sunday_open_schedule())
    sunday_open = datetime(2023, 1, 1, 22, 30, tzinfo=UTC)   # 17:30 NY, Sunday
    monday_noon = datetime(2023, 1, 2, 17, 0, tzinfo=UTC)    # 12:00 NY, Monday

    assert _buy(model, sunday_open) == pytest.approx(1.1000 + 10 * BASE)
    assert _buy(model, monday_noon) == pytest.approx(1.1000 + BASE)


def test_window_follows_new_york_daylight_saving_time():
    model = SpreadModel(points=BASE, schedule=_sunday_open_schedule())
    # Sunday 2 Jul 2023 is EDT (UTC-4): 17:30 NY = 21:30 UTC is inside, 22:30 UTC (18:30 NY) is out.
    assert _buy(model, datetime(2023, 7, 2, 21, 30, tzinfo=UTC)) == pytest.approx(1.1 + 10 * BASE)
    assert _buy(model, datetime(2023, 7, 2, 22, 30, tzinfo=UTC)) == pytest.approx(1.1 + BASE)


def test_overlapping_windows_take_the_widest_multiplier():
    rollover = SpreadWindow(5.0, time(16, 55), time(17, 5))
    open_ = SpreadWindow(10.0, time(17, 0), time(18, 0), weekdays=(6,))
    model = SpreadModel(points=BASE, schedule=SpreadSchedule(NY, (rollover, open_)))
    # Sunday 17:02 NY is in both windows; Monday 17:02 NY only in the rollover.
    assert _buy(model, datetime(2023, 1, 1, 22, 2, tzinfo=UTC)) == pytest.approx(1.1 + 10 * BASE)
    assert _buy(model, datetime(2023, 1, 2, 22, 2, tzinfo=UTC)) == pytest.approx(1.1 + 5 * BASE)


def test_a_maker_fill_never_pays_the_widened_spread():
    model = SpreadModel(points=BASE, schedule=_sunday_open_schedule())
    sunday_open = datetime(2023, 1, 1, 22, 30, tzinfo=UTC)
    assert _buy(model, sunday_open, Liquidity.MAKER) == 1.1000


# --- slice 2: CostModel threads the timestamp and scaling keeps the schedule ---

def test_cost_model_fill_price_uses_the_timestamp_and_scaled_keeps_the_schedule():
    cm = CostModel(PercentCommission(0.0), SpreadModel(BASE, _sunday_open_schedule()),
                   SlippageModel(0.0, 0.0), FinancingModel(0.0))
    sunday_open = datetime(2023, 1, 1, 22, 30, tzinfo=UTC)

    assert cm.fill_price(_eurusd(), Side.BUY, 1.1, ts=sunday_open) == pytest.approx(1.1 + 10 * BASE)
    # Doubling every cost doubles the widened spread too: 2 x 10 x 0.0002.
    assert cm.scaled(2.0).fill_price(
        _eurusd(), Side.BUY, 1.1, ts=sunday_open
    ) == pytest.approx(1.1 + 20 * BASE)


# --- slice 3: the engine charges the widened spread at the fill's bar ------

from datetime import timedelta  # noqa: E402

from hermes import Backtest, Strategy, Timeframe  # noqa: E402
from hermes.core import Bar  # noqa: E402
from hermes.data import InMemorySource  # noqa: E402

H1 = Timeframe.parse("1h")


class BuyThenSell(Strategy):
    """Buys 1 lot on the first bar, closes on the second. Orders fill at the NEXT bar's open."""

    def setup(self) -> None:
        self.n = 0

    def on_bar(self, bar) -> None:
        self.n += 1
        if self.n == 1:
            self.buy(1.0)
        elif self.n == 2:
            for trade in self.venue.open_trades():
                self.close(trade)


def _cash_after_round_trip(first_bar: datetime) -> float:
    """Flat 1.1000 market; the entry fills at the open of the bar after ``first_bar``."""
    bars = [Bar(first_bar + timedelta(hours=i), H1, 1.1, 1.1, 1.1, 1.1, 1.0) for i in range(4)]
    cm = CostModel(PercentCommission(0.0), SpreadModel(BASE, _sunday_open_schedule()),
                   SlippageModel(0.0, 0.0), FinancingModel(0.0))
    result = Backtest(
        strategy=BuyThenSell(), source=InMemorySource(_eurusd(), {H1: bars}),
        symbol=_eurusd().symbol, timeframes=[H1], start=bars[0].timestamp,
        end=bars[-1].timestamp, starting_cash=10_000, cost_model=cm, unconstrained=True,
    ).run()
    return result.equity_curve[-1][1]


def test_a_market_buy_at_the_sunday_open_pays_ten_times_the_midweek_spread():
    # Entry fills at the open of bar 2, exit (sell, at the bid) at bar 3: price 1.1 both ways,
    # so the round trip loses exactly the entry spread x 100,000 units (USD-quoted).
    sunday = datetime(2023, 1, 1, 21, 0, tzinfo=UTC)    # bar 2 opens 22:00 UTC = 17:00 NY Sunday
    midweek = datetime(2023, 1, 4, 14, 0, tzinfo=UTC)

    assert _cash_after_round_trip(sunday) == pytest.approx(10_000 - 10 * BASE * 100_000)
    assert _cash_after_round_trip(midweek) == pytest.approx(10_000 - BASE * 100_000)


# --- slice 4: broker presets and the CFD default ---------------------------

def _round_trip_loss(cm: CostModel, first_bar: datetime) -> float:
    bars = [Bar(first_bar + timedelta(hours=i), H1, 1.1, 1.1, 1.1, 1.1, 1.0) for i in range(4)]
    result = Backtest(
        strategy=BuyThenSell(), source=InMemorySource(_eurusd(), {H1: bars}),
        symbol=_eurusd().symbol, timeframes=[H1], start=bars[0].timestamp,
        end=bars[-1].timestamp, starting_cash=10_000, cost_model=cm, unconstrained=True,
    ).run()
    return 10_000 - result.equity_curve[-1][1]


MIDWEEK = datetime(2023, 1, 4, 14, 0, tzinfo=UTC)
SUNDAY = datetime(2023, 1, 1, 21, 0, tzinfo=UTC)   # entry bar opens 17:00 NY Sunday


def test_razor_midweek_round_trip_is_0_2_pip_plus_7_dollars_per_lot():
    # 0.2 pip = 2e-5 x 100,000 units = 2 USD spread, + 7 USD commission for the round trip.
    # (Slippage zeroed by the preset: Razor's cost is the quoted spread + commission.)
    loss = _round_trip_loss(CostModel.pepperstone_razor(_eurusd()), MIDWEEK)
    assert loss == pytest.approx(2.0 + 7.0)


def test_standard_midweek_round_trip_is_one_pip_and_no_commission():
    # 1 pip = 1e-4 x 100,000 units = 10 USD.
    assert _round_trip_loss(CostModel.pepperstone_standard(_eurusd()), MIDWEEK) == pytest.approx(10.0)


def test_presets_widen_at_the_sunday_open():
    # Razor spread 2 USD at x10 = 20 USD + 7 commission; Standard 10 USD x10 = 100 USD.
    assert _round_trip_loss(CostModel.pepperstone_razor(_eurusd()), SUNDAY) == pytest.approx(27.0)
    assert _round_trip_loss(CostModel.pepperstone_standard(_eurusd()), SUNDAY) == pytest.approx(100.0)


def test_cfd_default_cost_model_widens_the_spread_at_the_sunday_open():
    midweek = _round_trip_loss(CostModel.default_for(_eurusd()), MIDWEEK)
    sunday = _round_trip_loss(CostModel.default_for(_eurusd()), SUNDAY)
    assert sunday > midweek


# --- slice 5: stocks widen in the first minutes after the open -------------

def test_stock_spread_is_wider_in_the_first_five_minutes_after_the_open():
    from hermes import Stock
    from hermes.core import SessionCalendar

    stock = Stock(Symbol("AAPL", "yfinance"), session=SessionCalendar(NY, time(9, 30), time(16, 0)))
    model = SpreadModel(points=0.01, schedule=SpreadSchedule.us_equity_open(minutes=5, multiplier=3))

    def buy(ts):  # LAST basis: half the spread each side of mid
        return model.adjust_fill(stock, Side.BUY, 100.0, Liquidity.TAKER, ts)

    assert buy(datetime(2023, 1, 3, 14, 30, tzinfo=UTC)) == pytest.approx(100.0 + 0.03 / 2)  # 09:30 NY
    assert buy(datetime(2023, 1, 3, 14, 34, tzinfo=UTC)) == pytest.approx(100.0 + 0.03 / 2)  # 09:34
    assert buy(datetime(2023, 1, 3, 14, 35, tzinfo=UTC)) == pytest.approx(100.0 + 0.01 / 2)  # 09:35
    assert buy(datetime(2023, 1, 1, 14, 30, tzinfo=UTC)) == pytest.approx(100.0 + 0.01 / 2)  # Sunday
