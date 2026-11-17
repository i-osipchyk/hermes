"""F10 (docs/audit-2026-11.md): ``inf`` must not reach disk or stdout as the non-standard
JSON token ``Infinity``. It is written as the string "inf" and read back as ``inf``.
Seams: ``RunLedger.save``/``load`` and ``hermes run --json`` / ``analyze``."""

from __future__ import annotations

import json
import math

import pytest

from hermes.cli import main
from hermes.research.ledger import RunLedger, RunMeta, restore_result


def _strict(text: str):
    """Parse as a strict JSON consumer (jq, JS) would: NaN/Infinity are errors."""
    def boom(token):
        raise AssertionError(f"non-standard JSON token {token!r}")
    return json.loads(text, parse_constant=boom)


def _meta():
    return RunMeta(strategy="s", strategy_hash="h", source="src", ticker="T", universe=None,
                   start="2024-01-01T00:00:00+00:00", end="2024-02-01T00:00:00+00:00",
                   params={}, sizer="None", unconstrained=False,
                   created_at="2024-02-02T00:00:00+00:00")


def _result(pf):
    return {"metrics": {"profit_factor": pf, "num_trades": 3}, "equity_curve": [], "trades": []}


def test_the_ledger_writes_strict_json_and_reads_inf_back(tmp_path):
    led = RunLedger(tmp_path)
    led.save("k", _result(math.inf), _meta())
    text = (tmp_path / "k" / "result.json").read_text()
    assert "Infinity" not in text
    assert _strict(text)["metrics"]["profit_factor"] == "inf"
    loaded = led.load("k")
    assert loaded.metrics["profit_factor"] == math.inf
    assert restore_result(loaded.result).metrics.profit_factor == math.inf


def test_negative_infinity_and_nan_survive_too(tmp_path):
    led = RunLedger(tmp_path)
    led.save("k", {"metrics": {"profit_factor": -math.inf, "sharpe": math.nan}}, _meta())
    m = led.load("k").metrics
    assert m["profit_factor"] == -math.inf
    assert math.isnan(m["sharpe"])


def test_an_old_row_written_with_the_bare_infinity_token_still_loads(tmp_path):
    led = RunLedger(tmp_path)
    led.save("k", _result(1.0), _meta())
    (tmp_path / "k" / "result.json").write_text(
        '{"metrics": {"profit_factor": Infinity, "num_trades": 3}}')
    assert led.load("k").metrics["profit_factor"] == math.inf


def test_ordinary_strings_that_merely_look_like_words_are_untouched(tmp_path):
    led = RunLedger(tmp_path)
    led.save("k", {"metrics": {"profit_factor": 1.0}, "note": "info", "trades": [{"s": "INF"}]},
             _meta())
    got = led.load("k").result
    assert got["note"] == "info" and got["trades"] == [{"s": "INF"}]


STRATEGY = '''
from datetime import datetime, timedelta, timezone
from hermes import Backtest, Strategy, Symbol, Timeframe, Units
from hermes.core import Bar, CryptoPair
from hermes.data import InMemorySource

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 1, tzinfo=timezone.utc)
SYM = Symbol("BTCUSDT", "binance")


class Winner(Strategy):
    def setup(self):
        pass

    def on_bar(self, bar):
        if self.venue.position().is_flat:
            self.buy(Units(1), take_profit=bar.close * 1.001)


def build_backtest(**overrides):
    inst = CryptoPair(SYM, base_asset="BTC", quote_currency="USDT", tick_size=0.01)
    bars = [Bar(T0 + timedelta(hours=i), H1, 100 + i, 110 + i, 99 + i, 105 + i, 5.0)
            for i in range(60)]
    kw = dict(strategy=Winner(), source=InMemorySource(inst, {H1: bars}), symbol=SYM,
              timeframes=[H1], start=bars[0].timestamp, end=bars[-1].timestamp,
              starting_cash=10_000)
    kw.update(overrides)
    return Backtest(**kw)
'''


@pytest.fixture
def project(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "winner.py").write_text(STRATEGY)
    return ["--dir", str(sdir), "--runs-dir", str(tmp_path / "runs")], tmp_path / "runs"


def test_run_json_on_a_strategy_with_no_losers_is_strictly_parseable(project, capsys):
    common, runs = project
    main(["run", "winner", *common, "--json"])
    d = _strict(capsys.readouterr().out)
    assert d["metrics"]["profit_factor"] == "inf"        # the premise: it really is infinite
    key = d["run_key"]
    _strict((runs / key / "result.json").read_text())


def test_a_cached_rerun_and_show_agree_and_stay_strict(project, capsys):
    common, _ = project
    main(["run", "winner", *common, "--json"])
    first = _strict(capsys.readouterr().out)
    main(["run", "winner", *common, "--json"])
    second = _strict(capsys.readouterr().out)
    assert second["from_cache"] is True
    assert second["metrics"]["profit_factor"] == first["metrics"]["profit_factor"] == "inf"
    main(["show", first["run_key"], *common, "--json"])
    assert _strict(capsys.readouterr().out)["metrics"]["profit_factor"] == "inf"


def test_analyze_output_and_analysis_json_are_strict(project, capsys):
    common, runs = project
    main(["run", "winner", *common, "--json"])
    key = _strict(capsys.readouterr().out)["run_key"]
    main(["analyze", key, *common, "--axes", "trades,validation", "--json"])
    _strict(capsys.readouterr().out)
    _strict((runs / key / "analysis.json").read_text())
