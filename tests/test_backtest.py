from datetime import UTC, datetime, timedelta

from hermes import SMA, Backtest, CryptoPair, Strategy, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.execution import (
    CostModel,
    FinancingModel,
    PercentCommission,
    SlippageModel,
    SpreadModel,
)

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _btc() -> CryptoPair:
    return CryptoPair(
        Symbol("BTCUSDT", "binance"), base_asset="BTC", quote_currency="USDT", tick_size=0.01
    )


def _zero_costs() -> CostModel:
    return CostModel(PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), FinancingModel(0.0))


def _bar(i, o, h, l, c):
    return Bar(T0 + timedelta(hours=i), H1, o, h, l, c, 1.0)


class BuyOnce(Strategy):
    def setup(self) -> None:
        self.done = False

    def on_bar(self, bar) -> None:
        if not self.done and self.venue.position().is_flat:
            self.buy(1, stop_loss=self.price - 10, take_profit=self.price + 10)
            self.done = True


def _run(bars, **kw):
    src = InMemorySource(_btc(), {H1: bars})
    bt = Backtest(
        strategy=BuyOnce(),
        source=src,
        symbol=Symbol("BTCUSDT", "binance"),
        timeframes=[H1],
        start=bars[0].timestamp,
        end=bars[-1].timestamp,
        starting_cash=10_000,
        cost_model=_zero_costs(),
        **kw,
    )
    return bt.run()


def test_take_profit_exit_pnl():
    bars = [
        _bar(0, 100, 100, 100, 100),  # decide to buy
        _bar(1, 100, 100, 100, 100),  # market fills at open=100
        _bar(2, 100, 115, 100, 105),  # high 115 >= TP 110 -> exit at 110
        _bar(3, 105, 105, 105, 105),
    ]
    result = _run(bars)
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.exit_reason == "take_profit"
    assert t.entry_price == 100
    assert t.exit_price == 110
    assert t.net_pnl == 10
    assert result.equity_curve[-1][1] == 10_010
    assert result.metrics.win_rate == 1.0


def test_stop_loss_exit_pnl():
    bars = [
        _bar(0, 100, 100, 100, 100),
        _bar(1, 100, 100, 100, 100),  # entry 100
        _bar(2, 100, 100, 85, 90),    # low 85 <= SL 90 -> exit at 90
        _bar(3, 90, 90, 90, 90),
    ]
    result = _run(bars)
    t = result.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.exit_price == 90
    assert t.net_pnl == -10
    assert result.equity_curve[-1][1] == 9_990


class TrendRider(Strategy):
    """Base 15m, an SMA(3) on the 1h series (Forming-Bar aware). Records warmup."""

    def setup(self):
        self.sma = self.use(SMA(Timeframe.parse("1h"), 3))
        self.first_on_bar = None
        self.sma_none_seen_after_start = False

    def on_bar(self, bar):
        if self.first_on_bar is None:
            self.first_on_bar = bar.timestamp
        if self.indicator_value(self.sma)["value"] is None:
            self.sma_none_seen_after_start = True


def test_multi_timeframe_warmup_suppresses_until_indicator_ready():
    m15 = Timeframe.parse("15m")
    bars = [_bar_15m(i, 100 + (i % 7)) for i in range(80)]
    src = InMemorySource(_btc(), {m15: bars})
    strat = TrendRider()
    bt = Backtest(
        strategy=strat,
        source=src,
        symbol=Symbol("BTCUSDT", "binance"),
        timeframes=[m15, Timeframe.parse("1h")],
        start=bars[0].timestamp,
        end=bars[-1].timestamp,
        cost_model=_zero_costs(),
    )
    result = bt.run()
    # on_bar never fired before the 1h SMA(3) was warm (needs ~3 hourly bars).
    assert strat.first_on_bar is not None
    assert strat.first_on_bar >= bars[0].timestamp + timedelta(hours=2)
    assert strat.sma_none_seen_after_start is False
    assert isinstance(result.metrics.num_trades, int)


def _bar_15m(i, price):
    ts = T0 + timedelta(minutes=15 * i)
    return Bar(ts, Timeframe.parse("15m"), price, price + 1, price - 1, price, 1.0)


class OpenThenSignalClose(Strategy):
    """Buy once (fills next open), then signal-close on the following bar."""

    def setup(self):
        self.opened = False

    def on_bar(self, bar):
        if not self.opened and self.venue.position().is_flat:
            self.buy(1)
            self.opened = True
        elif not self.venue.position().is_flat:
            for t in self.venue.open_trades():
                self.close(t)


def test_signal_close_exit_time_is_the_fill_bar_not_stale():
    # Regression: a signal-close must be stamped with the bar it fills on, not the
    # stale _last_bar (which produced zero-duration trades).
    bars = [_bar(i, 100, 100, 100, 100) for i in range(5)]
    src = InMemorySource(_btc(), {H1: bars})
    bt = Backtest(
        strategy=OpenThenSignalClose(), source=src, symbol=Symbol("BTCUSDT", "binance"),
        timeframes=[H1], start=bars[0].timestamp, end=bars[-1].timestamp,
        cost_model=_zero_costs(),
    )
    result = bt.run()
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.entry_time == bars[1].timestamp    # market submitted bar0, fills bar1 open
    assert t.exit_time == bars[2].timestamp     # close decided bar1, fills bar2 open
    assert t.exit_time > t.entry_time           # never zero-duration


def test_sl_tp_clash_defaults_to_stop_first():
    bars = [
        _bar(0, 100, 100, 100, 100),
        _bar(1, 100, 100, 100, 100),   # entry 100
        _bar(2, 100, 115, 85, 100),    # engulfs both SL(90) and TP(110)
        _bar(3, 100, 100, 100, 100),
    ]
    result = _run(bars)
    assert result.trades[0].exit_reason == "stop_loss"  # conservative default
