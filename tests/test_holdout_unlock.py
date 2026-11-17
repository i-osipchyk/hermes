"""Phase 2 step 5 (ADR-0012/0013): a look at the holdout is earned, single and recorded.
Seams: ``analyze()`` (tier cap), the CLI ``hermes holdout check|unlock`` and
``hermes ideas set --status validated``."""

import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from hermes import CryptoPair, Symbol
from hermes.backtest.result import BacktestResult
from hermes.cli import EXIT_EMPTY, EXIT_ERR, EXIT_OK, main
from hermes.execution import Side
from hermes.execution.trade import Trade
from hermes.research.analysis import analyze
from hermes.research.unlock import holdout_verdict, required_psr
from tests.test_validation import _make_result  # noqa: E402

T0 = datetime(2018, 1, 1, tzinfo=UTC)


def _three_year_result(n_trades=120):
    """A result with enough trades and years to reach the `credible` tier on its own."""
    inst = CryptoPair(Symbol("X", "memory"), base_asset="X", quote_currency="USD", tick_size=0.01)
    rng = random.Random(3)
    trades, equity, curve = [], 10_000.0, [(T0, 10_000.0)]
    for i in range(n_trades):
        pnl = rng.gauss(15.0, 60.0)
        entry = T0 + timedelta(days=9 * i + 1)
        trades.append(Trade(instrument=inst, side=Side.BUY, size=1.0, entry_price=100.0,
                            entry_time=entry, exit_price=100.0 + pnl,
                            exit_time=entry + timedelta(days=2), exit_reason="take_profit",
                            gross_pnl=pnl, costs=0.0))
        equity += pnl
        curve.append((entry + timedelta(days=2), equity))
    return BacktestResult.compute(curve, trades, num_params=2)


def _tier(**kw):
    (axis,) = analyze(result=_three_year_result(), axes=("validation",), seed=0, **kw).axes
    return axis.data["sample_quality"]["tier"], axis.data["sample_quality"]["tier_reason"]


def test_the_result_alone_is_credible():
    assert _tier()[0] == "credible"


def test_no_carved_holdout_caps_the_tier_at_exploratory_and_says_why():
    tier, reason = _tier(holdout_carved=False)
    assert tier == "exploratory"
    assert "holdout" in reason.lower()


def test_a_carved_holdout_leaves_the_tier_alone():
    assert _tier(holdout_carved=True)[0] == "credible"


# --- the CLI ------------------------------------------------------------------


# One strategy file, three behaviours selected by NAME: a real conditional drift that keeps
# going, one that stops at the holdout (so it looks real in-sample and fails out of sample),
# and a random signal. about 4 years of daily bars; events are irregular so no shift re-aligns them.
_STRATEGY = '''
import random
from datetime import datetime, timedelta, timezone
from hermes import Backtest, Strategy, Symbol, Timeframe
from hermes.core import Bar, CryptoPair
from hermes.data import InMemorySource

D1 = Timeframe.parse("1d")
T0 = datetime(2016, 1, 1, tzinfo=timezone.utc)
SYM = Symbol("BTCUSDT", "binance")
N, HOLD = 1500, 3
KIND = "__KIND__"            # real | fades | random
FADE_AT = 1100               # the 'fades' effect stops here, before the holdout begins


def _events():
    rng, out, i = random.Random(11), [], 5
    while i < N - HOLD - 2:
        out.append(i)
        i += rng.randint(5, 10)
    return out


EVENTS = _events()


def _bars():
    rng, price, out = random.Random(21), 100.0, []
    for i in range(N):
        planted = any(1 <= i - e <= HOLD for e in EVENTS) and (KIND == "real" or
                                                                (KIND == "fades" and i < FADE_AT))
        o = price
        c = o * (1 + (0.015 if planted else 0.0) + rng.gauss(0, 0.01))
        out.append(Bar(T0 + timedelta(days=i), D1, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 1.0))
        price = c
    return out


class Trader(Strategy):
    def setup(self):
        self.i, self.age, self.rng = -1, {}, random.Random(5)

    def on_bar(self, bar):
        self.i += 1
        for t in self.venue.open_trades():
            self.age[id(t)] = self.age.get(id(t), 0) + 1
            if self.age[id(t)] >= HOLD:
                self.close(t)
        wants = (self.rng.random() < 0.08) if KIND == "random" else (self.i in EVENTS)
        if self.venue.position().is_flat and wants:
            self.buy(1000.0 / bar.close)       # fixed notional: the planted drift compounds


def build_backtest(**overrides):
    inst = CryptoPair(SYM, base_asset="BTC", quote_currency="USDT", tick_size=0.01)
    bars = _bars()
    kw = dict(strategy=Trader(), source=InMemorySource(inst, {D1: bars}), symbol=SYM,
              timeframes=[D1], start=bars[0].timestamp, end=bars[-1].timestamp,
              starting_cash=100_000)
    kw.update(overrides)
    return Backtest(**kw)
'''


