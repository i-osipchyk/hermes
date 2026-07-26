from datetime import UTC, datetime, timedelta

from hermes import (
    BinanceFuturesSource,
    BinanceSource,
    CryptoPair,
    CryptoPerpetual,
    Strategy,
    Symbol,
    Timeframe,
)
from hermes.backtest import Backtest
from hermes.core import AssetClass, Bar
from hermes.data import InMemorySource
from hermes.execution import (
    CostModel,
    FinancingModel,
    PercentCommission,
    Side,
    SlippageModel,
    SpreadModel,
)

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _zero_costs():
    return CostModel(PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), FinancingModel(0.0))


def _bar(i, o, h, l, c):
    return Bar(T0 + timedelta(hours=i), H1, o, h, l, c, 1.0)


# --- source config (offline) -----------------------------------------------

def test_futures_source_endpoints_and_cache_separation():
    s = BinanceFuturesSource(leverage=20)
    assert s.name == "binance-futures"
    assert s.base_url == "https://fapi.binance.com"
    assert s.klines_path == "/fapi/v1/klines"
    assert s.exchange_info_path == "/fapi/v1/exchangeInfo"
    # spot source is unchanged
    assert BinanceSource().base_url == "https://api.binance.com"


def test_futures_builds_leveraged_shortable_perpetual():
    s = BinanceFuturesSource(leverage=15)
    inst = s._make_instrument(Symbol("BTCUSDT", "binance-futures"), "BTC", "USDT", 0.01)
    assert isinstance(inst, CryptoPerpetual)
    assert inst.asset_class is AssetClass.CRYPTO_PERP
    assert inst.can_short is True
    assert inst.leverage == 15
    # spot remains a non-shortable CryptoPair
    spot = BinanceSource()._make_instrument(Symbol("BTCUSDT", "binance"), "BTC", "USDT", 0.01)
    assert isinstance(spot, CryptoPair)
    assert spot.can_short is False


def test_perp_cost_default_is_cheaper_than_spot():
    sym_f = Symbol("BTCUSDT", "binance-futures")
    sym_s = Symbol("BTCUSDT", "binance")
    perp = CryptoPerpetual(sym_f, base_asset="BTC", quote_currency="USDT", tick_size=0.01)
    spot = CryptoPair(sym_s, base_asset="BTC", quote_currency="USDT", tick_size=0.01)
    price, size = 100.0, 1.0
    perp_fee = CostModel.default_for(perp).commission.commission(perp, price, size)
    spot_fee = CostModel.default_for(spot).commission.commission(spot, price, size)
    assert perp_fee < spot_fee


# --- shorting works end-to-end (leverage + can_short) ----------------------

class ShortOnce(Strategy):
    def setup(self):
        self.done = False

    def on_bar(self, bar):
        if not self.done and self.venue.position().is_flat:
            self.sell(1, stop_loss=self.price + 10, take_profit=self.price - 10)
            self.done = True


def test_short_perpetual_take_profit():
    perp = CryptoPerpetual(
        Symbol("BTCUSDT", "binance-futures"), base_asset="BTC", quote_currency="USDT",
        tick_size=0.01, leverage=10,
    )
    bars = [
        _bar(0, 100, 100, 100, 100),  # decide short
        _bar(1, 100, 100, 100, 100),  # short fills at open 100
        _bar(2, 100, 100, 85, 90),    # low 85 <= TP 90 -> cover at 90
        _bar(3, 90, 90, 90, 90),
    ]
    bt = Backtest(
        strategy=ShortOnce(), source=InMemorySource(perp, {H1: bars}),
        symbol=Symbol("BTCUSDT", "binance-futures"), timeframes=[H1],
        start=bars[0].timestamp, end=bars[-1].timestamp,
        starting_cash=1_000, cost_model=_zero_costs(),
    )
    result = bt.run()
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.side is Side.SELL              # a real short (can_short worked)
    assert t.exit_reason == "take_profit"
    assert t.entry_price == 100 and t.exit_price == 90
    assert t.net_pnl == 10                  # short profits as price falls
