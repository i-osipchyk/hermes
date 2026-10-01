"""P&L is reported in the account currency (USD), not the pair's quote currency.

Expected values are worked by hand from the bars below, not recomputed with engine code.
"""

from datetime import UTC, datetime, timedelta

import pytest

from hermes import (
    Backtest,
    Cfd,
    PortfolioBacktest,
    RiskPercent,
    Strategy,
    Symbol,
    Timeframe,
    regime_analysis,
)
from hermes.backtest import BacktestResult
from hermes.core import Bar
from hermes.data import DataSource, InMemorySource
from hermes.data.sources.ctrader_source import _CFD_SESSION
from hermes.execution import (
    CostModel,
    FinancingModel,
    PercentCommission,
    Side,
    SlippageModel,
    SpreadModel,
    Trade,
)

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)  # a Monday


def _zero_costs() -> CostModel:
    return CostModel(PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), FinancingModel(0.0))


def _fx(ticker: str, tick_size: float = 1e-5) -> Cfd:
    return Cfd(Symbol(ticker, "pepperstone"), session=_CFD_SESSION, tick_size=tick_size,
               lot_size=100_000, leverage=30)


def _flat_then(i_last_close: float, flat: float):
    """Four hourly bars: three flat at ``flat``, the last closes at ``i_last_close``."""
    def bar(i, o, h, l, c):
        return Bar(T0 + timedelta(hours=i), H1, o, h, l, c, 1.0)
    return [
        bar(0, flat, flat, flat, flat),
        bar(1, flat, flat, flat, flat),
        bar(2, flat, flat, flat, flat),
        bar(3, flat, max(flat, i_last_close), min(flat, i_last_close), i_last_close),
    ]


class _PairSource(DataSource):
    """Serves several FX pairs, as a real broker source does."""

    name = "pepperstone"

    def __init__(self, pairs: dict[str, tuple[Cfd, list[Bar]]]) -> None:
        self._pairs = pairs

    def get_instrument(self, symbol):
        return self._pairs[symbol.ticker][0]

    def history(self, instrument, timeframe, start, end):
        bars = self._pairs[instrument.symbol.ticker][1]
        return [b for b in bars if start <= b.timestamp <= end]

    def supported_timeframes(self):
        return {H1}


def _closes(closes: list[float]):
    """Flat hourly bars, one per close (open = high = low = close)."""
    return [Bar(T0 + timedelta(hours=i), H1, c, c, c, c, 1.0) for i, c in enumerate(closes)]


class BuyLots(Strategy):
    def __init__(self, lots: float = 1.0) -> None:
        super().__init__()
        self.lots = lots

    def setup(self) -> None:
        self.done = False

    def on_bar(self, bar) -> None:
        if not self.done:
            self.buy(self.lots)
            self.done = True


def _run(instrument, bars, lots=1.0, **kw):
    src = InMemorySource(instrument, {H1: bars})
    return Backtest(
        strategy=BuyLots(lots), source=src, symbol=instrument.symbol, timeframes=[H1],
        start=bars[0].timestamp, end=bars[-1].timestamp,
        starting_cash=10_000, **{"cost_model": _zero_costs(), **kw},
    ).run()


def test_usdjpy_pnl_is_converted_from_jpy_to_usd_at_the_last_closed_rate():
    # Long 1 lot (100,000 USD) from 150.00 to 151.00 = +100,000 JPY.
    # The last *closed* USDJPY bar before the exit is bar 2, close 150.00,
    # so 1 JPY = 1/150 USD  ->  100,000 / 150 = 666.67 USD.
    # (Not 100,000 "USD" as before; not 662.25, which would use the exit bar's
    # own close of 151.00 -- a value that is not yet known when the exit fills.)
    result = _run(_fx("USDJPY", tick_size=0.001), _flat_then(151.0, 150.0), unconstrained=True)

    trade = result.trades[0]
    assert trade.net_pnl == pytest.approx(100_000 / 150)
    assert result.equity_curve[-1][1] == pytest.approx(10_000 + 100_000 / 150)


