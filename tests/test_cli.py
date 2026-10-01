"""The ``hermes`` CLI — the surface an agent drives (ADR-0010).

The contract under test: ``--json`` puts exactly one JSON document on stdout while
progress and warnings go to stderr, so a caller can always pipe it; and the exit code
distinguishes "worked" from "worked but produced nothing" from "bad request".
"""

from __future__ import annotations

import json

import pytest

from hermes.cli import EXIT_EMPTY, EXIT_ERR, EXIT_OK, main

# A self-contained strategy: InMemorySource, so the CLI never touches the network.
STRATEGY = '''
from datetime import datetime, timedelta, timezone
from hermes import Backtest, Parameter, Strategy, Symbol, Timeframe, Units
from hermes.core import Bar, CryptoPair
from hermes.data import InMemorySource

GENERATED_BY = "hermes-strategy"
H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 1, tzinfo=timezone.utc)
SYM = Symbol("BTCUSDT", "binance")


class BuyAndHold(Strategy):
    def setup(self):
        self.hold = self.param(Parameter("hold", 2, bounds=(1, 5)))

    def on_bar(self, bar):
        if self.venue.position().is_flat:
            self.buy(Units(1), take_profit=bar.close * 1.01)


def build_backtest(**overrides):
    inst = CryptoPair(SYM, base_asset="BTC", quote_currency="USDT", tick_size=0.01)
    bars = [Bar(T0 + timedelta(hours=i), H1, 100 + i, 102 + i, 99 + i, 101 + i, 5.0)
            for i in range(60)]
    kw = dict(strategy=BuyAndHold(), source=InMemorySource(inst, {H1: bars}),
              symbol=SYM, timeframes=[H1], start=bars[0].timestamp,
              end=bars[-1].timestamp, starting_cash=10_000)
    kw.update(overrides)
    return Backtest(**kw)
'''

NO_TRADES = STRATEGY.replace("if self.venue.position().is_flat:", "if False:")


@pytest.fixture
def project(tmp_path):
    """A strategies dir + an isolated ledger, as CLI argv fragments."""
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "buyhold.py").write_text(STRATEGY)
    runs = tmp_path / "runs"
    return ["--dir", str(sdir), "--runs-dir", str(runs)], sdir, runs


def _json(capsys):
    out = capsys.readouterr().out
    return json.loads(out)


# --- strategies -------------------------------------------------------------

def test_strategies_lists_what_is_runnable(project, capsys):
    common, sdir, _ = project
    assert main(["strategies", *common, "--json"]) == EXIT_OK
    d = _json(capsys)
    assert [s["name"] for s in d["strategies"]] == ["buyhold"]
    assert d["strategies"][0]["ai_generated"] is True
    assert d["broken"] == []


def test_strategies_reports_a_broken_file_without_hiding_the_good_one(project, capsys):
    common, sdir, _ = project
    (sdir / "oops.py").write_text("def build_backtest(:\n")
    assert main(["strategies", *common, "--json"]) == EXIT_OK
    d = _json(capsys)
    assert [s["name"] for s in d["strategies"]] == ["buyhold"]
    assert d["broken"][0]["name"] == "oops"
    assert "SyntaxError" in d["broken"][0]["error"]


def test_strategies_on_an_empty_dir_is_exit_empty(tmp_path, capsys):
    (tmp_path / "strategies").mkdir()
    assert main(["strategies", "--dir", str(tmp_path / "strategies"), "--json"]) == EXIT_EMPTY


# --- run --------------------------------------------------------------------

def test_run_produces_metrics_and_records_the_run(project, capsys):
    common, _, runs = project
    assert main(["run", "buyhold", *common, "--json"]) == EXIT_OK
    d = _json(capsys)
    assert d["strategy"] == "buyhold"
    assert d["from_cache"] is False
    assert d["metrics"]["num_trades"] > 0
    assert (runs / d["run_key"] / "result.json").exists()
    assert (runs / d["run_key"] / "meta.json").exists()


