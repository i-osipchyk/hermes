"""Phase 2 step 2 (ADR-0013): a run is attributed to an idea as a trial or a diagnostic,
trials are grouped into clusters, and effective trials are counted from the ledger.
Seams: CLI ``main([...])``, the ledger, ``TrialRegistry``."""

import json
import random
from datetime import UTC, datetime, timedelta

from hermes.cli import EXIT_OK, main
from hermes.research.ideas import IdeaBook
from hermes.research.ledger import RunLedger, RunMeta
from hermes.research.trials import TrialRegistry
from tests.test_cli import STRATEGY


def _project(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "buyhold.py").write_text(STRATEGY)
    return ["--dir", str(sdir), "--runs-dir", str(tmp_path / "runs")], RunLedger(tmp_path / "runs")


def _only_meta(ledger):
    (entry,) = ledger.entries()
    return entry.meta


def test_a_run_records_the_idea_it_is_a_trial_of(tmp_path, capsys):
    common, ledger = _project(tmp_path)
    assert main(["run", "buyhold", *common, "--idea", "abc123", "--json", "--note", "t"]) == EXIT_OK
    meta = _only_meta(ledger)
    assert (meta.idea, meta.kind) == ("abc123", "trial")


def test_a_diagnostic_run_is_marked_as_one(tmp_path, capsys):
    common, ledger = _project(tmp_path)
    main(["run", "buyhold", *common, "--idea", "abc123", "--diagnostic", "--json", "--note", "t"])
    assert _only_meta(ledger).kind == "diagnostic"


def test_a_run_with_no_idea_belongs_to_no_idea(tmp_path, capsys):
    common, ledger = _project(tmp_path)
    main(["run", "buyhold", *common, "--json", "--note", "t"])
    assert _only_meta(ledger).idea is None


def test_rerunning_a_diagnostic_as_a_trial_counts_it(tmp_path, capsys):
    """Same configuration, same cache key, one ledger row — so a later 'trial' claim must
    not be swallowed by the earlier diagnostic, or N_eff would be undercounted."""
    common, ledger = _project(tmp_path)
    main(["run", "buyhold", *common, "--idea", "a", "--diagnostic", "--json", "--note", "t"])
    main(["run", "buyhold", *common, "--idea", "a", "--json", "--note", "t"])
    assert _only_meta(ledger).kind == "trial"


def test_a_later_diagnostic_rerun_does_not_demote_a_trial(tmp_path, capsys):
    common, ledger = _project(tmp_path)
    main(["run", "buyhold", *common, "--idea", "a", "--json", "--note", "t"])
    main(["run", "buyhold", *common, "--idea", "a", "--diagnostic", "--json", "--note", "t"])
    assert _only_meta(ledger).kind == "trial"


# --- clustering ---------------------------------------------------------------



_T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _returns(seed, n=120):
    rng = random.Random(seed)
    return [rng.gauss(0.0004, 0.01) for _ in range(n)]


def _trades(days, side="buy"):
    return [{"side": side, "entry_time": (_T0 + timedelta(days=d, hours=9)).isoformat()}
            for d in days]


def _save(ledger, key, rets, trades, *, idea="x", kind="trial", minute=0):
    equity, eq = [], 10_000.0
    for i, r in enumerate(rets):
        eq *= 1 + r
        # two intraday points per day: the registry must resample to the day's last value
        equity.append(((_T0 + timedelta(days=i, hours=1)).isoformat(), eq * 0.999))
        equity.append(((_T0 + timedelta(days=i, hours=20)).isoformat(), eq))
    meta = RunMeta(
        strategy="s", strategy_hash="h", source="memory", ticker="T", universe=None,
        start=_T0.isoformat(), end=(_T0 + timedelta(days=len(rets))).isoformat(), params={},
        sizer="", unconstrained=False,
        created_at=(_T0 + timedelta(minutes=minute)).isoformat(), idea=idea, kind=kind,
    )
    ledger.save(key, {"equity_curve": equity, "trades": trades, "metrics": {}}, meta)


def test_a_nudged_variant_joins_the_cluster_but_a_different_one_starts_another(tmp_path):
    ledger = RunLedger(tmp_path / "runs")
    base = _returns(1)
    nudged = [r + 0.0001 * ((i % 3) - 1) for i, r in enumerate(base)]   # corr ~ 0.995
    other = _returns(2)                                                  # independent
    _save(ledger, "base", base, _trades(range(0, 100, 7)), minute=1)
    _save(ledger, "nudged", nudged, _trades(range(1, 100, 9), "sell"), minute=2)
    _save(ledger, "other", other, _trades(range(3, 100, 11), "sell"), minute=3)

    clusters = TrialRegistry(ledger).clusters("x")

    assert [c.keys for c in clusters] == [["base", "nudged"], ["other"]]


