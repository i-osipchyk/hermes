"""``hermes run --universe`` and friends — universe runs through the CLI.

Three ways to name the symbols (a static ``tickers/*.json`` list, a calendar universe,
or a file's own ``build_universe_backtest``), all recorded in the ledger as one row
with ``ticker=None``. Everything runs on in-memory bars; the tests ``chdir`` into a tmp
project because universes resolve against the working directory, as the CLI does.
"""

from __future__ import annotations

import json

import pytest

from hermes.cli import EXIT_ERR, EXIT_OK, main

# Shared by every strategy file below: a source that serves any of three tickers, so
# the factory can build a leg for whichever symbol it is handed.
COMMON = '''
from datetime import date, datetime, timedelta, timezone
from hermes import Backtest, Parameter, Strategy, Symbol, Timeframe, Units, UniverseBacktest
from hermes.core import Bar, CryptoPair
from hermes.data import ConstituentCalendar, InMemorySource

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 1, tzinfo=timezone.utc)
TICKERS = {"AAA": 100.0, "BBB": 50.0, "CCC": 20.0}


class MultiSource:
    name = "memory"

    def __init__(self):
        self._by = {}
        for t, base in TICKERS.items():
            inst = CryptoPair(Symbol(t, "memory"), base_asset=t, quote_currency="USD",
                              tick_size=0.01)
            bars = [Bar(T0 + timedelta(hours=i), H1, base + i, base + i + 2, base + i - 1,
                        base + i + 1, 5.0) for i in range(60)]
            self._by[t] = InMemorySource(inst, {H1: bars})

    def get_instrument(self, symbol):
        return self._by[symbol.ticker].get_instrument(symbol)

    def history(self, instrument, timeframe, s, e):
        return self._by[instrument.symbol.ticker].history(instrument, timeframe, s, e)

    def supported_timeframes(self):
        return {H1}


class BuyAndHold(Strategy):
    def setup(self):
        self.hold = self.param(Parameter("hold", 2, bounds=(1, 5)))

    def on_bar(self, bar):
        if self.venue.position().is_flat:
            self.buy(Units(1), take_profit=bar.close * 1.01)
'''

SINGLE = COMMON + '''

def build_backtest(**overrides):
    kw = dict(strategy=BuyAndHold(), source=MultiSource(), symbol=Symbol("AAA", "memory"),
              timeframes=[H1], start=T0, end=T0 + timedelta(hours=59), starting_cash=10_000)
    kw.update(overrides)
    return Backtest(**kw)
'''

