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


# --- combined portfolio (basket as one) ------------------------------------

def test_combined_equity_curve_sums_symbols():
    from hermes.backtest import BatchResult
    from hermes.backtest.result import BacktestResult

    t0, t1 = T0, T0 + timedelta(hours=1)
    a = BacktestResult.compute([(t0, 100.0), (t1, 110.0)], [])
    b = BacktestResult.compute([(t0, 100.0), (t1, 90.0)], [])
    batch = BatchResult(results={"A": a, "B": b})

    curve = batch.combined_equity_curve()
    assert curve[0][1] == 200.0            # 100 + 100
    assert curve[1][1] == 200.0            # 110 + 90
    assert batch.combined_result().metrics.total_return == 0.0  # 200 -> 200


def test_combined_forward_fills_misaligned_timestamps():
    from hermes.backtest import BatchResult
    from hermes.backtest.result import BacktestResult

    t0, t1, t2 = T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    a = BacktestResult.compute([(t0, 100.0), (t2, 120.0)], [])   # no point at t1
    b = BacktestResult.compute([(t1, 100.0), (t2, 80.0)], [])    # starts at t1
    batch = BatchResult(results={"A": a, "B": b})

    by_ts = dict(batch.combined_equity_curve())
    assert by_ts[t0] == 200.0   # A=100, B back-filled to its start 100
    assert by_ts[t1] == 200.0   # A carried 100, B=100
    assert by_ts[t2] == 200.0   # 120 + 80


def test_combined_result_pools_all_trades():
    batch = run_batch(["AAA", "BBB"], _build)
    combined = batch.combined_result()
    assert combined.metrics.num_trades == 2          # both symbols' trades pooled
    assert len(combined.equity_curve) > 0


# --- run_universe splits the total cash across tickers ---------------------

_UNIVERSE_STRATEGY = '''
from datetime import datetime, timezone, timedelta
from hermes import Backtest, Strategy, Symbol, Timeframe
from hermes.core import Bar, CryptoPair
from hermes.data import InMemorySource

H1 = Timeframe.parse("1h")


class S(Strategy):
    def setup(self): ...
    def on_bar(self, bar): ...


def build_backtest(**overrides):
    inst = CryptoPair(Symbol("X", "binance"), base_asset="X", quote_currency="USDT", tick_size=0.01)
    t0 = datetime(2023, 1, 1, tzinfo=timezone.utc)
    bars = [Bar(t0 + timedelta(hours=i), H1, 100, 100, 100, 100, 1.0) for i in range(4)]
    bt = Backtest(strategy=S(), source=InMemorySource(inst, {H1: bars}),
                  symbol=Symbol("X", "binance"), timeframes=[H1])
    for k, v in overrides.items():
        setattr(bt, k, v)
    return bt
'''


def test_run_universe_splits_total_cash(tmp_path):
    from hermes.webui import discovery

    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "u.py").write_text(_UNIVERSE_STRATEGY)
    entry = discovery.discover(sdir)[0]

    batch = discovery.run_universe(
        entry, tickers=["A", "B"], source_name=None,
        start=datetime(2023, 1, 1, tzinfo=UTC), end=datetime(2023, 1, 1, 3, tzinfo=UTC),
        starting_cash=1_000,
    )
    # each sleeve funded with total / N = 500
    for r in batch.results.values():
        assert r.equity_curve[0][1] == 500.0
    # combined portfolio starts at the full total
    assert batch.combined_equity_curve()[0][1] == 1_000.0


# --- universe (ticker list) loading ----------------------------------------

def test_universe_loading(tmp_path):
    d = tmp_path / "tickers"
    d.mkdir()
    (d / "stocks.json").write_text('{"source": "yfinance", "tickers": ["AAPL", "MSFT"]}')
    (d / "crypto.json").write_text('["BTCUSDT", "ETHUSDT"]')

    assert universes.universe_names(d) == ["crypto", "stocks"]
    assert universes.load_universe("stocks", d) == ("yfinance", ["AAPL", "MSFT"])
    assert universes.load_universe("crypto", d) == (None, ["BTCUSDT", "ETHUSDT"])