@pytest.fixture
def lab(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    for kind in ("real", "fades", "random"):
        (sdir / f"{kind}.py").write_text(_STRATEGY.replace("__KIND__", kind))
    common = ["--dir", str(sdir), "--runs-dir", str(tmp_path / "runs")]
    ideas = ["--ideas-file", str(tmp_path / "ideas.json")]
    return {"common": common, "ideas": ideas, "sdir": sdir, "tmp": tmp_path}


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def _to_testing(lab, capsys, idea):
    """The unlock is only for an idea that is `testing`; walk the legal edges there."""
    from tests.artifact_helpers import ready_for_testing

    out = lab["tmp"] / "ideas_out"
    ready_for_testing(out, idea)
    for status in ("quantified", "coded", "testing"):
        assert main(["ideas", "set", idea, "--status", status, *lab["ideas"],
                     "--holdout-dir", str(out), "--json"]) == EXIT_OK
        capsys.readouterr()


def _idea_and_run(lab, capsys, kind):
    main(["ideas", "add", f"{kind} idea", "--source", "discretionary", *lab["ideas"], "--json"])
    idea = _out(capsys)["idea"]["id"]
    _to_testing(lab, capsys, idea)
    assert main(["run", kind, *lab["common"], "--idea", idea, "--json", "--note", "t"]) == EXIT_OK
    return idea, _out(capsys)["run_key"]


def _check(lab, capsys, idea, key):
    code = main(["holdout", "check", idea, "--run-key", key, *lab["common"], *lab["ideas"],
                 "--null-n", "30", "--json"])
    return code, _out(capsys)


def test_a_real_effect_clears_the_in_sample_gate(lab, capsys):
    idea, key = _idea_and_run(lab, capsys, "real")
    code, d = _check(lab, capsys, idea, key)
    assert code == EXIT_OK and d["passed"] is True, d
    assert {c: d["checks"][c]["ok"] for c in ("tier", "dsr", "null")} == {
        "tier": True, "dsr": True, "null": True}


def test_a_random_signal_does_not_clear_the_gate(lab, capsys):
    idea, key = _idea_and_run(lab, capsys, "random")
    code, d = _check(lab, capsys, idea, key)
    assert code == EXIT_EMPTY and d["passed"] is False
    assert not (d["checks"]["dsr"]["ok"] and d["checks"]["null"]["ok"]), d


# --- unlock: refusals (nothing is spent) --------------------------------------

def _unlock(lab, capsys, idea, key, *extra):
    code = main(["holdout", "unlock", idea, "--run-key", key, *lab["common"], *lab["ideas"],
                 "--null-n", "30", "--out-dir", str(lab["tmp"] / "ideas_out"), "--json", *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _nothing_spent(lab, idea):
    assert not (lab["tmp"] / "ideas_out" / idea / "holdout.json").exists()
    store = lab["tmp"] / "holdouts.json"
    entries = json.loads(store.read_text()) if store.exists() else {}
    assert all(e["looks"] == [] for e in entries.values())


def test_an_idea_that_fails_the_gate_is_refused_and_nothing_is_spent(lab, capsys):
    idea, key = _idea_and_run(lab, capsys, "random")
    code, _, err = _unlock(lab, capsys, idea, key)
    assert code == EXIT_ERR and "gate" in err.lower()
    _nothing_spent(lab, idea)


def test_a_strategy_changed_since_the_run_is_refused(lab, capsys):
    idea, key = _idea_and_run(lab, capsys, "real")
    path = lab["sdir"] / "real.py"
    path.write_text(path.read_text() + "\n# tweaked after the run\n")
    code, _, err = _unlock(lab, capsys, idea, key)
    assert code == EXIT_ERR and "changed" in err.lower()
    _nothing_spent(lab, idea)


def test_a_run_that_belongs_to_another_idea_is_refused(lab, capsys):
    _, key = _idea_and_run(lab, capsys, "real")
    main(["ideas", "add", "someone else", "--source", "discretionary", *lab["ideas"], "--json"])
    other = _out(capsys)["idea"]["id"]
    code, _, err = _unlock(lab, capsys, other, key)
    assert code == EXIT_ERR and "not a trial of idea" in err
    _nothing_spent(lab, other)


def test_a_history_too_short_to_carve_a_holdout_cannot_be_unlocked(lab, capsys):
    (lab["sdir"] / "short.py").write_text(
        _STRATEGY.replace("__KIND__", "real").replace("N, HOLD = 1500, 3", "N, HOLD = 425, 3"))
    idea, key = _idea_and_run(lab, capsys, "short")
    code, _, err = _unlock(lab, capsys, idea, key)
    assert code == EXIT_ERR and "no holdout" in err.lower()
    _nothing_spent(lab, idea)


# --- unlock: the single look ---------------------------------------------------

def _store(lab):
    return json.loads((lab["tmp"] / "holdouts.json").read_text())["memory/BTCUSDT/1d"]


def _record(lab, idea):
    return json.loads((lab["tmp"] / "ideas_out" / idea / "holdout.json").read_text())


def _status(lab, capsys, idea):
    main(["ideas", "list", *lab["ideas"], "--json"])
    return {i["id"]: i for i in _out(capsys)["ideas"]}[idea]


def test_a_real_effect_passes_the_holdout_and_the_idea_is_validated(lab, capsys):
    idea, key = _idea_and_run(lab, capsys, "real")
    code, out, _ = _unlock(lab, capsys, idea, key)

    assert code == EXIT_OK, out
    rec = _record(lab, idea)
    held = _store(lab)["holdout_start"]
    assert rec["passed"] is True and rec["window"]["start"] == held
    assert rec["holdout"]["n_trades"] >= 10 and rec["holdout"]["psr"] >= rec["required_psr"]
    assert rec["run_key"] == key and rec["strategy_hash"]
    assert [look["idea"] for look in _store(lab)["looks"]] == [idea]
    assert _status(lab, capsys, idea)["status"] == "validated"


def test_the_look_is_single_even_after_the_strategy_changes(lab, capsys):
    idea, key = _idea_and_run(lab, capsys, "real")
    assert _unlock(lab, capsys, idea, key)[0] == EXIT_OK
    path = lab["sdir"] / "real.py"
    path.write_text(path.read_text() + "\n# a new tweak\n")

    code, _, err = _unlock(lab, capsys, idea, key)

    assert code == EXIT_ERR and "already looked" in err
    assert len(_store(lab)["looks"]) == 1


def test_an_effect_that_does_not_survive_the_holdout_is_rejected(lab, capsys):
    idea, key = _idea_and_run(lab, capsys, "fades")
    code, out, _ = _unlock(lab, capsys, idea, key)

    assert code == EXIT_EMPTY, out
    rec = _record(lab, idea)
    assert rec["passed"] is False
    got = _status(lab, capsys, idea)
    assert got["status"] == "rejected" and "holdout_failed" in got["verdict"]
    assert len(_store(lab)["looks"]) == 1                 # a failed look is still a look


def test_an_effect_that_fades_fails_on_the_numbers_not_for_lack_of_trades(lab, capsys):
    idea, key = _idea_and_run(lab, capsys, "fades")
    _unlock(lab, capsys, idea, key)
    checks = _record(lab, idea)["checks"]
    assert checks["trades"]["ok"] is True
    assert checks["psr"]["ok"] is False


# --- the pass bar rises with every earlier look --------------------------------



def test_the_required_psr_rises_with_each_earlier_look():
    assert [round(required_psr(k), 4) for k in (0, 1, 2, 4)] == [0.8, 0.9, 0.9333, 0.96]


def test_a_holdout_that_passes_alone_fails_once_others_have_looked():
    marginal = _make_result([random.Random(i).gauss(13, 40) for i in range(40)])
    first = holdout_verdict(1.0, marginal, prior_looks=0)
    third = holdout_verdict(1.0, marginal, prior_looks=2)

    assert 0.8 < first["holdout"]["psr"] < required_psr(2)       # the fixture is on the fence
    assert first["passed"] is True
    assert third["passed"] is False and third["checks"]["psr"]["ok"] is False


def test_a_holdout_with_the_opposite_sign_fails_whatever_its_psr():
    losing = _make_result([random.Random(i).gauss(-15, 40) for i in range(40)])
    verdict = holdout_verdict(1.0, losing, prior_looks=0)
    assert verdict["passed"] is False and verdict["checks"]["sign"]["ok"] is False


# --- `validated` has to be earned ----------------------------------------------

def _set(lab, capsys, idea, *args):
    code = main(["ideas", "set", idea, *lab["ideas"], "--holdout-dir",
                 str(lab["tmp"] / "ideas_out"), "--json", *args])
    captured = capsys.readouterr()
    return code, captured.err


def _new_idea(lab, capsys):
    main(["ideas", "add", "candidate", "--source", "discretionary", *lab["ideas"], "--json"])
    return _out(capsys)["idea"]["id"]


def test_an_idea_cannot_be_marked_validated_without_a_passed_holdout(lab, capsys):
    idea = _new_idea(lab, capsys)
    _to_testing(lab, capsys, idea)
    code, err = _set(lab, capsys, idea, "--status", "validated")
    assert code == EXIT_ERR and "holdout" in err.lower()
    assert _status(lab, capsys, idea)["status"] == "testing"


def test_a_failed_holdout_does_not_license_validated(lab, capsys):
    idea = _new_idea(lab, capsys)
    _to_testing(lab, capsys, idea)
    d = lab["tmp"] / "ideas_out" / idea
    d.mkdir(parents=True, exist_ok=True)
    (d / "holdout.json").write_text(json.dumps({"state": "done", "passed": False}))
    assert _set(lab, capsys, idea, "--status", "validated")[0] == EXIT_ERR


def test_a_passed_holdout_licenses_validated(lab, capsys):
    idea = _new_idea(lab, capsys)
    _to_testing(lab, capsys, idea)
    d = lab["tmp"] / "ideas_out" / idea
    d.mkdir(parents=True, exist_ok=True)
    (d / "holdout.json").write_text(json.dumps({"state": "done", "passed": True}))
    assert _set(lab, capsys, idea, "--status", "validated")[0] == EXIT_OK
    assert _status(lab, capsys, idea)["status"] == "validated"


def test_other_edits_and_other_statuses_are_not_gated(lab, capsys):
    idea = _new_idea(lab, capsys)
    _to_testing(lab, capsys, idea)
    assert _set(lab, capsys, idea, "--verdict", "wip")[0] == EXIT_OK
    assert _set(lab, capsys, idea, "--status", "rejected", "--failure-kind", "no_edge",
                "--failure-assumption", "it drifts")[0] == EXIT_OK