OWN = COMMON + '''

def build_universe_backtest(**overrides):
    kw = dict(strategy_factory=BuyAndHold, source=MultiSource(),
              calendar=ConstituentCalendar([(date(2023, 1, 1), ["AAA", "BBB", "CCC"])]),
              timeframes=[H1], start=T0, end=T0 + timedelta(hours=59), starting_cash=30_000)
    kw.update(overrides)
    return UniverseBacktest(**kw)
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "buyhold.py").write_text(SINGLE)
    (sdir / "buyhold_own.py").write_text(OWN)
    (tmp_path / "tickers").mkdir()
    (tmp_path / "tickers" / "pair.json").write_text(json.dumps(["AAA", "BBB"]))
    runs = tmp_path / "runs"
    return ["--dir", str(sdir), "--runs-dir", str(runs)], runs


def _json(capsys):
    return json.loads(capsys.readouterr().out)


def _symbols(d) -> set[str]:
    """Per-symbol rows are keyed ``source:ticker``, as PortfolioResult reports them."""
    return {r["symbol"].split(":")[-1] for r in d["per_symbol"]}


def test_strategies_marks_a_universe_file(project, capsys):
    common, _ = project
    assert main(["strategies", *common, "--json"]) == EXIT_OK
    by = {s["name"]: s for s in _json(capsys)["strategies"]}
    assert by["buyhold_own"]["universe"] is True
    assert by["buyhold_own"]["single_symbol"] is False
    assert by["buyhold"]["universe"] is False


def test_run_over_a_static_universe_records_one_row(project, capsys):
    common, runs = project
    assert main(["run", "buyhold", "--universe", "pair", *common, "--note", "why",
                 "--json"]) == EXIT_OK
    d = _json(capsys)
    assert d["universe"] == "pair"
    assert d["from_cache"] is False
    assert _symbols(d) == {"AAA", "BBB"}
    assert all(r["trades"] > 0 for r in d["per_symbol"])
    meta = json.loads((runs / d["run_key"] / "meta.json").read_text())
    assert meta["ticker"] is None and meta["universe"] == "pair" and meta["note"] == "why"
    assert (runs / d["run_key"] / "universe_meta.json").exists()


def test_a_second_identical_universe_run_is_served_from_the_ledger(project, capsys):
    common, _ = project
    main(["run", "buyhold", "--universe", "pair", *common, "--json"])
    first = _json(capsys)
    main(["run", "buyhold", "--universe", "pair", *common, "--json"])
    second = _json(capsys)
    assert second["from_cache"] is True
    assert second["run_key"] == first["run_key"]
    assert second["per_symbol"] == first["per_symbol"]


def test_a_universe_run_is_not_the_single_symbol_run(project, capsys):
    common, _ = project
    main(["run", "buyhold", *common, "--json"])
    single = _json(capsys)["run_key"]
    main(["run", "buyhold", "--universe", "pair", *common, "--json"])
    assert _json(capsys)["run_key"] != single


def test_run_over_a_calendar_universe(project, capsys, tmp_path):
    common, _ = project
    (tmp_path / "sp500_history.csv").write_text('date,tickers\n2023-01-01,"AAA,BBB,CCC"\n')
    assert main(["run", "buyhold", "--universe", "sp500", *common, "--json"]) == EXIT_OK
    d = _json(capsys)
    assert d["universe_size"] == 3
    assert _symbols(d) == {"AAA", "BBB", "CCC"}


def test_a_file_with_its_own_universe_runs_without_naming_one(project, capsys):
    common, runs = project
    assert main(["run", "buyhold_own", *common, "--json"]) == EXIT_OK
    d = _json(capsys)
    assert d["universe"] == "(own)"
    assert d["universe_size"] == 3
    assert json.loads((runs / d["run_key"] / "meta.json").read_text())["universe"] == "(own)"


def test_params_reach_every_leg_and_change_the_key(project, capsys):
    common, _ = project
    main(["run", "buyhold_own", *common, "--json"])
    a = _json(capsys)["run_key"]
    main(["run", "buyhold_own", *common, "-p", "hold=3", "--json"])
    assert _json(capsys)["run_key"] != a


def test_an_unknown_universe_lists_the_known_ones(project, capsys):
    common, _ = project
    assert main(["run", "buyhold", "--universe", "nope", *common]) == EXIT_ERR
    assert "pair" in capsys.readouterr().err


def test_symbol_and_universe_are_exclusive(project, capsys):
    common, _ = project
    assert main(["run", "buyhold", "--universe", "pair", "--symbol", "AAA", *common]) == EXIT_ERR


def test_a_universe_file_refuses_a_named_universe(project, capsys):
    common, _ = project
    assert main(["run", "buyhold_own", "--universe", "pair", *common]) == EXIT_ERR
    assert "own universe" in capsys.readouterr().err


def test_show_includes_the_per_symbol_breakdown(project, capsys):
    common, _ = project
    main(["run", "buyhold", "--universe", "pair", *common, "--json"])
    key = _json(capsys)["run_key"]
    assert main(["show", key[:6], *common, "--json"]) == EXIT_OK
    d = _json(capsys)
    assert d["meta"]["universe"] == "pair"
    assert _symbols(d) == {"AAA", "BBB"}


def test_human_output_names_the_symbols(project, capsys):
    common, _ = project
    main(["run", "buyhold", "--universe", "pair", *common])
    out = capsys.readouterr().out
    assert "universe    2 symbols, 2 traded" in out
    assert "AAA" in out and "BBB" in out


def test_analyze_a_universe_run_reports_unmeasured_axes_as_such(project, capsys):
    common, runs = project
    main(["run", "buyhold", "--universe", "pair", *common, "--json"])
    key = _json(capsys)["run_key"]
    assert main(["analyze", key, *common, "--axes", "trades,costs,oos", "--json"]) == EXIT_OK
    d = _json(capsys)
    by = {a["axis"]: a for a in d["axes"]}
    assert [a["axis"] for a in d["axes"]] == ["trades", "costs", "oos"]
    assert by["trades"]["ran"] is True
    for axis in ("costs", "oos"):
        assert by[axis]["ran"] is False
        assert "universe" in by[axis]["error"]
    assert json.loads((runs / key / "analysis.json").read_text()) == d


def test_analyze_a_universe_strategy_by_name(project, capsys):
    common, _ = project
    assert main(["analyze", "buyhold_own", *common, "--axes", "trades,validation",
                 "--json"]) == EXIT_OK
    by = {a["axis"]: a for a in _json(capsys)["axes"]}
    assert by["trades"]["ran"] is True


def test_json_mode_keeps_progress_off_stdout(project, capsys):
    common, _ = project
    main(["run", "buyhold", "--universe", "pair", *common, "--json"])
    cap = capsys.readouterr()
    json.loads(cap.out)
    assert "running buyhold" in cap.err


def test_discover_keeps_universe_only_files_out_of_the_single_symbol_list(project):
    from hermes import discover, discover_all

    _, _ = project
    assert [e.name for e in discover()] == ["buyhold"]
    working, _ = discover_all()
    assert {e.name for e in working} == {"buyhold", "buyhold_own"}
