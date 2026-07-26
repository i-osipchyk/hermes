from datetime import UTC, datetime, timedelta

import pytest

from hermes import Backtest, CryptoPerpetual, Strategy, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.execution import (
    CostModel,
    FinancingModel,
    Liquidity,
    MakerTakerCommission,
    OrderType,
    Side,
    SlippageModel,
    SpreadModel,
)

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _perp():
    return CryptoPerpetual(
        Symbol("BTCUSDT", "binance-futures"), base_asset="BTC", quote_currency="USDT", tick_size=0.1
    )


def _bar(i, o, h, l, c):
    return Bar(T0 + timedelta(hours=i), H1, o, h, l, c, 1.0)


# --- unit: liquidity-aware costs -------------------------------------------

def test_maker_taker_commission_picks_rate_and_allows_rebate():
    inst = _perp()
    c = MakerTakerCommission(maker_rate=-0.0002, taker_rate=0.0005)  # notional 200 @ (100,2)
    assert c.commission(inst, 100, 2, Liquidity.TAKER) == pytest.approx(200 * 0.0005)
    assert c.commission(inst, 100, 2, Liquidity.MAKER) == pytest.approx(200 * -0.0002)  # rebate


def test_slippage_skips_maker_fills():
    inst, s = _perp(), SlippageModel(percent=0.0002)
    assert s.adjust_fill(inst, Side.BUY, 100.0, Liquidity.TAKER) == pytest.approx(100.02)
    assert s.adjust_fill(inst, Side.BUY, 100.0, Liquidity.MAKER) == 100.0  # limit fills at price


def test_spread_skips_maker_fills():
    inst, sp = _perp(), SpreadModel(points=1.0)
    assert sp.adjust_fill(inst, Side.BUY, 100.0, Liquidity.TAKER) == pytest.approx(100.5)
    assert sp.adjust_fill(inst, Side.BUY, 100.0, Liquidity.MAKER) == 100.0


# --- integration: a limit entry fills at its price (no slippage) + maker rebate ---

class LimitBuyOnce(Strategy):
    def setup(self):
        self.done = False

    def on_bar(self, bar):
        if not self.done and self.venue.position().is_flat:
            self.buy(1, type=OrderType.LIMIT, limit=98, stop_loss=95, take_profit=104)
            self.done = True


def test_limit_entry_has_no_slippage_and_earns_maker_rebate():
    cost = CostModel(
        commission=MakerTakerCommission(maker_rate=-0.0002, taker_rate=0.0005),
        spread=SpreadModel(0.0),
        slippage=SlippageModel(percent=0.001),  # 0.1% — would move a taker fill, not a maker
        financing=FinancingModel(0.0),
    )
    bars = [
        _bar(0, 100, 100, 100, 100),  # place BUY LIMIT @ 98
        _bar(1, 100, 100, 97, 99),    # low 97 <= 98 -> limit fills at 98
        _bar(2, 99, 105, 99, 104),    # high 105 >= TP 104 -> take-profit (maker) at 104
        _bar(3, 104, 104, 104, 104),
    ]
    bt = Backtest(
        strategy=LimitBuyOnce(), source=InMemorySource(_perp(), {H1: bars}),
        symbol=Symbol("BTCUSDT", "binance-futures"), timeframes=[H1],
        start=bars[0].timestamp, end=bars[-1].timestamp, starting_cash=10_000, cost_model=cost,
    )
    t = bt.run().trades[0]
    assert t.entry_price == 98.0    # exact — a maker fill takes no slippage despite 0.1% model
    assert t.exit_price == 104.0    # take-profit is also a maker fill
    assert t.costs < 0              # maker rebates net to a credit, not a charge
    assert t.net_pnl > t.gross_pnl  # rebate boosts the result above raw price P&L