def test_a_second_identical_run_is_served_from_the_ledger(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common, "--json"])
    first = _json(capsys)
    main(["run", "buyhold", *common, "--json"])
    second = _json(capsys)
    assert second["from_cache"] is True
    assert second["run_key"] == first["run_key"]
    assert second["metrics"] == first["metrics"]


def test_no_cache_forces_a_rerun(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common, "--json"])
    capsys.readouterr()
    main(["run", "buyhold", *common, "--no-cache", "--json"])
    assert _json(capsys)["from_cache"] is False


def test_changing_a_parameter_is_a_different_run(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common, "--json"])
    a = _json(capsys)
    main(["run", "buyhold", *common, "-p", "hold=4", "--json"])
    b = _json(capsys)
    assert a["run_key"] != b["run_key"]


def test_no_save_leaves_the_ledger_empty(project, capsys):
    common, _, runs = project
    main(["run", "buyhold", *common, "--no-save", "--json"])
    assert not runs.exists() or list(runs.iterdir()) == []


def test_a_zero_trade_run_is_exit_empty_and_says_why(project, capsys):
    """Zero trades is the commonest failure; the agent needs the cause, not a
    list of three things it might be."""
    common, sdir, _ = project
    (sdir / "flat.py").write_text(NO_TRADES)
    assert main(["run", "flat", *common, "--json"]) == EXIT_EMPTY
    d = _json(capsys)
    assert d["metrics"]["num_trades"] == 0
    # bars were stepped, so the diagnosis must point at the logic, not the data
    assert d["coverage"]["steps"] > 0
    assert "never became true" in d["zero_trade_diagnosis"]


def test_a_healthy_run_reports_coverage_too(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common, "--json"])
    d = _json(capsys)
    assert d["coverage"]["steps"] > 0
    assert d["coverage"]["trades"] == d["metrics"]["num_trades"]
    assert "zero_trade_diagnosis" not in d


def test_an_unknown_strategy_is_a_usage_error(project, capsys):
    common, _, _ = project
    assert main(["run", "nope", *common]) == EXIT_ERR
    assert "no strategy 'nope'" in capsys.readouterr().err


def test_a_broken_strategy_names_itself_in_the_error(project, capsys):
    common, sdir, _ = project
    (sdir / "oops.py").write_text("import a_module_that_is_not_installed\n")
    assert main(["run", "oops", *common]) == EXIT_ERR
    assert "failed to import" in capsys.readouterr().err


def test_a_bad_date_is_rejected_before_any_work(project, capsys):
    common, _, _ = project
    assert main(["run", "buyhold", *common, "--start", "last-tuesday"]) == EXIT_ERR
    assert "bad date" in capsys.readouterr().err


def test_a_malformed_param_is_rejected(project, capsys):
    common, _, _ = project
    assert main(["run", "buyhold", *common, "-p", "hold"]) == EXIT_ERR
    assert "expected name=value" in capsys.readouterr().err


# --- runs / show ------------------------------------------------------------

def test_runs_lists_the_ledger(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common, "--json"])
    key = _json(capsys)["run_key"]
    assert main(["runs", *common, "--json"]) == EXIT_OK
    assert [r["key"] for r in _json(capsys)["runs"]] == [key]


def test_runs_on_an_empty_ledger_is_exit_empty(project, capsys):
    common, _, _ = project
    assert main(["runs", *common, "--json"]) == EXIT_EMPTY


def test_show_accepts_a_key_prefix(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common, "--json"])
    key = _json(capsys)["run_key"]
    assert main(["show", key[:4], *common, "--json"]) == EXIT_OK
    assert _json(capsys)["run_key"] == key


def test_show_trades_includes_the_blotter(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common, "--json"])
    key = _json(capsys)["run_key"]
    main(["show", key, *common, "--trades", "--json"])
    assert len(_json(capsys)["trades"]) > 0


def test_show_on_an_unknown_key_is_a_usage_error(project, capsys):
    common, _, _ = project
    assert main(["show", "zzzz", *common]) == EXIT_ERR
    assert "no run matching" in capsys.readouterr().err


