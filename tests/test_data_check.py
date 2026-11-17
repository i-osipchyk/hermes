"""The data check between the spec and `quantified`: does the data the spec names exist, or
whose problem is it? Credentials missing is the owner's; no source at all is the Developer's.
Offline: fake sources and a parquet cache in tmp_path."""

from datetime import UTC, datetime, timedelta

from hermes import CryptoPair, Symbol, Timeframe
from hermes.core import Bar
from hermes.data import BarCache
from hermes.research.data_check import check_spec_data
from tests.artifact_helpers import good_spec

H1, M5 = Timeframe.parse("1h"), Timeframe.parse("5m")
T0 = datetime(2023, 1, 2, tzinfo=UTC)


class FakeSource:
    def __init__(self, timeframes):
        self._tf = {Timeframe.parse(t) for t in timeframes}

    def supported_timeframes(self):
        return self._tf


def _sources(**named):
    return named


def _spec(instruments=("XAUUSD",), timeframes=("1h",)):
    return good_spec(instruments=list(instruments), timeframes=list(timeframes))


def test_cached_bars_are_reported_with_their_span(tmp_path):
    cache = BarCache(tmp_path)
    inst = CryptoPair(Symbol("XAUUSD", "pepperstone"), base_asset="X", quote_currency="USD",
                      tick_size=0.01)
    cache.write(inst, H1, [Bar(T0 + timedelta(hours=i), H1, 1, 2, 0.5, 1, 1.0) for i in range(48)])

    got = check_spec_data(_spec(), cache_root=tmp_path, env={},
                          sources=_sources(pepperstone=FakeSource(["1h"])))

    assert got.verdict == "ok"
    row = got.rows[0]
    assert row.state == "cached" and row.source == "pepperstone" and row.bars == 48
    assert row.first.startswith("2023-01-02") and row.last.startswith("2023-01-03")


def test_a_source_needing_credentials_that_are_not_set_is_the_owners_problem(tmp_path):
    got = check_spec_data(_spec(), cache_root=tmp_path, env={},
                          sources=_sources(pepperstone=FakeSource(["1h"])),
                          credentials={"pepperstone": ("CTRADER_ACCOUNT_ID",)})
    assert got.verdict == "owner_action"
    assert "CTRADER_ACCOUNT_ID" in got.rows[0].detail
    assert got.rows[0].state == "needs_credentials"


def test_credentials_present_but_nothing_probed_is_ok_and_says_unverified(tmp_path):
    got = check_spec_data(_spec(), cache_root=tmp_path, env={"CTRADER_ACCOUNT_ID": "1"},
                          sources=_sources(pepperstone=FakeSource(["1h"])),
                          credentials={"pepperstone": ("CTRADER_ACCOUNT_ID",)})
    assert got.verdict == "ok" and got.rows[0].state == "unprobed"


def test_no_source_offers_the_timeframe_is_a_missing_capability(tmp_path):
    got = check_spec_data(_spec(timeframes=("5m",)), cache_root=tmp_path, env={},
                          sources=_sources(yfinance=FakeSource(["1d"])))
    assert got.verdict == "needs_infra" and got.rows[0].state == "unavailable"
    assert "5m" in got.rows[0].detail


def test_a_probe_that_finds_bars_confirms_the_instrument(tmp_path):
    seen = []

    def probe(source, ticker, tf):
        seen.append((source, ticker, str(tf)))
        return 120

    got = check_spec_data(_spec(), cache_root=tmp_path, env={},
                          sources=_sources(yfinance=FakeSource(["1h"])), probe=probe)
    assert got.verdict == "ok" and got.rows[0].state == "probed" and got.rows[0].bars == 120
    assert seen == [("yfinance", "XAUUSD", "1h")]


def test_a_probe_that_finds_nothing_anywhere_is_a_missing_capability(tmp_path):
    def probe(source, ticker, tf):
        raise LookupError("unknown symbol")

    got = check_spec_data(_spec(), cache_root=tmp_path, env={},
                          sources=_sources(yfinance=FakeSource(["1h"])), probe=probe)
    assert got.verdict == "needs_infra" and got.rows[0].state == "not_found"
    assert "unknown symbol" in got.rows[0].detail


def test_a_failed_probe_with_a_credential_gated_source_left_untried_is_the_owners(tmp_path):
    def probe(source, ticker, tf):
        raise LookupError("no such ticker")

    got = check_spec_data(_spec(), cache_root=tmp_path, env={},
                          sources=_sources(yfinance=FakeSource(["1h"]),
                                           pepperstone=FakeSource(["1h"])),
                          credentials={"pepperstone": ("CTRADER_ACCOUNT_ID",)}, probe=probe)
    assert got.verdict == "owner_action"


def test_one_row_per_instrument_and_timeframe_and_the_worst_row_decides(tmp_path):
    got = check_spec_data(_spec(("A", "B"), ("1h", "5m")), cache_root=tmp_path, env={},
                          sources=_sources(yfinance=FakeSource(["1h"])))
    assert [(r.instrument, r.timeframe) for r in got.rows] == [
        ("A", "1h"), ("A", "5m"), ("B", "1h"), ("B", "5m")]
    assert got.verdict == "needs_infra"


def test_the_result_is_strict_json(tmp_path):
    import json
    got = check_spec_data(_spec(), cache_root=tmp_path, env={},
                          sources=_sources(yfinance=FakeSource(["1h"])))
    json.dumps(got.to_dict(), allow_nan=False)
