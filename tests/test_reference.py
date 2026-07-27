from datetime import UTC, datetime, timedelta

from hermes import SMA, Backtest, CryptoPair, Strategy, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import InMemorySource

D1 = Timeframe.parse("1d")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _inst(name):
    return CryptoPair(Symbol(name, "binance"), base_asset=name, quote_currency="USDT", tick_size=0.01)


def _daily(closes):
    return [Bar(T0 + timedelta(days=i), D1, c, c, c, c, 1.0) for i, c in enumerate(closes)]


class RefRecorder(Strategy):
    def __init__(self, spy_source):
        super().__init__()
        self._spy_source = spy_source

    def setup(self):
        self.spy = self.use_reference("SPY", source=self._spy_source)
        self.spy_sma = self.spy.use(SMA(D1, 2))
        self.seen = []  # (main_ts, spy_close_seen, spy_sma)

    def on_bar(self, bar):
        series = self.spy.data(D1).closed()
        self.seen.append((bar.timestamp, series[-1].close, self.spy.value(self.spy_sma)["value"]))


def test_reference_feed_aligned_and_warmed():
    main = InMemorySource(_inst("MAIN"), {D1: _daily([50, 50, 50, 50, 50, 50])})
    spy = InMemorySource(_inst("SPY"), {D1: _daily([100, 101, 102, 103, 104, 105])})
    strat = RefRecorder(spy)
    Backtest(
        strategy=strat, source=main, symbol=Symbol("MAIN", "binance"),
        timeframes=[D1], start=T0, end=T0 + timedelta(days=5),
    ).run()

    # on_bar suppressed until the reference's SMA(2) is warm -> first fire on day index 1.
    assert strat.seen[0][0] == T0 + timedelta(days=1)
    # SPY value is aligned to the current bar (no look-ahead) and the indicator computes.
    assert strat.seen[0][1] == 101.0            # SPY close on day 1, not a future value
    assert strat.seen[0][2] == 100.5            # SMA(2) of 100,101
    # Last step sees SPY's latest close, still no look-ahead.
    assert strat.seen[-1][0] == T0 + timedelta(days=5)
    assert strat.seen[-1][1] == 105.0


def test_reference_defaults_to_main_source_when_none():
    # No source override -> reference uses the Backtest's source (same InMemorySource here,
    # which returns its instrument for any symbol).
    src = InMemorySource(_inst("X"), {D1: _daily([10, 11, 12, 13])})

    class S(Strategy):
        def setup(self):
            self.ref = self.use_reference("X")  # no source -> main source
            self.ref.use(SMA(D1, 2))
            self.ok = False

        def on_bar(self, bar):
            self.ok = self.ref.data(D1).closed()[-1].close is not None

    s = S()
    Backtest(strategy=s, source=src, symbol=Symbol("X", "binance"),
             timeframes=[D1], start=T0, end=T0 + timedelta(days=3)).run()
    assert s.ok
