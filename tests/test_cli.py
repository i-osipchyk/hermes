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


def test_run_finds_a_strategy_grouped_in_a_family_folder(project, capsys):
    common, sdir, _ = project
    (sdir / "holds").mkdir()
    (sdir / "holds" / "nested_hold.py").write_text(STRATEGY)
    assert main(["run", "nested_hold", *common, "--json"]) == EXIT_OK
    d = _json(capsys)
    assert d["strategy"] == "nested_hold"
    assert d["metrics"]["num_trades"] > 0


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


# --- F3 / F2: the key identifies the run, not the moment it was asked for ----

OPEN_ENDED = STRATEGY.replace("end=bars[-1].timestamp,", "end=datetime.now(timezone.utc),")


def test_a_strategy_whose_default_end_is_now_still_hits_the_cache(project, capsys):
    common, sdir, _ = project
    (sdir / "buyhold.py").write_text(OPEN_ENDED)
    main(["run", "buyhold", *common, "--json"])
    first = _json(capsys)
    main(["run", "buyhold", *common, "--json"])
    second = _json(capsys)
    assert first["run_key"] == second["run_key"]
    assert second["from_cache"] is True


def test_an_explicit_end_is_left_alone(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common, "--end", "2023-01-02", "--json"])
    a = _json(capsys)
    main(["run", "buyhold", *common, "--end", "2023-01-03", "--json"])
    assert _json(capsys)["run_key"] != a["run_key"]


def test_a_fixed_default_end_is_not_truncated(project, capsys):
    """Only an open-ended ('now') default is floored to a day; a strategy that pins an
    intraday end keeps it."""
    common, _, _ = project
    main(["run", "buyhold", *common, "--json"])
    assert _json(capsys)["end"] == "2023-01-03T11:00:00+00:00"


def test_starting_cash_is_part_of_the_run_identity(project, capsys):
    common, _, _ = project
    main(["run", "buyhold", *common, "--cash", "10000", "--json"])
    a = _json(capsys)["run_key"]
    main(["run", "buyhold", *common, "--cash", "50000", "--json"])
    assert _json(capsys)["run_key"] != a


# --- F16: the key follows the data the run actually read ---------------------

EPOCH_SRC = STRATEGY.replace(
    "from hermes.data import InMemorySource",
    "from pathlib import Path\nfrom hermes.data import InMemorySource\n"
    "EPOCH = Path(__file__).with_name('epoch.txt')\n"
    "class EpochSource(InMemorySource):\n"
    "    def data_epoch(self, ticker):\n"
    "        return EPOCH.read_text()\n"
    "    def history(self, *a, **k):\n"
    "        # a first fetch that learns a corporate action moves the epoch\n"
    "        if EPOCH.read_text() == 'e0':\n"
    "            EPOCH.write_text('e1')\n"
    "        return super().history(*a, **k)\n",
).replace("source=InMemorySource(", "source=EpochSource(")


def test_run_key_follows_the_data_epoch_including_one_learned_during_the_run(project, capsys):
    common, sdir, _ = project
    (sdir / "buyhold.py").write_text(EPOCH_SRC)
    (sdir / "epoch.txt").write_text("e0")
    main(["run", "buyhold", *common, "--json"])
    first = _json(capsys)
    assert (sdir / "epoch.txt").read_text() == "e1"          # the run moved it
    main(["run", "buyhold", *common, "--json"])
    second = _json(capsys)
    assert second["from_cache"] is True                       # keyed on the post-run epoch
    assert second["run_key"] == first["run_key"]
    (sdir / "epoch.txt").write_text("e2")                    # a later corporate action
    main(["run", "buyhold", *common, "--json"])
    third = _json(capsys)
    assert third["from_cache"] is False and third["run_key"] != first["run_key"]


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


def _earned_dir(ideas):
    from pathlib import Path

    return Path(ideas[1]).parent / "holdouts_dir"


def _earned(ideas, idea_id):
    """Record a passed holdout for the idea (what `hermes holdout unlock` writes), and return
    the argv that points `ideas set` at it — `validated` is earned, not asserted (ADR-0012)."""
    from pathlib import Path

    holdouts = Path(ideas[1]).parent / "holdouts_dir"
    (holdouts / idea_id).mkdir(parents=True, exist_ok=True)
    (holdouts / idea_id / "holdout.json").write_text('{"state": "done", "passed": true}')
    return ["--holdout-dir", str(holdouts)]


def _walk(ideas, idea_id, capsys, *statuses):
    """Move an idea along legal edges, the way a session would."""
    from tests.artifact_helpers import ready_for_testing

    holdouts = _earned_dir(ideas)
    ready_for_testing(holdouts, idea_id)
    for status in statuses:
        assert main(["ideas", "set", idea_id, "--status", status, *ideas,
                     "--holdout-dir", str(holdouts), "--json"]) == EXIT_OK
        capsys.readouterr()


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
    _walk(ideas, idea_id, capsys, "quantified", "coded", "testing")

    main(["ideas", "set", idea_id[:4], "--status", "validated",
          "--strategy", "ema_crossover", "--run-key", "abc123",
          "--verdict", "survives costs", *ideas, *_earned(ideas, idea_id), "--json"])
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
    main(["ideas", "set", closed, "--status", "rejected", "--verdict", "no",
          "--failure-kind", "no_edge", "--failure-assumption", "it drifts", *ideas, "--json"])
    _json(capsys)

    main(["ideas", *ideas, "--open", "--json"])
    assert [i["title"] for i in _json(capsys)["ideas"]] == ["open one"]


def test_ideas_hit_rate_scores_provenance(ideas, capsys):
    main(["ideas", "add", "a", "--source", "trader", *ideas, "--json"])
    good = _json(capsys)["idea"]["id"]
    _walk(ideas, good, capsys, "quantified", "coded", "testing")
    main(["ideas", "set", good, "--status", "validated", *ideas, *_earned(ideas, good), "--json"])
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
