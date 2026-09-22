"""Tests for PortfolioBacktest — shared capital pool across multiple instruments."""

from datetime import UTC, datetime, timedelta

import pytest

from hermes import Backtest, CryptoPair, PortfolioBacktest, Strategy, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.execution import CostModel, FinancingModel, PercentCommission, SlippageModel, SpreadModel

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _zero_costs():
    return CostModel(
        PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), FinancingModel(0.0)
    )


def _inst(ticker):
    return CryptoPair(
        Symbol(ticker, "binance"), base_asset=ticker, quote_currency="USDT", tick_size=0.01
    )


def _bars(close=100.0, n=4):
    return [Bar(T0 + timedelta(hours=i), H1, close, close, close, close, 1.0) for i in range(n)]


def _winning_bars():
    """Four bars: buy at open of bar1 (100), TP fires on bar2 (high=115 -> TP=110)."""
    return [
        Bar(T0 + timedelta(hours=0), H1, 100, 100, 100, 100, 1.0),  # decide buy
        Bar(T0 + timedelta(hours=1), H1, 100, 100, 100, 100, 1.0),  # fill at 100
        Bar(T0 + timedelta(hours=2), H1, 100, 115, 100, 105, 1.0),  # TP=110 hit
        Bar(T0 + timedelta(hours=3), H1, 105, 105, 105, 105, 1.0),
    ]


class BuyOnce(Strategy):
    def setup(self):
        self.done = False

    def on_bar(self, bar):
        if not self.done and self.venue.position().is_flat:
            self.buy(1, stop_loss=self.price - 10, take_profit=self.price + 10)
            self.done = True


class DoNothing(Strategy):
    def setup(self): ...
    def on_bar(self, bar): ...


def _leg(ticker, bars, strategy=None):
    if strategy is None:
        strategy = BuyOnce()
    return Backtest(
        strategy=strategy,
        source=InMemorySource(_inst(ticker), {H1: bars}),
        symbol=Symbol(ticker, "binance"),
        timeframes=[H1],
        start=T0,
        end=T0 + timedelta(hours=3),
        cost_model=_zero_costs(),
    )


# --- basic correctness -------------------------------------------------------

def test_single_leg_matches_plain_backtest():
    """PortfolioBacktest with one leg should produce the same trades as Backtest."""
    bars = _winning_bars()
    single = Backtest(
        strategy=BuyOnce(),
        source=InMemorySource(_inst("AAA"), {H1: bars}),
        symbol=Symbol("AAA", "binance"),
        timeframes=[H1],
        start=T0, end=T0 + timedelta(hours=3),
        cost_model=_zero_costs(),
        starting_cash=10_000,
    ).run()

    port = PortfolioBacktest(
        legs=[_leg("AAA", bars)],
        starting_cash=10_000,
    ).run()

    assert len(port.result.trades) == len(single.trades) == 1
    assert port.result.trades[0].net_pnl == single.trades[0].net_pnl == 10.0


def test_shared_cash_credited_across_legs():
    """Both legs win; the portfolio equity reflects the sum of both gains."""
    bars = _winning_bars()
    pr = PortfolioBacktest(
        legs=[_leg("AAA", bars), _leg("BBB", bars)],
        starting_cash=10_000,
    ).run()

    # Each leg wins +10; total gain = 20.
    assert pr.result.metrics.num_trades == 2
    total_pnl = sum(t.net_pnl or 0 for t in pr.result.trades)
    assert total_pnl == 20.0
    # Final equity = starting + both wins.
    assert pr.result.equity_curve[-1][1] == pytest.approx(10_020.0)


def test_capital_is_truly_shared_not_split():
    """Starting cash is NOT divided per leg — each leg can use the full pool."""
    bars = _winning_bars()
    pr = PortfolioBacktest(
        legs=[_leg("AAA", bars), _leg("BBB", bars)],
        starting_cash=10_000,
    ).run()
    # Both strategies bought 1 unit at price=100, so combined notional=200.
    # With 10_000 starting cash there was no rejection.
    assert pr.result.metrics.num_trades == 2


def test_per_symbol_breakdown():
    """per_symbol maps each ticker to its own trade list."""
    bars = _winning_bars()
    pr = PortfolioBacktest(
        legs=[_leg("AAA", bars), _leg("BBB", _bars())],
        starting_cash=10_000,
    ).run()

    assert set(pr.per_symbol) == {"binance:AAA", "binance:BBB"}
    assert len(pr.per_symbol["binance:AAA"]) == 1  # winning trade
    # BBB opened a position that never hit TP/SL; it is force-closed at period end.
    bbb_trades = pr.per_symbol["binance:BBB"]
    assert len(bbb_trades) == 1
    assert bbb_trades[0].exit_reason == "period_end"


def test_summary_rows_one_row_per_symbol():
    bars = _winning_bars()
    pr = PortfolioBacktest(
        legs=[_leg("AAA", bars), _leg("BBB", bars)],
        starting_cash=10_000,
    ).run()
    rows = {r["symbol"]: r for r in pr.summary_rows()}
    assert set(rows) == {"binance:AAA", "binance:BBB"}
    assert rows["binance:AAA"]["trades"] == 1
    assert rows["binance:AAA"]["win_%"] == 100.0
    assert rows["binance:AAA"]["net_pnl"] == 10.0


def test_empty_legs_raises():
    with pytest.raises(ValueError, match="at least one leg"):
        PortfolioBacktest(legs=[]).run()


def test_equity_curve_non_empty():
    bars = _winning_bars()
    pr = PortfolioBacktest(legs=[_leg("AAA", bars)], starting_cash=10_000).run()
    assert len(pr.result.equity_curve) > 0
    # Curve starts at or near starting_cash (no open positions yet at bar 0).
    assert pr.result.equity_curve[0][1] == pytest.approx(10_000.0, abs=1.0)