# --- analyze ----------------------------------------------------------------

def test_analyze_a_stored_run_writes_analysis_json(project, capsys):
    common, _, runs = project
    main(["run", "buyhold", *common, "--json"])
    key = _json(capsys)["run_key"]

    assert main(["analyze", key, *common, "--axes", "trades,costs,oos", "--json"]) == EXIT_OK
    d = _json(capsys)
    assert d["run_key"] == key
    assert [a["axis"] for a in d["axes"]] == ["trades", "costs", "oos"]
    assert all(a["ran"] for a in d["axes"])

    saved = json.loads((runs / key / "analysis.json").read_text())
    assert saved == d, "analysis.json must match what was printed"


def test_analyze_accepts_a_strategy_name_and_runs_it_fresh(project, capsys):
    common, _, _ = project
    assert main(["analyze", "buyhold", *common, "--axes", "trades", "--json"]) == EXIT_OK
    assert _json(capsys)["axes"][0]["axis"] == "trades"


def test_analyze_rejects_an_unknown_axis(project, capsys):
    common, _, _ = project
    assert main(["analyze", "buyhold", *common, "--axes", "vibes"]) == EXIT_ERR
    assert "unknown axis" in capsys.readouterr().err


def test_analyse_is_accepted_as_an_alias(project, capsys):
    common, _, _ = project
    assert main(["analyse", "buyhold", *common, "--axes", "trades", "--json"]) == EXIT_OK


def test_analyze_no_save_does_not_write(project, capsys):
    common, _, runs = project
    main(["run", "buyhold", *common, "--json"])
    key = _json(capsys)["run_key"]
    main(["analyze", key, *common, "--axes", "trades", "--no-save", "--json"])
    assert not (runs / key / "analysis.json").exists()


# --- the JSON contract itself ----------------------------------------------

@pytest.mark.parametrize("argv", [
    ["strategies"],
    ["run", "buyhold"],
    ["runs"],
])
def test_json_mode_puts_nothing_but_json_on_stdout(project, capsys, argv):
    """An agent pipes stdout. Progress must not land there."""
    common, _, _ = project
    if argv[0] != "strategies":
        main(["run", "buyhold", *common, "--json"])
        capsys.readouterr()
    main([*argv, *common, "--json"])
    cap = capsys.readouterr()
    json.loads(cap.out)                      # must parse whole
    assert cap.out.count("\n") >= 1


def test_human_mode_puts_no_json_on_stdout(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common])
    out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert "trades" in out and "sharpe" in out


def test_version_is_reported():
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0


# --- ideas (framework step 1) ----------------------------------------------

@pytest.fixture
def ideas(tmp_path):
    return ["--ideas-file", str(tmp_path / "ideas.json")]


def test_ideas_add_then_list(ideas, capsys):
    assert main(["ideas", "add", "ORB on NQ", "--source", "book",
                 "--detail", "Carver", "--hypothesis", "first 30m sets direction",
                 *ideas, "--json"]) == EXIT_OK
    added = _json(capsys)["idea"]
    assert added["status"] == "raw" and added["source"] == "book"

    assert main(["ideas", *ideas, "--json"]) == EXIT_OK
    d = _json(capsys)
    assert [i["title"] for i in d["ideas"]] == ["ORB on NQ"]
    assert d["pipeline"]["raw"] == 1


def test_ideas_list_on_an_empty_backlog_is_exit_empty(ideas, capsys):
    assert main(["ideas", *ideas, "--json"]) == EXIT_EMPTY


def test_ideas_rejects_an_unknown_source(ideas, capsys):
    assert main(["ideas", "add", "x", "--source", "a_dream", *ideas]) == EXIT_ERR
    assert "unknown --source" in capsys.readouterr().err


