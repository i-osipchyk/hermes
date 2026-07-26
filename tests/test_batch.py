from datetime import UTC, datetime, timedelta

from hermes import Backtest, CryptoPair, Strategy, Symbol, Timeframe
from hermes.backtest import run_batch
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.execution import CostModel, FinancingModel, PercentCommission, SlippageModel, SpreadModel
from hermes.webui import universes

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _zero_costs():
    return CostModel(PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), FinancingModel(0.0))


def _inst(ticker):
    return CryptoPair(Symbol(ticker, "binance"), base_asset=ticker, quote_currency="USDT", tick_size=0.01)


def _bars():
    return [
        Bar(T0 + timedelta(hours=0), H1, 100, 100, 100, 100, 1.0),
        Bar(T0 + timedelta(hours=1), H1, 100, 100, 100, 100, 1.0),   # entry at 100
        Bar(T0 + timedelta(hours=2), H1, 100, 115, 100, 105, 1.0),   # high 115 -> TP 110
        Bar(T0 + timedelta(hours=3), H1, 105, 105, 105, 105, 1.0),
    ]


class BuyOnce(Strategy):
    def setup(self):
        self.done = False

    def on_bar(self, bar):
        if not self.done and self.venue.position().is_flat:
            self.buy(1, stop_loss=self.price - 10, take_profit=self.price + 10)
            self.done = True


def _build(ticker):
    if ticker == "BAD":
        raise ValueError("no data for BAD")
    return Backtest(
        strategy=BuyOnce(), source=InMemorySource(_inst(ticker), {H1: _bars()}),
        symbol=Symbol(ticker, "binance"), timeframes=[H1],
        start=T0, end=T0 + timedelta(hours=3), cost_model=_zero_costs(),
    )


# --- batch runner ----------------------------------------------------------

def test_run_batch_collects_results_and_errors():
    seen = []
    batch = run_batch(["AAA", "BBB", "BAD"], _build, progress=lambda d, t: seen.append((d, t)))

    assert set(batch.results) == {"AAA", "BBB"}
    assert set(batch.errors) == {"BAD"}
    assert batch.results["AAA"].trades[0].net_pnl == 10  # each symbol ran independently
    assert seen[0] == (0, 3) and seen[-1] == (3, 3)      # progress fired


def test_batch_summary_and_aggregate():
    batch = run_batch(["AAA", "BBB"], _build)
    rows = {r["symbol"]: r for r in batch.summary_rows()}
    assert set(rows) == {"AAA", "BBB"}
    assert rows["AAA"]["trades"] == 1

    agg = batch.aggregate()
    assert agg["symbols"] == 2
    assert agg["total_trades"] == 2
    assert agg["pct_profitable"] == 1.0  # both symbols profitable
    assert agg["errors"] == 0


# --- universe (ticker list) loading ----------------------------------------

def test_universe_loading(tmp_path):
    d = tmp_path / "tickers"
    d.mkdir()
    (d / "stocks.json").write_text('{"source": "yfinance", "tickers": ["AAPL", "MSFT"]}')
    (d / "crypto.json").write_text('["BTCUSDT", "ETHUSDT"]')

    assert universes.universe_names(d) == ["crypto", "stocks"]
    assert universes.load_universe("stocks", d) == ("yfinance", ["AAPL", "MSFT"])
    assert universes.load_universe("crypto", d) == (None, ["BTCUSDT", "ETHUSDT"])
