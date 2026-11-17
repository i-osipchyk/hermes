"""ADR-0015 (audit F8, F9, F14): the pessimistic fill model. Seam: ``SimulatedVenue``."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from hermes.core import Bar, Cfd, SessionCalendar, Symbol, Timeframe
from hermes.execution import (
    Account,
    CostModel,
    FinancingModel,
    PercentCommission,
    SimulatedVenue,
    SlippageModel,
    SpreadModel,
)
from hermes.execution.order import Order, OrderStatus, OrderType, Side

H1 = Timeframe.parse("1h")
T0 = datetime(2024, 1, 2, tzinfo=UTC)
SESSION = SessionCalendar(timezone=ZoneInfo("UTC"), weekdays=(0, 1, 2, 3, 4, 5, 6))


def _cfd(leverage=1.0):
    return Cfd(Symbol("XYZ", "t"), session=SESSION, tick_size=0.01, lot_size=1.0,
               leverage=leverage)


def _costs(spread=0.0, penetration=None):
    kw = {} if penetration is None else {"limit_penetration_ticks": penetration}
    return CostModel(PercentCommission(0.0), SpreadModel(spread), SlippageModel(0.0),
                     FinancingModel(0.0), **kw)


def _venue(spread=0.0, cash=100_000.0, leverage=1.0, **kw):
    return SimulatedVenue(_cfd(leverage), Account(cash), _costs(spread), **kw)


def _bar(i, o, h, l, c):
    return Bar(T0 + timedelta(hours=i), H1, o, h, l, c, 1.0)


def _prime(v, price=100.0):
    v.on_base_bar(_bar(0, price, price, price, price))


def _order(v, side, size=1.0, type=OrderType.MARKET, **kw):
    return v.submit(Order(instrument=v.instrument, side=side, size=size, type=type, **kw))


# --- F14: buy-side triggers see the ask ------------------------------------------

def test_short_stop_loss_triggers_when_the_ask_reaches_it():
    v = _venue(spread=2.0)
    _prime(v)
    _order(v, Side.SELL, stop_loss=105.0)
    v.on_base_bar(_bar(1, 100, 100, 100, 100))      # short opens at the bid, 100
    v.on_base_bar(_bar(2, 100, 103.5, 100, 101))    # bid high 103.5 -> ask 105.5 >= 105
    [t] = v.closed_trades
    assert t.exit_reason == "stop_loss"
    assert t.exit_price == 105.0                    # bought back at the ask level


def test_short_stop_loss_not_triggered_while_the_ask_is_below_it():
    v = _venue(spread=2.0)
    _prime(v)
    _order(v, Side.SELL, stop_loss=105.0)
    v.on_base_bar(_bar(1, 100, 100, 100, 100))
    v.on_base_bar(_bar(2, 100, 102.9, 100, 101))    # ask high 104.9 < 105
    assert v.closed_trades == []


def test_buy_limit_entry_needs_the_ask_to_come_down_to_it():
    v = _venue(spread=2.0)
    _prime(v)
    _order(v, Side.BUY, type=OrderType.LIMIT, limit_price=95.0)
    v.on_base_bar(_bar(1, 100, 100, 94.0, 96))      # bid low 94 -> ask 96 > 95: no fill
    assert v.open_trades() == []
    v.on_base_bar(_bar(2, 100, 100, 92.9, 96))      # ask 94.9 < 95 - tick: fills
    [t] = v.open_trades()
    assert t.entry_price == 95.0                    # maker: its own price, no spread


def test_short_take_profit_is_a_buy_limit_and_needs_the_ask():
    v = _venue(spread=2.0)
    _prime(v)
    _order(v, Side.SELL, take_profit=90.0)
    v.on_base_bar(_bar(1, 100, 100, 100, 100))
    v.on_base_bar(_bar(2, 100, 100, 89.0, 95))      # ask low 91 > 90
    assert v.closed_trades == []
    v.on_base_bar(_bar(3, 95, 95, 87.9, 90))        # ask low 89.9 < 90
    assert [t.exit_reason for t in v.closed_trades] == ["take_profit"]


def test_sell_side_triggers_are_unchanged_by_the_spread():
    v = _venue(spread=2.0)
    _prime(v)
    _order(v, Side.BUY, stop_loss=95.0)
    v.on_base_bar(_bar(1, 100, 100, 100, 100))
    v.on_base_bar(_bar(2, 100, 100, 94.9, 96))      # bid low 94.9 <= 95
    assert [t.exit_reason for t in v.closed_trades] == ["stop_loss"]


# --- F8: a resting limit needs penetration, not a touch --------------------------

def test_a_touch_does_not_fill_a_buy_limit_entry_but_trading_through_does():
    v = _venue()
    _prime(v)
    _order(v, Side.BUY, type=OrderType.LIMIT, limit_price=95.0)
    v.on_base_bar(_bar(1, 100, 100, 95.0, 97))      # exactly touches
    assert v.open_trades() == []
    v.on_base_bar(_bar(2, 100, 100, 94.9, 97))      # trades through
    assert v.open_trades()[0].entry_price == 95.0


def test_a_touch_does_not_take_profit_but_trading_through_does():
    v = _venue()
    _prime(v)
    _order(v, Side.BUY, take_profit=105.0)
    v.on_base_bar(_bar(1, 100, 100, 100, 100))
    v.on_base_bar(_bar(2, 100, 105.0, 100, 104))
    assert v.closed_trades == []
    v.on_base_bar(_bar(3, 104, 105.1, 104, 105))
    [t] = v.closed_trades
    assert (t.exit_reason, t.exit_price) == ("take_profit", 105.0)


def test_zero_penetration_restores_touch_fills():
    v = SimulatedVenue(_cfd(), Account(100_000.0), _costs(penetration=0.0))
    _prime(v)
    _order(v, Side.BUY, type=OrderType.LIMIT, limit_price=95.0)
    v.on_base_bar(_bar(1, 100, 100, 95.0, 97))
    assert len(v.open_trades()) == 1


def test_a_gap_through_a_limit_still_fills_at_the_open():
    v = _venue()
    _prime(v)
    _order(v, Side.BUY, type=OrderType.LIMIT, limit_price=95.0)
    v.on_base_bar(_bar(1, 90, 92, 89, 91))
    assert v.open_trades()[0].entry_price == 90.0


def test_stops_still_trigger_on_a_touch():
    v = _venue()
    _prime(v)
    _order(v, Side.BUY, stop_loss=95.0)
    v.on_base_bar(_bar(1, 100, 100, 100, 100))
    v.on_base_bar(_bar(2, 100, 100, 95.0, 96))
    assert [t.exit_reason for t in v.closed_trades] == ["stop_loss"]


# --- F9: margin at the fill, and a floor -----------------------------------------

def test_an_order_that_became_unaffordable_at_the_gap_open_is_rejected():
    v = _venue(cash=1_000.0)
    _prime(v)
    o = _order(v, Side.BUY, size=10)                # 10 x 100 = 1000 margin: just fits
    assert o.status is OrderStatus.WORKING
    v.on_base_bar(_bar(1, 120, 121, 119, 120))      # fills at 120 -> 1200 > 1000
    assert v.open_trades() == []
    assert o.status is OrderStatus.REJECTED


def test_an_affordable_order_still_fills_at_the_open():
    v = _venue(cash=1_000.0)
    _prime(v)
    _order(v, Side.BUY, size=10)
    v.on_base_bar(_bar(1, 100, 101, 99, 100))
    assert len(v.open_trades()) == 1


def test_equity_at_or_below_zero_liquidates_open_trades():
    v = _venue(cash=1_000.0, leverage=10.0)
    _prime(v)
    _order(v, Side.BUY, size=100)                   # notional 10,000, margin 1,000
    v.on_base_bar(_bar(1, 100, 100, 100, 100))
    v.on_base_bar(_bar(2, 100, 100, 85, 85))        # -1,500 -> equity -500
    [t] = v.closed_trades
    assert t.exit_reason == "liquidation"
    assert t.exit_price == 85.0
    assert v.open_trades() == []


def test_a_drawdown_that_leaves_equity_positive_is_not_liquidated():
    v = _venue(cash=1_000.0, leverage=10.0)
    _prime(v)
    _order(v, Side.BUY, size=100)
    v.on_base_bar(_bar(1, 100, 100, 100, 100))
    v.on_base_bar(_bar(2, 100, 100, 95, 95))        # -500 -> equity 500
    assert v.closed_trades == [] and len(v.open_trades()) == 1


def test_unconstrained_runs_are_not_liquidated():
    v = _venue(cash=1_000.0, leverage=10.0, unconstrained=True)
    _prime(v)
    _order(v, Side.BUY, size=100)
    v.on_base_bar(_bar(1, 100, 100, 100, 100))
    v.on_base_bar(_bar(2, 100, 100, 85, 85))
    assert v.closed_trades == []
