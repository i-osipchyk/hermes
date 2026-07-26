import json
from datetime import UTC, datetime, timedelta

from hermes import CryptoPair, Side, Symbol, Timeframe
from hermes.backtest.result import BacktestResult
from hermes.execution import Trade
from hermes.webui import discovery, review

H1 = Timeframe.parse("1h")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _btc():
    return CryptoPair(Symbol("BTCUSDT", "binance"), base_asset="BTC", quote_currency="USDT", tick_size=0.01)


# --- result serialization --------------------------------------------------

def _result():
    inst = _btc()
    t = Trade(
        instrument=inst, side=Side.BUY, size=1.0, entry_price=100.0, entry_time=T0,
        exit_price=110.0, exit_time=T0 + timedelta(hours=2), exit_reason="take_profit",
        gross_pnl=10.0, costs=1.0,
    )
    curve = [(T0, 10_000.0), (T0 + timedelta(hours=2), 10_009.0)]
    return BacktestResult.compute(curve, [t])


def test_to_dict_is_json_serializable():
    d = _result().to_dict()
    json.dumps(d)  # must not raise
    assert set(d) == {"metrics", "equity_curve", "trades"}
    assert d["trades"][0]["exit_reason"] == "take_profit"
    assert d["trades"][0]["net_pnl"] == 9.0
    assert d["equity_curve"][0][0] == T0.isoformat()


# --- review lifecycle ------------------------------------------------------

def test_run_id_content_addressed():
    a, b = _result().to_dict(), _result().to_dict()
    assert review.run_id(a) == review.run_id(b)
    b["metrics"]["sharpe"] = 999
    assert review.run_id(a) != review.run_id(b)


def test_review_status_transitions(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "REVIEWS_DIR", tmp_path / "reviews")
    d = _result().to_dict()
    rid = review.run_id(d)

    assert review.status(rid).state == "idle"
    review.write_result(d, rid)
    assert json.loads((tmp_path / "reviews" / rid / "result.json").read_text())["trades"]
    assert review.status(rid).state == "idle"  # result written, review not started

    (review.REVIEWS_DIR / rid / ".started").touch()
    assert review.status(rid).state == "running"

    review.review_path(rid).write_text("# Verdict\nLooks real.")
    assert review.status(rid).state == "done"
    assert review.read_review(rid).startswith("# Verdict")


def test_review_failed_when_exit_without_md(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "REVIEWS_DIR", tmp_path / "reviews")
    rid = "deadbeef"
    (review.REVIEWS_DIR / rid).mkdir(parents=True)
    (review.REVIEWS_DIR / rid / ".started").touch()
    (review.REVIEWS_DIR / rid / ".exit").write_text("1")
    assert review.status(rid).state == "failed"


def test_manual_instructions_mentions_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "REVIEWS_DIR", tmp_path / "reviews")
    txt = review.manual_instructions("abc123")
    assert "hermes-analyze-results" in txt and "review.md" in txt


# --- discovery -------------------------------------------------------------

_STRATEGY_FILE = '''
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from hermes import Backtest, Strategy, Symbol, Timeframe
from hermes.core import Bar, CryptoPair
from hermes.data import InMemorySource

GENERATED_BY = "hermes-strategy"
H1 = Timeframe.parse("1h")


@dataclass  # must not crash when loaded via importlib (needs sys.modules registration)
class Params:
    fast: int = 10


class Buy(Strategy):
    def setup(self): ...
    def on_bar(self, bar): ...


def build_backtest(**overrides):
    inst = CryptoPair(Symbol("BTCUSDT", "binance"), base_asset="BTC",
                      quote_currency="USDT", tick_size=0.01)
    t0 = datetime(2023, 1, 1, tzinfo=timezone.utc)
    bars = [Bar(t0 + timedelta(hours=i), H1, 100, 101, 99, 100, 1.0) for i in range(5)]
    bt = Backtest(
        strategy=Buy(), source=InMemorySource(inst, {H1: bars}),
        symbol=Symbol("BTCUSDT", "binance"), timeframes=[H1],
        start=t0, end=t0 + timedelta(hours=4), starting_cash=10_000,
    )
    return bt
'''


def test_discover_and_configure(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "buy.py").write_text(_STRATEGY_FILE)

    entries = discovery.discover(sdir)
    assert len(entries) == 1
    e = entries[0]
    assert e.name == "buy" and e.is_ai_generated

    default = discovery.default_config(e)
    assert default.symbol.ticker == "BTCUSDT"

    configured = discovery.configured_backtest(
        e, ticker="BTCUSDT", start=T0, end=T0 + timedelta(hours=3), starting_cash=5_000
    )
    assert configured.starting_cash == 5_000
    assert configured.strategy is not default.strategy  # fresh instance per build