def test_ideas_set_walks_the_pipeline_and_links_runs(ideas, capsys):
    main(["ideas", "add", "ema cross", "--source", "trader", *ideas, "--json"])
    idea_id = _json(capsys)["idea"]["id"]

    main(["ideas", "set", idea_id[:4], "--status", "validated",
          "--strategy", "ema_crossover", "--run-key", "abc123",
          "--verdict", "survives costs", *ideas, "--json"])
    got = _json(capsys)["idea"]
    assert got["status"] == "validated"
    assert got["run_keys"] == ["abc123"]
    assert got["verdict"] == "survives costs"


def test_ideas_set_rejects_an_unknown_status(ideas, capsys):
    main(["ideas", "add", "x", *ideas, "--json"])
    idea_id = _json(capsys)["idea"]["id"]
    assert main(["ideas", "set", idea_id, "--status", "probably_fine", *ideas]) == EXIT_ERR
    assert "unknown status" in capsys.readouterr().err


def test_ideas_set_with_nothing_to_change_is_an_error(ideas, capsys):
    main(["ideas", "add", "x", *ideas, "--json"])
    idea_id = _json(capsys)["idea"]["id"]
    assert main(["ideas", "set", idea_id, *ideas]) == EXIT_ERR
    assert "nothing to change" in capsys.readouterr().err


def test_ideas_open_filter_hides_decided_work(ideas, capsys):
    main(["ideas", "add", "open one", *ideas, "--json"])
    _json(capsys)
    main(["ideas", "add", "closed one", *ideas, "--json"])
    closed = _json(capsys)["idea"]["id"]
    main(["ideas", "set", closed, "--status", "rejected", "--verdict", "no", *ideas, "--json"])
    _json(capsys)

    main(["ideas", *ideas, "--open", "--json"])
    assert [i["title"] for i in _json(capsys)["ideas"]] == ["open one"]


def test_ideas_hit_rate_scores_provenance(ideas, capsys):
    main(["ideas", "add", "a", "--source", "trader", *ideas, "--json"])
    good = _json(capsys)["idea"]["id"]
    main(["ideas", "set", good, "--status", "validated", *ideas, "--json"])
    _json(capsys)
    main(["ideas", "add", "b", "--source", "book", *ideas, "--json"])
    _json(capsys)

    main(["ideas", *ideas, "--hit-rate", "--json"])
    hr = _json(capsys)["hit_rate"]
    assert hr["trader"]["rate"] == 1.0
    assert hr["book"]["rate"] is None      # undecided


def test_ideas_rm(ideas, capsys):
    main(["ideas", "add", "x", *ideas, "--json"])
    idea_id = _json(capsys)["idea"]["id"]
    assert main(["ideas", "rm", idea_id, *ideas, "--json"]) == EXIT_OK
    _json(capsys)
    main(["ideas", *ideas, "--json"])
    assert _json(capsys)["ideas"] == []


# --- correlate (framework step 4) ------------------------------------------

def test_correlate_needs_at_least_two_runs(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common, "--json"])
    key = _json(capsys)["run_key"]
    assert main(["correlate", key, *common]) == EXIT_ERR
    assert "at least 2 runs" in capsys.readouterr().err


def test_correlate_two_runs_reports_a_matrix(project, capsys):
    common, sdir, _ = project
    (sdir / "buyhold2.py").write_text(STRATEGY.replace("102 + i", "103 + i"))
    main(["run", "buyhold", *common, "--json"])
    a = _json(capsys)["run_key"]
    main(["run", "buyhold2", *common, "--json"])
    b = _json(capsys)["run_key"]

    assert main(["correlate", a, b, *common, "--json"]) == EXIT_OK
    d = _json(capsys)
    assert len(d["labels"]) == 2
    assert d["matrix"][0][0] == 1.0
    assert set(d["runs"].values()) == {a, b}


def test_correlate_defaults_to_the_newest_run_per_strategy(project, capsys):
    common, sdir, _ = project
    (sdir / "buyhold2.py").write_text(STRATEGY.replace("102 + i", "103 + i"))
    main(["run", "buyhold", *common, "--json"])
    main(["run", "buyhold2", *common, "--json"])
    capsys.readouterr()

    assert main(["correlate", *common, "--json"]) == EXIT_OK
    assert len(_json(capsys)["labels"]) == 2
