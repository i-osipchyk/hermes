"""The run ledger — the memory that makes an agent's search resumable (ADR-0010)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hermes.research.ledger import (
    AmbiguousKey,
    RunLedger,
    RunMeta,
    cache_key,
    restore_result,
    strategy_hash,
)


def _meta(strategy="ema_crossover", ticker="AAPL", created="2026-09-30T12:00:00+00:00", **kw):
    base = dict(
        strategy=strategy, strategy_hash="deadbeefdeadbeef", source="yfinance",
        ticker=ticker, universe=None, start="2021-01-01T00:00:00+00:00",
        end="2023-12-31T00:00:00+00:00", params={}, sizer="None",
        unconstrained=False, created_at=created,
    )
    base.update(kw)
    return RunMeta(**base)


def _result(sharpe=1.5, n=10):
    return {
        "metrics": {"sharpe": sharpe, "num_trades": n, "total_return": 0.2},
        "equity_curve": [["2021-01-01T00:00:00+00:00", 10_000.0],
                         ["2021-06-01T00:00:00+00:00", 12_000.0]],
        "trades": [{"side": "buy", "net_pnl": 100.0}],
        "vetoed_signals": [],
    }


def test_save_then_load_roundtrips(tmp_path):
    led = RunLedger(tmp_path)
    led.save("abc123", _result(), _meta())
    rec = led.load("abc123")
    assert rec is not None
    assert rec.key == "abc123"
    assert rec.meta.strategy == "ema_crossover"
    assert rec.metrics["sharpe"] == 1.5
    assert rec.trades[0]["net_pnl"] == 100.0


def test_load_missing_returns_none(tmp_path):
    assert RunLedger(tmp_path).load("nope") is None


def test_entries_are_newest_first_and_filterable(tmp_path):
    led = RunLedger(tmp_path)
    led.save("k1", _result(), _meta(ticker="AAPL", created="2026-01-01T00:00:00+00:00"))
    led.save("k2", _result(), _meta(ticker="MSFT", created="2026-06-01T00:00:00+00:00"))
    led.save("k3", _result(), _meta(strategy="gap_breakout", ticker="AAPL",
                                    created="2026-03-01T00:00:00+00:00"))

    assert [e.key for e in led.entries()] == ["k2", "k3", "k1"]
    assert [e.key for e in led.entries(strategy="gap_breakout")] == ["k3"]
    assert [e.key for e in led.entries(ticker="AAPL")] == ["k3", "k1"]
    assert [e.key for e in led.entries(limit=2)] == ["k2", "k3"]


def test_entries_skips_unreadable_rows_instead_of_failing(tmp_path):
    """A half-written or corrupt run must not take down the whole listing."""
    led = RunLedger(tmp_path)
    led.save("good", _result(), _meta())
    (tmp_path / "corrupt").mkdir()
    (tmp_path / "corrupt" / "meta.json").write_text("{not json")
    (tmp_path / "no_meta").mkdir()

    assert [e.key for e in led.entries()] == ["good"]


def test_entries_on_a_missing_root(tmp_path):
    assert RunLedger(tmp_path / "absent").entries() == []


# --- the two questions an agent asks ---------------------------------------

def test_find_answers_have_i_already_run_this(tmp_path):
    led = RunLedger(tmp_path)
    inputs = dict(
        strategy="ema_crossover", strategy_hash="abcd", source="yfinance",
        ticker="AAPL", universe=None, start="2021-01-01", end="2023-12-31",
        params={"fast": 10}, sizer="None", unconstrained=False,
    )
    assert led.find(**inputs) is None

    led.save(cache_key(**inputs), _result(), _meta())
    assert led.find(**inputs) is not None
    # any change to the inputs is a different experiment
    assert led.find(**{**inputs, "params": {"fast": 11}}) is None


def test_editing_a_strategy_invalidates_its_prior_runs(tmp_path):
    """The key covers the file hash, so a stale result is never served for
    code that would no longer produce it."""
    f = tmp_path / "s.py"
    f.write_text("x = 1\n")
    h1 = strategy_hash(f)
    f.write_text("x = 2\n")
    h2 = strategy_hash(f)
    assert h1 != h2

    common = dict(strategy="s", source="yfinance", ticker="AAPL", universe=None,
                  start="a", end="b", params={}, sizer="None", unconstrained=False)
    assert cache_key(strategy_hash=h1, **common) != cache_key(strategy_hash=h2, **common)


def test_cache_key_is_order_independent_but_value_sensitive():
    common = dict(strategy="s", strategy_hash="h", source="yfinance", ticker="AAPL",
                  universe=None, start="a", end="b", sizer="None", unconstrained=False)
    assert cache_key(params={"a": 1, "b": 2}, **common) == cache_key(params={"b": 2, "a": 1}, **common)
    assert cache_key(params={"a": 1}, **common) != cache_key(params={"a": 2}, **common)
    assert cache_key(params={}, leverage=2.0, **common) != cache_key(params={}, **common)


# --- key prefixes (so a CLI can take four characters) ----------------------

def test_resolve_expands_a_unique_prefix(tmp_path):
    led = RunLedger(tmp_path)
    led.save("abcd1234", _result(), _meta())
    assert led.resolve("abcd1234") == "abcd1234"
    assert led.resolve("abcd") == "abcd1234"


def test_resolve_rejects_an_ambiguous_prefix(tmp_path):
    led = RunLedger(tmp_path)
    led.save("abcd1111", _result(), _meta())
    led.save("abcd2222", _result(), _meta())
    with pytest.raises(AmbiguousKey):
        led.resolve("abcd")


def test_resolve_raises_on_no_match(tmp_path):
    with pytest.raises(KeyError):
        RunLedger(tmp_path).resolve("zzzz")


def test_delete_removes_the_run(tmp_path):
    led = RunLedger(tmp_path)
    led.save("k", _result(), _meta())
    led.delete("k")
    assert led.load("k") is None
    led.delete("k")  # idempotent


# --- fail-open surfacing ---------------------------------------------------

def test_fail_open_calls_is_read_off_the_stored_result(tmp_path):
    led = RunLedger(tmp_path)
    clean = _result()
    clean["llm_summary"] = {"total_calls": 5, "fail_open_calls": 0}
    dirty = _result()
    dirty["llm_summary"] = {"total_calls": 5, "fail_open_calls": 3}
    led.save("clean", clean, _meta())
    led.save("dirty", dirty, _meta())

    assert led.load("clean").fail_open_calls == 0
    assert led.load("dirty").fail_open_calls == 3
    assert led.load("dirty").fail_open_calls  # truthy → a reviewer must flag it


# --- restoring a result ----------------------------------------------------

def test_restore_result_rebuilds_metrics_and_curve():
    r = restore_result(_result(sharpe=2.0))
    assert r.metrics.sharpe == 2.0
    assert r.equity_curve[0][0] == datetime(2021, 1, 1, tzinfo=UTC)
    assert r.to_dict()["metrics"]["num_trades"] == 10


def test_restored_result_exposes_no_trade_objects():
    """Guards a subtle trap: readers use ``getattr(result, "trades", [])`` and expect
    real ``Trade`` objects. A restored run only has dicts, so it must not offer the
    attribute at all rather than hand back the wrong type."""
    assert not hasattr(restore_result(_result()), "trades")


def test_run_meta_label_is_human_readable():
    assert _meta().label == "ema_crossover · AAPL · 2021-01-01–2023-12-31"
    assert "sp500" in _meta(ticker=None, universe="sp500").label
