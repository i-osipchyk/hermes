"""F18 and F12 (docs/audit-2026-11.md). Seams: ``Backtest.run`` / ``PortfolioBacktest.run``.

F18: at the first trading step a ``latest``-mode indicator was seeded from closed bars only,
ignoring the Forming Bar that every later step includes (ADR-0002).
F12: the equity curve's final point (after ``force_close_all``) reused the last bar's
timestamp, adding a zero-duration step that carried only the exit costs.
"""

from datetime import UTC, datetime, timedelta

from hermes import SMA, Backtest, CryptoPair, PortfolioBacktest, Strategy, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import InMemorySource
from hermes.execution import CostModel, FinancingModel, PercentCommission, SlippageModel, SpreadModel

H1 = Timeframe.parse("1h")
D1 = Timeframe.parse("1d")
T0 = datetime(2023, 1, 1, tzinfo=UTC)


def _inst(t="X"):
    return CryptoPair(Symbol(t, "t"), base_asset=t, quote_currency="USD", tick_size=0.01)


def _free():
    return CostModel(PercentCommission(0.0), SpreadModel(0.0), SlippageModel(0.0, 0.0),
                     FinancingModel(0.0))


def _bars():
    """Hourly bars; day d trades at base[d] + hour, so each day's close is base[d] + 23."""
    base = {0: 10, 1: 20, 2: 30}
    return [Bar(T0 + timedelta(hours=h), H1, base[h // 24] + h % 24, base[h // 24] + h % 24,
                base[h // 24] + h % 24, base[h // 24] + h % 24, 1.0) for h in range(72)]


class _Probe(Strategy):
    mode = "latest"

    def setup(self):
        self.sma = self.use(SMA(D1, 2, mode=self.mode))
        self.seen = []

    def on_bar(self, bar):
        self.seen.append((bar.timestamp, self.indicator_value(self.sma)["value"]))


def _run(strategy, start_hour=50):
    return Backtest(
        strategy=strategy, source=InMemorySource(_inst(), {H1: _bars()}),
        symbol=Symbol("X", "t"), timeframes=[H1, D1], start=T0 + timedelta(hours=start_hour),
        end=T0 + timedelta(hours=71), starting_cash=10_000, cost_model=_free(),
    )


# --- F18 -------------------------------------------------------------------------

def test_a_latest_indicator_includes_the_forming_bar_at_the_very_first_step():
    s = _Probe()
    _run(s).run()
    # Hour 50 is 02:00 on day 2. Closed day 1 closes at 43; the forming day 2 is at 32.
    assert s.seen[0] == (T0 + timedelta(hours=50), (43 + 32) / 2)       # was (33 + 43) / 2
    assert s.seen[1][1] == (43 + 33) / 2                                # unchanged later


def test_the_first_step_agrees_with_what_an_incremental_run_would_have_shown():
    """A run starting later must give the same value at hour 50 as one that already
    traded through it: the first step is not special."""
    early, late = _Probe(), _Probe()
    _run(early, start_hour=49).run()
    _run(late, start_hour=50).run()
    e = dict(early.seen)
    assert late.seen[0][1] == e[late.seen[0][0]]


def test_latest_confirmed_indicators_are_unchanged():
    class Confirmed(_Probe):
        mode = "latest_confirmed"

    s = Confirmed()
    _run(s).run()
    assert s.seen[0][1] == (33 + 43) / 2        # closed bars only, by definition


def test_a_reference_indicator_includes_its_forming_bar_at_the_first_step():
    class RefProbe(Strategy):
        def setup(self):
            # H1 reference base with a D1 indicator: the D1 bar is aggregated, so it has a
            # Forming Bar mid-day (a D1-only reference would have none).
            self.ref = self.use_reference("R", source=InMemorySource(_inst("R"), {H1: _bars()}))
            self.ref.use(SMA(H1, 1))
            self.rsma = self.ref.use(SMA(D1, 2, mode="latest"))
            self.seen = []

        def on_bar(self, bar):
            self.seen.append(self.ref.value(self.rsma)["value"])

    s = RefProbe()
    Backtest(strategy=s, source=InMemorySource(_inst(), {H1: _bars()}), symbol=Symbol("X", "t"),
             timeframes=[H1], start=T0 + timedelta(hours=50), end=T0 + timedelta(hours=71),
             starting_cash=10_000, cost_model=_free()).run()
    assert s.seen[0] == (43 + 32) / 2
    assert s.seen[1] == (43 + 33) / 2


def test_the_portfolio_loop_has_the_same_first_step():
    s = _Probe()
    PortfolioBacktest(legs=[_run(s)], starting_cash=10_000).run()
    assert s.seen[0][1] == (43 + 32) / 2


# --- F12 -------------------------------------------------------------------------

class _BuyHold(Strategy):
    def setup(self):
        pass

    def on_bar(self, bar):
        if self.venue.position().is_flat:
            self.buy(1)


def _costly_run():
    costs = CostModel(PercentCommission(0.001), SpreadModel(0.0), SlippageModel(0.0, 0.0),
                      FinancingModel(0.0))
    bars = [Bar(T0 + timedelta(hours=i), H1, 100.0, 100.0, 100.0, 100.0, 1.0) for i in range(6)]
    return Backtest(strategy=_BuyHold(), source=InMemorySource(_inst(), {H1: bars}),
                    symbol=Symbol("X", "t"), timeframes=[H1], start=bars[0].timestamp,
                    end=bars[-1].timestamp, starting_cash=10_000, cost_model=costs).run()


def test_equity_curve_has_one_point_per_timestamp():
    curve = _costly_run().equity_curve
    stamps = [t for t, _ in curve]
    assert len(stamps) == len(set(stamps))
    assert stamps == sorted(stamps)


def test_the_final_point_is_equity_after_the_exit_costs_were_paid():
    r = _costly_run()
    assert r.trades, "premise: the position was force-closed at the end"
    assert r.equity_curve[-1][1] == 10_000 + sum(t.net_pnl for t in r.trades)
    # and it is lower than mark-to-market would have been: the exit commission is in it
    assert r.equity_curve[-1][1] < r.equity_curve[-2][1]