def test_usdjpy_margin_is_reserved_in_usd_not_jpy():
    # 1 lot at 150.00 = 15,000,000 JPY notional = 100,000 USD; at 30x that is
    # 3,333 USD of margin -- affordable on 10,000 USD. Read as USD it would be
    # 500,000 and the order would be rejected.
    bars = _flat_then(150.0, 150.0)
    assert len(_run(_fx("USDJPY", tick_size=0.001), bars, lots=1).trades) == 1
    # 4 lots need 13,333 USD of margin: genuinely unaffordable, still rejected.
    assert _run(_fx("USDJPY", tick_size=0.001), bars, lots=4).trades == []


def test_usdjpy_percent_commission_is_charged_in_usd():
    # 0.1% of a 15,000,000 JPY notional = 15,000 JPY per side = 100 USD at 150.
    # Round trip (price unchanged, so no P&L) costs 200 USD, not 30,000.
    costs = CostModel(PercentCommission(0.001), SpreadModel(0.0), SlippageModel(0.0, 0.0),
                      FinancingModel(0.0))
    result = _run(_fx("USDJPY", tick_size=0.001), _flat_then(150.0, 150.0),
                  unconstrained=True, cost_model=costs)

    assert result.trades[0].net_pnl == pytest.approx(-200.0)
    assert result.equity_curve[-1][1] == pytest.approx(9_800.0)


def test_usdjpy_financing_is_charged_in_usd():
    # 30 flat hourly bars at 150.00 cross two New York midnights while the position is
    # open. 3.65%/yr on a 15,000,000 JPY notional = 1,500 JPY per night = 10 USD,
    # so two nights cost 20 USD (not 3,000).
    def bar(i):
        return Bar(T0 + timedelta(hours=i), H1, 150.0, 150.0, 150.0, 150.0, 1.0)
    costs = CostModel(PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0),
                      FinancingModel(0.0365))
    result = _run(_fx("USDJPY", tick_size=0.001), [bar(i) for i in range(30)],
                  unconstrained=True, cost_model=costs)

    assert result.equity_curve[-1][1] == pytest.approx(10_000 - 20.0)


def test_cross_pair_pnl_is_converted_through_the_quote_currencys_usd_pair():
    # EURGBP long 1 lot (100,000 EUR) from 0.8500 to 0.8600 = +1,000 GBP.
    # GBPUSD's last closed bar before the exit is 1.25 -> 1,250 USD. Its final bar
    # (1.40) has not closed when the exit fills and must not be used.
    eurgbp = _fx("EURGBP")
    gbpusd = _fx("GBPUSD")
    src = _PairSource({
        "EURGBP": (eurgbp, _closes([0.85, 0.85, 0.85, 0.86])),
        "GBPUSD": (gbpusd, _closes([1.25, 1.25, 1.25, 1.40])),
    })
    result = Backtest(
        strategy=BuyLots(1), source=src, symbol=eurgbp.symbol, timeframes=[H1],
        start=T0, end=T0 + timedelta(hours=3), starting_cash=10_000,
        cost_model=_zero_costs(), unconstrained=True,
    ).run()

    assert result.trades[0].net_pnl == pytest.approx(1_250.0)


def test_risk_percent_sizing_converts_per_lot_risk_to_usd():
    # Risk 1% of 10,000 USD = 100 USD with a 1.00 JPY stop. One lot loses
    # 100,000 JPY = 666.67 USD on that move (at 150), so the right size is
    # 100 / 666.67 = 0.15 lots. Unconverted it would size 0.001 lots and round to nothing.
    class RiskOnce(BuyLots):
        def on_bar(self, bar) -> None:
            if not self.done:
                self.buy(RiskPercent(0.01), stop_loss=bar.close - 1.0)
                self.done = True

    bars = _flat_then(150.0, 150.0)
    inst = _fx("USDJPY", tick_size=0.001)
    result = Backtest(
        strategy=RiskOnce(), source=InMemorySource(inst, {H1: bars}), symbol=inst.symbol,
        timeframes=[H1], start=bars[0].timestamp, end=bars[-1].timestamp,
        starting_cash=10_000, cost_model=_zero_costs(),
    ).run()

    assert result.trades[0].size == pytest.approx(0.15)