def test_trades_that_overlap_heavily_cluster_even_when_returns_differ(tmp_path):
    ledger = RunLedger(tmp_path / "runs")
    entries = list(range(0, 100, 5))
    _save(ledger, "a", _returns(3), _trades(entries), minute=1)
    # different return stream (other sizing/exits) but the same entry days and side
    _save(ledger, "b", _returns(4), _trades(entries[:-1] + [97]), minute=2)

    assert [c.keys for c in TrialRegistry(ledger).clusters("x")] == [["a", "b"]]


def test_diagnostics_and_other_ideas_are_not_trials(tmp_path):
    ledger = RunLedger(tmp_path / "runs")
    _save(ledger, "t1", _returns(5), _trades([1, 2]), minute=1)
    _save(ledger, "diag", _returns(6), _trades([4, 5]), kind="diagnostic", minute=2)
    _save(ledger, "elsewhere", _returns(7), _trades([8, 9]), idea="y", minute=3)

    assert [c.keys for c in TrialRegistry(ledger).clusters("x")] == [["t1"]]


# --- effective trials ---------------------------------------------------------



def _idea_with_trials(ledger, book, title, n, seed0):
    idea = book.add(title)
    for i in range(n):          # n independent (so n distinct) trials
        _save(ledger, f"{idea.id}-{i}", _returns(seed0 + i), _trades([seed0 + i * 13], "sell"),
              idea=idea.id, minute=seed0 + i)
    return idea


def test_effective_trials_add_the_clusters_of_ideas_that_informed_it(tmp_path):
    ledger, book = RunLedger(tmp_path / "runs"), IdeaBook(tmp_path / "ideas.json")
    mine = _idea_with_trials(ledger, book, "mine", 2, 100)
    used = _idea_with_trials(ledger, book, "used", 1, 200)
    _idea_with_trials(ledger, book, "unrelated", 3, 300)   # never looked at: must not count
    book.update(mine.id, informed_by=[used.id])

    eff = TrialRegistry(ledger, book).effective_trials(mine.id)

    assert eff.n_eff == 3                                  # 2 own + 1 inherited
    assert eff.own == 2 and eff.inherited == {used.id: 1}


def test_an_idea_nothing_informed_counts_only_its_own_clusters(tmp_path):
    ledger, book = RunLedger(tmp_path / "runs"), IdeaBook(tmp_path / "ideas.json")
    mine = _idea_with_trials(ledger, book, "mine", 2, 100)
    _idea_with_trials(ledger, book, "other", 4, 300)
    eff = TrialRegistry(ledger, book).effective_trials(mine.id)
    assert eff.n_eff == 2 and eff.inherited == {}


# --- `hermes trials` ----------------------------------------------------------

def test_hermes_trials_reports_clusters_and_effective_trials(tmp_path, capsys):
    common, ledger = _project(tmp_path)
    ideas = ["--ideas-file", str(tmp_path / "ideas.json")]
    main(["ideas", "add", "mine", *ideas, "--json"])
    mine = json.loads(capsys.readouterr().out)["idea"]["id"]
    main(["ideas", "add", "used", *ideas, "--json"])
    used = json.loads(capsys.readouterr().out)["idea"]["id"]
    main(["ideas", "set", mine, *ideas, "--informed-by", used, "--json"])
    capsys.readouterr()

    # two variants of one bet (the param is inert here) and one run for the idea that informed it
    for hold in ("2", "3"):
        main(["run", "buyhold", *common, "--idea", mine, "-p", f"hold={hold}", "--json",
              "--note", "t"])
    main(["run", "buyhold", *common, "--idea", used, "--cash", "5000", "--json", "--note", "t"])
    capsys.readouterr()

    out = tmp_path / "ideas_out"
    code = main(["trials", mine, *common, *ideas, "--out-dir", str(out), "--json"])

    assert code == EXIT_OK
    d = json.loads(capsys.readouterr().out)
    assert d["own"] == 1 and len(d["clusters"]) == 1 and len(d["clusters"][0]["keys"]) == 2
    assert d["inherited"] == {used: 1} and d["n_eff"] == 2
    assert json.loads((out / mine / "trials.json").read_text()) == d
