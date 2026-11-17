"""F1 (docs/audit-2026-11.md): a Reference on a coarser timeframe than the base must not
show a bar's final close before that bar has closed (ADR-0002).

Bar timestamps are bar *open* times, so a D1 bar stamped 2023-01-03 00:00 closes at
2023-01-04 00:00. Seams: ``Backtest.run`` and ``PortfolioBacktest.run``.
"""

from datetime import UTC, datetime, timedelta

from hermes import SMA, Backtest, CryptoPair, PortfolioBacktest, Strategy, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.execution import CostModel, FinancingModel, PercentCommission, SlippageModel, SpreadModel

H1 = Timeframe.parse("1h")
D1 = Timeframe.parse("1d")
DAY0 = datetime(2023, 1, 2, tzinfo=UTC)   # reference close 100
DAY1 = datetime(2023, 1, 3, tzinfo=UTC)   # reference close 110 — only known at end of day


def _inst(ticker):
    return CryptoPair(
        Symbol(ticker, "binance"), base_asset=ticker, quote_currency="USDT", tick_size=0.01
    )


def _zero_costs():
    return CostModel(
        PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0), FinancingModel(0.0)
    )


def _flat(tf, ts, price):
    return Bar(ts, tf, price, price, price, price, 1.0)


class _Probe(Strategy):
    """Records the last *closed* D1 close of the reference at every base step."""

    def on_bar(self, bar):
        closed = self.ref.data(D1).closed()
        self.seen[bar.timestamp] = closed[-1].close if closed else float("nan")


def _sources():
    main = InMemorySource(
        _inst("MAIN"),
        {H1: [_flat(H1, DAY0 + timedelta(hours=h), 50.0) for h in range(48)]},
    )
    ref = InMemorySource(
        _inst("REF"),
        {D1: [_flat(D1, DAY0, 100.0), _flat(D1, DAY1, 110.0)]},
    )
    return main, ref


class _ProbeWithSource(_Probe):
    def setup(self):
        _main, ref = _sources()
        self.ref = self.use_reference("REF", source=ref)
        self.ref.use(SMA(D1, 1))
        self.seen = {}


def _assert_no_lookahead(seen):
    # Before day 1 has closed, the latest closed D1 bar is day 0 (close 100).
    assert seen[DAY1] == 100.0, "day 1's final close leaked at its first hourly step"
    assert seen[DAY1 + timedelta(hours=12)] == 100.0
    assert seen[DAY1 + timedelta(hours=22)] == 100.0
    # Once the last base bar of the day is in, day 1 is closed (the base bar's own close is
    # visible at its step, ADR-0002), so day 1 may appear now and not earlier.
    assert seen[DAY1 + timedelta(hours=23)] == 110.0


def test_coarser_reference_bar_is_not_visible_before_it_closes():
    strat = _ProbeWithSource()
    main, _ = _sources()
    Backtest(
        strategy=strat,
        source=main,
        symbol=Symbol("MAIN", "binance"),
        timeframes=[H1],
        start=DAY1,
        end=DAY1 + timedelta(hours=23),
        cost_model=_zero_costs(),
    ).run()
    _assert_no_lookahead(strat.seen)


def test_coarser_reference_bar_is_not_visible_before_it_closes_in_a_portfolio():
    strat = _ProbeWithSource()
    main, _ = _sources()
    leg = Backtest(
        strategy=strat,
        source=main,
        symbol=Symbol("MAIN", "binance"),
        timeframes=[H1],
        start=DAY1,
        end=DAY1 + timedelta(hours=23),
        cost_model=_zero_costs(),
    )
    PortfolioBacktest(legs=[leg], starting_cash=10_000).run()
    _assert_no_lookahead(strat.seen)


def test_same_timeframe_reference_is_unchanged():
    """A reference on the base timeframe still shows its bar at that bar's step."""

    class SameTf(Strategy):
        def setup(self):
            ref_src = InMemorySource(
                _inst("REF"), {H1: [_flat(H1, DAY0 + timedelta(hours=h), 100.0 + h) for h in range(48)]}
            )
            self.ref = self.use_reference("REF", source=ref_src)
            self.ref.use(SMA(H1, 1))
            self.seen = {}

        def on_bar(self, bar):
            self.seen[bar.timestamp] = self.ref.data(H1).closed()[-1].close

    strat = SameTf()
    main, _ = _sources()
    Backtest(
        strategy=strat, source=main, symbol=Symbol("MAIN", "binance"), timeframes=[H1],
        start=DAY1, end=DAY1 + timedelta(hours=5), cost_model=_zero_costs(),
    ).run()
    # hour 24 of the series is DAY1 00:00 -> close 124.0, visible at that very step.
    assert strat.seen[DAY1] == 124.0
    assert strat.seen[DAY1 + timedelta(hours=3)] == 127.0