def test_missing_conversion_data_raises_instead_of_booking_gbp_as_usd():
    eurgbp = _fx("EURGBP")
    src = _PairSource({
        "EURGBP": (eurgbp, _closes([0.85, 0.85, 0.85, 0.86])),
        "GBPUSD": (_fx("GBPUSD"), []),
    })
    bt = Backtest(
        strategy=BuyLots(1), source=src, symbol=eurgbp.symbol, timeframes=[H1],
        start=T0, end=T0 + timedelta(hours=3), cost_model=_zero_costs(), unconstrained=True,
    )

    with pytest.raises(ValueError, match="GBPUSD"):
        bt.run()


def _portfolio_legs():
    """USDJPY (+100,000 JPY) and EURGBP (+1,000 GBP) legs over the same four hours."""
    usdjpy = _fx("USDJPY", tick_size=0.001)
    eurgbp = _fx("EURGBP")
    jpy_bars = _flat_then(151.0, 150.0)
    gbp_src = _PairSource({
        "EURGBP": (eurgbp, _closes([0.85, 0.85, 0.85, 0.86])),
        "GBPUSD": (_fx("GBPUSD"), _closes([1.25, 1.25, 1.25, 1.40])),
    })
    common = dict(timeframes=[H1], start=T0, end=T0 + timedelta(hours=3),
                  cost_model=_zero_costs())
    return [
        Backtest(strategy=BuyLots(1), source=InMemorySource(usdjpy, {H1: jpy_bars}),
                 symbol=usdjpy.symbol, **common),
        Backtest(strategy=BuyLots(1), source=gbp_src, symbol=eurgbp.symbol, **common),
    ]


def test_portfolio_legs_book_pnl_in_usd():
    # 100,000 JPY / 150 = 666.67 USD plus 1,000 GBP * 1.25 = 1,250 USD.
    pr = PortfolioBacktest(legs=_portfolio_legs(), starting_cash=10_000, unconstrained=True).run()

    assert pr.result.equity_curve[-1][1] == pytest.approx(10_000 + 666.6667 + 1_250.0, abs=0.01)


def test_turnover_uses_usd_notional():
    # Traded notional in USD: 1 lot USDJPY at 150 = 15,000,000 JPY / 150 = 100,000 USD,
    # plus 1 lot EURGBP at 0.85 = 85,000 GBP * 1.25 = 106,250 USD  ->  206,250 USD.
    pr = PortfolioBacktest(legs=_portfolio_legs(), starting_cash=10_000, unconstrained=True).run()

    curve = [e for _, e in pr.result.equity_curve]
    avg_equity = sum(curve) / len(curve)
    expected = (206_250 / avg_equity) * (pr.result.steps_per_year / len(curve))
    assert pr.result.metrics.turnover == pytest.approx(expected)


def test_regime_sharpe_uses_usd_capital_at_risk():
    # Two 1-lot USDJPY longs opened in the same regime, at very different price levels.
    #   A: 100 -> 101 = +1,000 USD on 100,000 USD of notional  = +1%
    #   B: 200 -> 204 = +2,000 USD on 100,000 USD of notional  = +2%
    # Returns [0.01, 0.02]: mean 0.015, std 0.005, so Sharpe = 3 * sqrt(252).
    # Dividing USD P&L by JPY notional would give 1e-4 for both and no Sharpe at all.
    inst = _fx("USDJPY", tick_size=0.001)
    entry = T0 + timedelta(hours=9)

    def trade(price, exit_price, pnl_usd):
        return Trade(inst, Side.BUY, 1.0, price, entry, exit_price=exit_price,
                     exit_time=entry + timedelta(hours=1), gross_pnl=pnl_usd, costs=0.0,
                     quote_rate=1 / price)

    result = BacktestResult(
        trades=[trade(100.0, 101.0, 1_000.0), trade(200.0, 204.0, 2_000.0)],
        benchmark_equity=[(T0 + timedelta(hours=i), 100.0 + i) for i in range(10)],
    )
    analysis = regime_analysis(result, trend_lookback=3, vol_lookback=3)

    (bucket,) = [r for r in analysis.regimes if r.n_trades == 2]
    assert bucket.sharpe == pytest.approx(3 * 252 ** 0.5)
